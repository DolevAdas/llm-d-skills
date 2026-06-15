"""Parse autoconfig-poc benchmark Job logs and render a metrics summary.

Consumes the stdout from `kubectl logs` on an autoconfig-benchmark Job, finds
the `---BENCHMARK RESULTS---` delimiter (emitted by benchmark.py's wrapped
shell command), extracts the JSON result files that follow, normalizes
inference-perf vs guidellm shapes, and emits a Markdown table.

Usage:
    kubectl logs -n <ns> -l job-name=autoconfig-bench-<hash> --tail=3000 \
        | python3 parse_bench_results.py

    # With SLA comparison (milliseconds):
    kubectl logs ... | python3 parse_bench_results.py \
        --ttft-sla 800 --tpot-sla 25 --e2e-sla 5000

    # From a saved log file:
    python3 parse_bench_results.py --logs bench-logs.txt

Exit codes:
    0  parse succeeded, no SLAs supplied OR all SLAs met
    1  parse succeeded, one or more SLAs breached
    2  parse failed (no delimiter, no JSON, malformed input)

Why this exists: the bench Job's wrapped shell command dumps the result JSON
files to stdout after the harness completes. Without this script, the agent
or user has to grep through thousands of lines and pull out p95s by hand.
"""

import argparse
import json
import re
import sys


_DELIMITER_RE = re.compile(r"^---BENCHMARK RESULTS.*?---$", re.MULTILINE)


def _read_input(logs_path: str | None) -> str:
    if logs_path:
        with open(logs_path) as f:
            return f.read()
    return sys.stdin.read()


def _extract_json_blocks(text: str) -> list[dict]:
    """Find the delimiter, then pull every top-level JSON object that follows.

    The `find ... -print -exec cat` block in benchmark.py prints `<path>\n<contents>`
    for each file. JSON files are pretty-printed multi-line objects. We use a
    brace-counting walk rather than regex to handle nested structures correctly.
    """
    match = _DELIMITER_RE.search(text)
    if not match:
        return []

    tail = text[match.end():]
    blocks: list[dict] = []
    i = 0
    while i < len(tail):
        # Skip until the next '{' that starts a top-level JSON object.
        if tail[i] != "{":
            i += 1
            continue
        depth = 0
        in_string = False
        escape = False
        start = i
        while i < len(tail):
            ch = tail[i]
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = not in_string
            elif not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = tail[start:i + 1]
                        try:
                            blocks.append(json.loads(candidate))
                        except json.JSONDecodeError:
                            pass  # Not valid JSON; skip.
                        i += 1
                        break
            i += 1
        else:
            break  # Unterminated; bail out.
    return blocks


# ----------------------------------------------------------------------------
# Format detection + normalization
# ----------------------------------------------------------------------------


def _detect_format(block: dict) -> str | None:
    """Return 'inference-perf', 'guidellm', or None.

    inference-perf: top-level has `successes.latency.request_latency` shape.
    guidellm: top-level has `benchmarks` list with `metrics.time_to_first_token_ms`.
    """
    if isinstance(block.get("successes"), dict) and isinstance(
        block["successes"].get("latency"), dict
    ):
        return "inference-perf"
    if isinstance(block.get("benchmarks"), list) and block["benchmarks"]:
        first = block["benchmarks"][0]
        if isinstance(first, dict) and isinstance(first.get("metrics"), dict):
            return "guidellm"
    return None


def _normalize_inference_perf(block: dict) -> dict | None:
    """Pull common metrics from an inference-perf summary JSON. Latencies are
    in seconds in the source; we convert TTFT/TPOT/e2e to milliseconds and
    throughput stays in native units (req/s, tokens/s)."""
    successes = block.get("successes") or {}
    latency = successes.get("latency") or {}
    throughput = successes.get("throughput") or {}
    failures = block.get("failures") or {}

    def pct(metric: str, key: str) -> float | None:
        d = latency.get(metric) or {}
        return d.get(key)

    def to_ms(v: float | None) -> float | None:
        return v * 1000.0 if v is not None else None

    return {
        "harness": "inference-perf",
        "count_success": successes.get("count"),
        "count_failure": failures.get("count", 0),
        "metrics_ms": {
            "request_latency": {
                "mean": to_ms(pct("request_latency", "mean")),
                "p50": to_ms(pct("request_latency", "median")),
                "p95": to_ms(pct("request_latency", "p95")),
                "p99": to_ms(pct("request_latency", "p99")),
            },
            "ttft": {
                "mean": to_ms(pct("time_to_first_token", "mean")),
                "p50": to_ms(pct("time_to_first_token", "median")),
                "p95": to_ms(pct("time_to_first_token", "p95")),
                "p99": to_ms(pct("time_to_first_token", "p99")),
            },
            "tpot": {
                "mean": to_ms(pct("time_per_output_token", "mean")),
                "p50": to_ms(pct("time_per_output_token", "median")),
                "p95": to_ms(pct("time_per_output_token", "p95")),
                "p99": to_ms(pct("time_per_output_token", "p99")),
            },
        },
        "throughput": {
            "requests_per_sec": throughput.get("requests_per_sec"),
            "input_tokens_per_sec": throughput.get("input_tokens_per_sec"),
            "output_tokens_per_sec": throughput.get("output_tokens_per_sec"),
            "total_tokens_per_sec": throughput.get("total_tokens_per_sec"),
        },
    }


def _normalize_guidellm(block: dict) -> dict | None:
    """Pull common metrics from guidellm's benchmarks_aggregator.json. guidellm
    reports TTFT / ITL in milliseconds natively. Multiple benchmarks may be
    present (one per rate stage); we take the last (highest-rate) one which
    is the most loaded and most interesting for SLA validation."""
    benchmarks = block.get("benchmarks") or []
    if not benchmarks:
        return None
    last = benchmarks[-1]
    metrics = last.get("metrics") or {}

    def pct(metric: str, key: str) -> float | None:
        d = metrics.get(metric) or {}
        return d.get(key)

    return {
        "harness": "guidellm",
        "count_success": last.get("successful_requests"),
        "count_failure": last.get("errored_requests", 0),
        "metrics_ms": {
            "request_latency": {
                "mean": pct("request_latency", "mean"),
                "p50": pct("request_latency", "median"),
                "p95": pct("request_latency", "p95"),
                "p99": pct("request_latency", "p99"),
            },
            "ttft": {
                "mean": pct("time_to_first_token_ms", "mean"),
                "p50": pct("time_to_first_token_ms", "median"),
                "p95": pct("time_to_first_token_ms", "p95"),
                "p99": pct("time_to_first_token_ms", "p99"),
            },
            "tpot": {
                "mean": pct("inter_token_latency_ms", "mean"),
                "p50": pct("inter_token_latency_ms", "median"),
                "p95": pct("inter_token_latency_ms", "p95"),
                "p99": pct("inter_token_latency_ms", "p99"),
            },
        },
        "throughput": {
            "requests_per_sec": last.get("requests_per_second"),
            "input_tokens_per_sec": last.get("prompt_tokens_per_second"),
            "output_tokens_per_sec": last.get("output_tokens_per_second"),
            "total_tokens_per_sec": None,  # guidellm doesn't aggregate input+output
        },
    }


def _normalize(blocks: list[dict]) -> dict | None:
    """Try each block until one matches a known format."""
    for block in blocks:
        fmt = _detect_format(block)
        if fmt == "inference-perf":
            return _normalize_inference_perf(block)
        if fmt == "guidellm":
            return _normalize_guidellm(block)
    return None


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------


def _fmt(v: float | None, decimals: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:.{decimals}f}"


def render_markdown(summary: dict, slas: dict | None = None) -> tuple[str, bool]:
    """Render the summary as a markdown report. Returns (text, any_sla_breach)."""
    m = summary["metrics_ms"]
    tp = summary["throughput"]
    any_breach = False
    lines: list[str] = []

    lines.append(f"**Benchmark harness:** {summary['harness']}")
    fail_count = summary.get("count_failure") or 0
    lines.append(
        f"**Requests:** {summary.get('count_success', '—')} successful, "
        f"{fail_count} failed"
    )
    lines.append("")
    lines.append("| Metric | mean | p50 | p95 | p99 |")
    lines.append("|---|---|---|---|---|")
    for label, key in [
        ("Request latency (ms)", "request_latency"),
        ("TTFT (ms)", "ttft"),
        ("TPOT (ms)", "tpot"),
    ]:
        d = m.get(key) or {}
        lines.append(
            f"| {label} | {_fmt(d.get('mean'))} | {_fmt(d.get('p50'))} | "
            f"{_fmt(d.get('p95'))} | {_fmt(d.get('p99'))} |"
        )
    lines.append("")
    rps = tp.get("requests_per_sec")
    in_tps = tp.get("input_tokens_per_sec")
    out_tps = tp.get("output_tokens_per_sec")
    tot_tps = tp.get("total_tokens_per_sec")
    lines.append(
        f"**Throughput:** {_fmt(rps, 2)} req/s · "
        f"{_fmt(in_tps, 0)} input tokens/s · "
        f"{_fmt(out_tps, 0)} output tokens/s"
        + (f" · {_fmt(tot_tps, 0)} total tokens/s" if tot_tps is not None else "")
    )

    if slas:
        lines.append("")
        lines.append("**SLA validation (p95):**")
        for label, sla_key, metric_key in [
            ("TTFT", "ttft_ms", "ttft"),
            ("TPOT", "tpot_ms", "tpot"),
            ("End-to-end", "e2e_ms", "request_latency"),
        ]:
            target = slas.get(sla_key)
            if target is None:
                continue
            measured = (m.get(metric_key) or {}).get("p95")
            if measured is None:
                lines.append(f"- {label}: target {target} ms, measured —")
                continue
            if measured <= target:
                lines.append(
                    f"- {label}: {measured:.0f} ms (target: {target} ms) ✓ within target"
                )
            else:
                breach_pct = (measured - target) / target * 100
                lines.append(
                    f"- {label}: {measured:.0f} ms (target: {target} ms) "
                    f"✗ EXCEEDS by {breach_pct:.0f}%"
                )
                any_breach = True

    return "\n".join(lines) + "\n", any_breach


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--logs",
        help="Path to a saved logs file. If omitted, reads from stdin.",
    )
    parser.add_argument("--ttft-sla", type=float, help="TTFT p95 target in ms.")
    parser.add_argument("--tpot-sla", type=float, help="TPOT p95 target in ms.")
    parser.add_argument(
        "--e2e-sla", type=float, help="End-to-end request latency p95 target in ms."
    )
    args = parser.parse_args(argv)

    text = _read_input(args.logs)
    blocks = _extract_json_blocks(text)
    if not blocks:
        print(
            "error: no JSON blocks found after `---BENCHMARK RESULTS---` delimiter. "
            "Did the Job complete? Check `kubectl logs --tail=3000` directly.",
            file=sys.stderr,
        )
        return 2

    summary = _normalize(blocks)
    if summary is None:
        print(
            "error: extracted JSON blocks but none matched inference-perf or "
            "guidellm format. Run with --logs to a file and inspect manually.",
            file=sys.stderr,
        )
        return 2

    slas = {}
    if args.ttft_sla is not None:
        slas["ttft_ms"] = args.ttft_sla
    if args.tpot_sla is not None:
        slas["tpot_ms"] = args.tpot_sla
    if args.e2e_sla is not None:
        slas["e2e_ms"] = args.e2e_sla

    report, any_breach = render_markdown(summary, slas or None)
    print(report)
    return 1 if any_breach else 0


if __name__ == "__main__":
    sys.exit(main())

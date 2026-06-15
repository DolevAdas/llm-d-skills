"""Benchmark generation for the autoconfig POC.

Splits two concerns from autoconfig_poc.py:

1. **build_benchmark()** — produces a guidellm benchmark config dict tied to
   the same model + namespace + workload signals as the EPP config. Same
   workload-class-aware logic as before (sanity always, sla_validation when
   SLOs present, rates derived from cluster size + workload class).

2. **build_benchmark_deployment()** — produces a complete multi-document K8s
   YAML (ConfigMap + Job, optional PVC) that's ready to `kubectl apply -f`.
   Substitution happens at script-call time via target_url / namespace /
   pvc_name args — no placeholders, no agent-side sed.

Imports nothing from autoconfig_poc.py to keep the dependency one-way. Type
annotations on `inp` use a forward reference so we don't need to import the
Input dataclass.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from autoconfig_poc import Input


# Image picked by harness:
# - guidellm uses the llm-d-benchmark wrapper image (the wrapper handles
#   guidellm correctly).
# - inference-perf uses the native upstream image directly. The llm-d-benchmark
#   wrapper at v0.5.2 passes the workload file through to inference-perf
#   without extracting the workload-name block, which makes it expect a flat
#   schema while the wrapper templates (e.g. sanity.yaml) ship the nested
#   `workload.<name>` shape. Bypassing the wrapper for inference-perf also
#   means we set the entrypoint + args explicitly (no more `-l/-w` flags).
#   Source for native command: kubernetes-sigs/inference-perf
#   deploy/inference-perf/templates/job.yaml in the upstream repo
#   (https://github.com/kubernetes-sigs/inference-perf).
_BENCHMARK_HARNESS_IMAGE = "ghcr.io/llm-d/llm-d-benchmark:v0.5.2"
_INFERENCE_PERF_IMAGE = "quay.io/inference-perf/inference-perf:latest"

# Baseline req/s per replica per workload class. Tuned for the "canonical"
# input of each class (the ISL/OSL that defines the class). T4 — principle.
_BENCHMARK_BASE_RATES: dict[str, dict] = {
    "balanced-conversational": {"per_replica_qps": 5.0, "base_isl": 1000, "base_osl": 500},
    "high-prefix-share":       {"per_replica_qps": 1.0, "base_isl": 4000, "base_osl": 300},
    "latency-tight":           {"per_replica_qps": 3.0, "base_isl": 500,  "base_osl": 200},
}


def _estimate_cluster_qps(inp: "Input", workload_class: str) -> float:
    """Rough cluster-wide max QPS estimate. T4-tier heuristic.

    base_qps_per_replica × replicas, adjusted for the user's ISL/OSL relative
    to the workload class's canonical ISL/OSL. Long prompts/outputs reduce
    per-replica throughput; short ones raise it. Adjustment clamped to
    [0.25×, 4×] so wildly-off inputs can't produce nonsense rates.
    """
    base = _BENCHMARK_BASE_RATES.get(workload_class, _BENCHMARK_BASE_RATES["balanced-conversational"])
    per_replica = base["per_replica_qps"]

    if inp.workload.isl and inp.workload.osl:
        actual_token_cost = inp.workload.isl * inp.workload.osl
        base_token_cost = base["base_isl"] * base["base_osl"]
        adjustment = max(0.25, min(4.0, base_token_cost / actual_token_cost))
        per_replica *= adjustment

    # PD-aware: total pod count is prefill + decode under disagg. The "QPS per
    # replica" baseline still applies — both prefill and decode pods serve
    # tokens (split across the request lifecycle), and benchmark rates are
    # naturally bounded by the slower side, so summing both pool sizes for
    # the QPS estimate is the right first-order approximation.
    return inp.topology.total_replicas() * per_replica


def _sla_validation_rates(cluster_qps: float) -> list[int]:
    """Three rates along the saturation curve: 25% / 50% / 85% of estimated max."""
    rate_low = max(1, int(round(cluster_qps * 0.25)))
    rate_mid = max(rate_low + 1, int(round(cluster_qps * 0.50)))
    rate_near = max(rate_mid + 1, int(round(cluster_qps * 0.85)))
    return [rate_low, rate_mid, rate_near]


def _benchmark_data_block(inp: "Input", workload_class: str, *, with_distribution: bool) -> dict:
    """The data section of a guidellm workload.

    - sanity (with_distribution=False): fixed prompt_tokens + output_tokens.
    - SLA validation (with_distribution=True): mean + stdev + min/max + samples.

    Uses ISL/OSL when known; falls back to guidellm-template defaults (50/50)
    so the config is still runnable on missing inputs.

    NOTE: guidellm has no shared-prefix synthesis. For high-prefix-share
    workloads with prefix_len, build_benchmark surfaces a warning. The data
    block here just emits the full ISL as prompt_tokens.
    """
    prompt_tokens = inp.workload.isl if inp.workload.isl else 50
    output_tokens = inp.workload.osl if inp.workload.osl else 50

    if not with_distribution:
        return {
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
        }

    return {
        "prompt_tokens_min": max(10, int(prompt_tokens * 0.5)),
        "prompt_tokens_max": int(prompt_tokens * 1.5),
        "prompt_tokens": prompt_tokens,
        "prompt_tokens_stdev": max(1, int(prompt_tokens * 0.1)),
        "output_tokens_min": max(10, int(output_tokens * 0.5)),
        "output_tokens_max": int(output_tokens * 1.5),
        "output_tokens": output_tokens,
        "output_tokens_stdev": max(1, int(output_tokens * 0.1)),
        "samples": 1000,
    }


def build_benchmark(inp: "Input", workload_class: str, *, harness: str = "guidellm") -> dict:
    """Construct a benchmark config for the chosen harness (guidellm or inference-perf).

    Returns {"config": dict, "harness": str, "warnings": list[str]}. The config dict has
    ${IP}, ${NAMESPACE}, ${BENCHMARK_PVC} placeholders that get substituted
    later — either by the agent OR (preferred) by passing real values to
    build_benchmark_deployment().

    Trade-offs:
    - guidellm: simpler schema, fixed prompt distributions (with stdev/min/max for
      SLA validation), no shared-prefix synthesis support.
    - inference-perf: richer schema with random Gaussian distributions and proper
      shared_prefix synthesis. Recommended for rag-style workloads with prefix_len.
    """
    if harness not in ("guidellm", "inference-perf"):
        raise ValueError(f"harness must be 'guidellm' or 'inference-perf'; got {harness!r}")

    if harness == "guidellm":
        return _build_guidellm(inp, workload_class)
    return _build_inference_perf(inp, workload_class)


def _build_guidellm(inp: "Input", workload_class: str) -> dict:
    """guidellm config — matches optimized-baseline/guidellm.yaml structure."""
    model_basename = inp.model.split("/")[-1] if "/" in inp.model else inp.model
    stack_name = f"{inp.context.release_name}-{model_basename}"

    workloads: dict[str, dict] = {}
    bench_warnings: list[str] = []

    sanity_data = _benchmark_data_block(inp, workload_class, with_distribution=False)
    workloads["sanity"] = {
        "target": "${IP}",
        "model": inp.model,
        "request_type": "text_completions",
        "profile": "constant",
        "rate": [1],
        "max_seconds": 30,
        "data": sanity_data,
    }

    if inp.slo.ttft_ms or inp.slo.tpot_ms or inp.slo.request_latency_ms:
        cluster_qps = _estimate_cluster_qps(inp, workload_class)
        rates = _sla_validation_rates(cluster_qps)
        sla_data = _benchmark_data_block(inp, workload_class, with_distribution=True)
        sla_workload = {
            "target": "${IP}",
            "model": inp.model,
            "request_type": "text_completions",
            "profile": "constant",
            "rate": rates,
            "max_seconds": 120,
            "data": sla_data,
        }
        sla_targets = {}
        if inp.slo.ttft_ms is not None:
            sla_targets["ttft_ms_p95_target"] = inp.slo.ttft_ms
        if inp.slo.tpot_ms is not None:
            sla_targets["tpot_ms_p95_target"] = inp.slo.tpot_ms
        if inp.slo.request_latency_ms is not None:
            sla_targets["request_latency_ms_p95_target"] = inp.slo.request_latency_ms
        if sla_targets:
            sla_workload["sla_targets"] = sla_targets
        sla_workload["_rate_derivation"] = (
            f"workload class {workload_class} baseline "
            f"{_BENCHMARK_BASE_RATES.get(workload_class, {}).get('per_replica_qps', 'n/a')} req/s/replica "
            f"× {inp.topology.total_replicas()} replicas, adjusted for ISL/OSL → "
            f"~{cluster_qps:.1f} req/s estimated max; rates are 25%/50%/85% of that"
        )
        workloads["sla_validation"] = sla_workload

    if workload_class == "high-prefix-share" and inp.workload.prefix_len:
        bench_warnings.append(
            "guidellm does not synthesize shared prefixes — this benchmark sends "
            "uniformly-distributed prompts and won't exercise prefix-cache routing. "
            "For shared-prefix benchmarking, switch to the inference-perf harness "
            "(--bench-harness inference-perf) which supports proper shared_prefix synthesis."
        )

    # Resolve HF secret name from context. Null when user picked Q0.5 option 3
    # (skip — public model); in that case the bench Job's HF_TOKEN env is
    # omitted entirely (build_benchmark_deployment checks the sentinel).
    hf_secret_name = inp.context.hf_secret_name

    # Tokenizer override: same Phase 2 Q5.5 signal that inference-perf consumes
    # (context.bench_tokenizer_override, set when the served model isn't on HF
    # but a public HF tokenizer can be used). Emitted as `endpoint.tokenizer`
    # which the llm-d-benchmark wrapper passes to guidellm's --tokenizer CLI
    # flag. We omit the key entirely when unset so older wrapper versions that
    # don't recognize it stay clean — the wrapper just ignores unknown fields.
    config_endpoint: dict = {
        "stack_name": stack_name,
        "model": inp.model,
        "namespace": "${NAMESPACE}",
        "base_url": "${IP}",
        "hf_token_secret": hf_secret_name,
    }
    if inp.context.bench_tokenizer_override:
        config_endpoint["tokenizer"] = inp.context.bench_tokenizer_override

    config = {
        "endpoint": config_endpoint,
        "control": {
            "work_dir": "$HOME/llm-d-bench-work",
            "kubectl": "kubectl",
        },
        "harness": {
            "name": "guidellm",
            "results_pvc": "${BENCHMARK_PVC}",
            "namespace": "${NAMESPACE}",
            "parallelism": 1,
            "wait_timeout": 6000,
            "image": _BENCHMARK_HARNESS_IMAGE,
        },
        "workload": workloads,
        # Sentinel for build_benchmark_deployment to wire HF_TOKEN env. Same
        # value as endpoint.hf_token_secret; duplicated as a sentinel so the
        # deployment builder doesn't have to know which key holds it (the
        # inference-perf flat config has no `endpoint` block). Null = skip
        # the env entirely (public model, no token needed).
        "_hf_token_secret": hf_secret_name,
    }

    return {"config": config, "harness": "guidellm", "warnings": bench_warnings}


def _build_inference_perf(inp: "Input", workload_class: str) -> dict:
    """inference-perf config — FLAT native schema as the inference-perf binary
    expects. Source: kubernetes-sigs/inference-perf — `config.yml` at the repo
    root + `deploy/inference-perf/templates/job.yaml`
    (https://github.com/kubernetes-sigs/inference-perf).

    The native binary (`inference-perf --config_file <path>`) reads a flat
    document with `load`, `api`, `server`, `tokenizer`, `data`, `report`,
    `storage` at the top level — NO `endpoint`, `control`, `harness`, or
    `workload.<name>` wrappers. The wrappers are an artifact of the
    llm-d-benchmark image's templating; we bypass that wrapper for inference-
    perf (build_benchmark_deployment uses the native image
    quay.io/inference-perf/inference-perf instead).

    Multiple workloads (sanity + sla_validation) collapse to a single multi-
    stage `load` block — inference-perf runs one config per invocation, and a
    single multi-stage load is its native way to express a rate ladder.

    Differences from guidellm:
    - Flat schema (no wrapper keys).
    - Data block has an explicit `type` discriminator (random | shared_prefix).
    - For high-prefix-share with prefix_len, uses `type: shared_prefix` — the
      whole reason to pick this harness over guidellm.

    Sentinel keys (prefix `_`) are stripped before YAML serialization but
    preserved through the dict pipeline:
    - `_hf_token_secret`: name of the K8s Secret holding the HF token; read by
      build_benchmark_deployment to wire HF_TOKEN env into the Job.
    - `_rate_derivation`: human-readable rate-derivation explainer.
    """
    bench_warnings: list[str] = []

    has_slo = bool(inp.slo.ttft_ms or inp.slo.tpot_ms or inp.slo.request_latency_ms)

    # Build the combined stages list: 30s warmup at rate=1, then the SLA
    # rate ladder (25%/50%/85% × 120s) when SLOs are provided.
    stages: list[dict] = [{"rate": 1, "duration": 30}]
    rate_derivation: str | None = None
    if has_slo:
        cluster_qps = _estimate_cluster_qps(inp, workload_class)
        rates = _sla_validation_rates(cluster_qps)
        stages.extend({"rate": r, "duration": 120} for r in rates)
        rate_derivation = (
            f"workload class {workload_class} baseline "
            f"{_BENCHMARK_BASE_RATES.get(workload_class, {}).get('per_replica_qps', 'n/a')} req/s/replica "
            f"× {inp.topology.total_replicas()} replicas, adjusted for ISL/OSL → "
            f"~{cluster_qps:.1f} req/s estimated max; rates are 25%/50%/85% of that "
            "(prepended with 30s warmup at rate=1)"
        )

    # Tokenizer: defaults to the served model id, but agent can override via
    # context.bench_tokenizer_override when the model isn't on HF (proprietary
    # weights from PVC/GCS/S3 + a public HF tokenizer the variant inherits from,
    # e.g. meta-llama/Llama-3.1-8B-Instruct for a proprietary Llama variant).
    # Set by Phase 2 Q5.5 when the HF config.json fetch returned 404.
    tokenizer_ref = inp.context.bench_tokenizer_override or inp.model

    config: dict = {
        "load": {"type": "constant", "stages": stages},
        "api": {"type": "completion", "streaming": True},
        "server": {
            "type": "vllm",
            "model_name": inp.model,
            "base_url": "${IP}",
            "ignore_eos": True,
        },
        "tokenizer": {"pretrained_model_name_or_path": tokenizer_ref},
        "data": _inference_perf_data_block(inp, workload_class),
        "report": {
            "request_lifecycle": {"summary": True, "per_stage": True, "per_request": True},
        },
        # Mount-over bug: the inference-perf image has WORKDIR /workspace and
        # ships its venv at /workspace/.venv (PATH includes /workspace/.venv/bin).
        # Mounting an emptyDir or PVC at /workspace shadows the venv, producing
        # "sh: inference-perf: not found". Use /results instead.
        # build_benchmark_deployment mounts the results volume at /results to match.
        "storage": {"local_storage": {"path": "/results"}},
    }

    # SLA targets for downstream comparison logic (skill Phase 7.4 reads these).
    if has_slo:
        sla_targets: dict = {}
        if inp.slo.ttft_ms is not None:
            sla_targets["ttft_ms_p95_target"] = inp.slo.ttft_ms
        if inp.slo.tpot_ms is not None:
            sla_targets["tpot_ms_p95_target"] = inp.slo.tpot_ms
        if inp.slo.request_latency_ms is not None:
            sla_targets["request_latency_ms_p95_target"] = inp.slo.request_latency_ms
        if sla_targets:
            config["_sla_targets"] = sla_targets
    if rate_derivation:
        config["_rate_derivation"] = rate_derivation

    # Sentinel: HF token Secret name. inference-perf may pull a gated tokenizer
    # from HuggingFace; HF_TOKEN gets injected as env in build_benchmark_deployment.
    # Null when Phase 2 Q0.5 = skip (public model); env block omitted.
    config["_hf_token_secret"] = inp.context.hf_secret_name

    return {"config": config, "harness": "inference-perf", "warnings": bench_warnings}


def _inference_perf_data_block(inp: "Input", workload_class: str) -> dict:
    """inference-perf data block — random Gaussian by default, shared_prefix for high-prefix-share with prefix_len."""
    if workload_class == "high-prefix-share" and inp.workload.prefix_len:
        # The whole reason to pick inference-perf for RAG: real shared-prefix synthesis.
        # Total prompt = system_prompt_len + question_len; we map ISL → that sum.
        prompt_tokens = inp.workload.isl if inp.workload.isl else 4000
        output_tokens = inp.workload.osl if inp.workload.osl else 300
        question_len = max(64, prompt_tokens - inp.workload.prefix_len)
        return {
            "type": "shared_prefix",
            "shared_prefix": {
                "num_groups": 32,
                "num_prompts_per_group": 32,
                "system_prompt_len": inp.workload.prefix_len,
                "question_len": question_len,
                "output_len": output_tokens,
            },
        }

    # Random Gaussian
    prompt_mean = inp.workload.isl if inp.workload.isl else 50
    output_mean = inp.workload.osl if inp.workload.osl else 50
    return {
        "type": "random",
        "input_distribution": {
            "min": max(10, int(prompt_mean * 0.5)),
            "max": int(prompt_mean * 1.5),
            "mean": prompt_mean,
            "std_dev": max(1, int(prompt_mean * 0.1)),
            "total_count": 100,
        },
        "output_distribution": {
            "min": max(10, int(output_mean * 0.5)),
            "max": int(output_mean * 1.5),
            "mean": output_mean,
            "std_dev": max(1, int(output_mean * 0.1)),
            "total_count": 100,
        },
    }


# ---------------------------------------------------------------------------
# build_benchmark_deployment — produce a complete K8s YAML for `kubectl apply -f`
# ---------------------------------------------------------------------------


def _substitute(value, target_url: str, namespace: str, pvc_name: str | None):
    """Recursively walk the benchmark config dict and replace ${IP},
    ${NAMESPACE}, ${BENCHMARK_PVC} placeholders with real values.

    pvc_name=None means "use emptyDir for results storage"; we substitute the
    placeholder with an empty-string sentinel that build_benchmark_deployment
    later interprets as emptyDir.
    """
    if isinstance(value, str):
        out = value
        out = out.replace("${IP}", target_url)
        out = out.replace("${NAMESPACE}", namespace)
        out = out.replace("${BENCHMARK_PVC}", pvc_name if pvc_name else "")
        return out
    if isinstance(value, dict):
        return {k: _substitute(v, target_url, namespace, pvc_name) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, target_url, namespace, pvc_name) for v in value]
    return value


def _job_name_suffix(config: dict) -> str:
    """Deterministic 8-char hash suffix from the config so re-running with the
    same input produces the same Job name (idempotent kubectl apply)."""
    canonical = json.dumps(config, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


def _strip_sentinels(value):
    """Recursively drop dict keys starting with `_` so the rendered YAML in the
    ConfigMap doesn't carry our internal scaffolding (e.g. `_hf_token_secret`,
    `_rate_derivation`, `_sla_targets`). The sentinels stay on the in-memory
    dict so build_benchmark_deployment can read them BEFORE this strip happens.
    """
    if isinstance(value, dict):
        return {k: _strip_sentinels(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [_strip_sentinels(v) for v in value]
    return value


def build_benchmark_deployment(
    benchmark_config: dict,
    target_url: str,
    namespace: str,
    pvc_name: str | None = None,
    job_name_suffix: str | None = None,
) -> str:
    """Build a complete multi-document K8s YAML for one-shot kubectl apply.

    Produces:
        ---
        ConfigMap   (the substituted workload config, mounted in the Job)
        ---
        Job         (mounts the ConfigMap, runs the harness image, writes results)

    pvc_name behavior:
        - provided: Job mounts the named PVC at /workspace for results
        - None: Job uses emptyDir at /workspace (results lost when pod terminates)

    job_name_suffix:
        - provided: used directly (caller's choice; useful for re-runs)
        - None: derived deterministically from a hash of the substituted config

    Per-harness branching:

    - guidellm: uses the llm-d-benchmark wrapper image with `-l guidellm
      -w <file>` args. ConfigMap mounted at /profiles/guidellm/ as the wrapper
      expects. The wrapper handles guidellm correctly.

    - inference-perf: uses the NATIVE inference-perf image directly (the
      llm-d-benchmark wrapper at v0.5.2 passes the workload file through
      without extracting `workload.<name>`, which makes inference-perf reject
      the nested schema). Command is the native `inference-perf --config_file
      /etc/config/config.yml --log-level INFO`. ConfigMap mounted at
      /etc/config/ with key `config.yml`.

    Both harnesses get HF_TOKEN env (from the config's `_hf_token_secret`
    sentinel) marked optional so non-gated public models still work.
    inference-perf additionally gets RAYON_NUM_THREADS=4 (its tokenizer is
    Rust-backed and benefits from explicit thread setting).

    The Job's container command is wrapped in `sh -c` so the harness binary's
    exit code is preserved AND a post-completion `cat /workspace/**/*.json`
    surfaces result files in `kubectl logs` output (otherwise the user has to
    exec into a completed pod, which is impossible for finished pods).
    """
    substituted = _substitute(benchmark_config, target_url, namespace, pvc_name)
    # Hash the SCRUBBED dict (no sentinels) so the suffix is stable regardless
    # of which sentinels we add over time.
    scrubbed = _strip_sentinels(substituted)
    suffix = job_name_suffix or _job_name_suffix(scrubbed)
    cm_name = f"autoconfig-bench-config-{suffix}"
    job_name = f"autoconfig-bench-{suffix}"

    # Harness-name resolution:
    # - guidellm: nested under config.harness.name (the llm-d-benchmark wrapper
    #   structure)
    # - inference-perf: flat config has no `harness` block; we identify it by
    #   the presence of the top-level `load.stages` shape
    if "harness" in benchmark_config and isinstance(benchmark_config["harness"], dict):
        harness_name = benchmark_config["harness"].get("name", "guidellm")
    elif "load" in benchmark_config and isinstance(benchmark_config.get("load"), dict):
        harness_name = "inference-perf"
    else:
        raise ValueError(
            "could not determine harness from config; expected either "
            "config.harness.name (guidellm) or top-level config.load.stages "
            "(inference-perf)"
        )
    if harness_name not in ("guidellm", "inference-perf"):
        raise ValueError(f"unsupported harness {harness_name!r} in config; expected guidellm or inference-perf")

    # Per-harness layout: image, command/args, ConfigMap mount path/filename,
    # and where the results volume gets mounted.
    #
    # Mount-over bug:
    # - inference-perf image has WORKDIR /workspace + venv at /workspace/.venv
    #   + PATH=/workspace/.venv/bin:$PATH. Mounting any volume at /workspace
    #   shadows the venv. Use /results instead, and tell the workload config
    #   (storage.local_storage.path) to write there too.
    # - guidellm wrapper image (ghcr.io/llm-d/llm-d-benchmark) writes results
    #   to /workspace by default and has no venv conflict there. Keep /workspace.
    if harness_name == "guidellm":
        image = _BENCHMARK_HARNESS_IMAGE
        config_mount_path = "/profiles/guidellm"
        config_filename = "autoconfig-workload.yaml"
        results_mount_path = "/workspace"
        # Wrap with sh -c so we cat result JSONs to stdout post-completion.
        # The llm-d-benchmark wrapper's entrypoint accepts -l/-w as positional
        # args; we re-invoke it via the script entrypoint then dump results.
        runner_cmd = f"/usr/local/bin/llm-d-benchmark -l guidellm -w {config_filename}"
    else:  # inference-perf
        image = _INFERENCE_PERF_IMAGE
        config_mount_path = "/etc/config"
        config_filename = "config.yml"  # native inference-perf default
        results_mount_path = "/results"
        runner_cmd = f"inference-perf --config_file {config_mount_path}/{config_filename} --log-level INFO"

    # Single shell wrapper: run harness, capture exit code, dump JSON results
    # to stdout (so `kubectl logs` shows them), exit with the harness's code.
    wrapped_cmd = (
        f"set -o pipefail; "
        f"{runner_cmd}; rc=$?; "
        f"echo '---BENCHMARK RESULTS (json files in {results_mount_path})---'; "
        f"find {results_mount_path} -type f \\( -name '*.json' -o -name '*.yaml' \\) "
        f"-print -exec cat {{}} \\; 2>/dev/null || true; "
        f"exit $rc"
    )

    # HF token env (from sentinel); injected into both harnesses, marked
    # optional so a non-gated model with no Secret still works.
    hf_secret_name = benchmark_config.get("_hf_token_secret")

    docs: list[dict] = []

    docs.append({
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": cm_name,
            "namespace": namespace,
        },
        "data": {
            config_filename: yaml.safe_dump(scrubbed, sort_keys=False),
        },
    })

    # Volume spec for results — PVC if named, emptyDir otherwise
    if pvc_name:
        results_volume = {
            "name": "results",
            "persistentVolumeClaim": {"claimName": pvc_name},
        }
    else:
        results_volume = {
            "name": "results",
            "emptyDir": {"sizeLimit": "5Gi"},
        }

    # Build the env list. RAYON_NUM_THREADS only relevant for inference-perf.
    env_list: list[dict] = []
    if harness_name == "inference-perf":
        env_list.append({"name": "RAYON_NUM_THREADS", "value": "4"})
    if hf_secret_name:
        env_list.append({
            "name": "HF_TOKEN",
            "valueFrom": {
                "secretKeyRef": {
                    "name": hf_secret_name,
                    "key": "HF_TOKEN",
                    "optional": True,
                },
            },
        })

    container: dict = {
        "name": "bench",
        "image": image,
        "command": ["sh", "-c"],
        "args": [wrapped_cmd],
        "volumeMounts": [
            {"name": "config", "mountPath": config_mount_path},
            # Results path differs by harness. See the per-harness layout block
            # above for the mount-over reasoning.
            {"name": "results", "mountPath": results_mount_path},
        ],
    }
    if env_list:
        container["env"] = env_list

    # Job spec
    docs.append({
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/component": "autoconfig-benchmark",
            },
        },
        "spec": {
            "backoffLimit": 0,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/component": "autoconfig-benchmark",
                        "job-name": job_name,
                    },
                },
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [container],
                    "volumes": [
                        {"name": "config", "configMap": {"name": cm_name}},
                        results_volume,
                    ],
                },
            },
        },
    })

    return "---\n".join(yaml.safe_dump(d, sort_keys=False) for d in docs)

"""Verify every URL in feature_docs.yaml is reachable.

The agent runs this at session start (or on demand). If any URLs return
non-200, surfaces the gap so the agent knows which docs it can't lean on
for recommendations.

Behavior:
    - HTTP HEAD per URL (fast, no body download)
    - Falls back to GET when HEAD returns 405 (some servers don't support HEAD)
    - Reports redirects (3xx) — usually fine, but flagged because the doc
      may have moved
    - Distinguishes 404 (missing — needs map update) from 5xx (transient,
      retry recommended) from network errors (cluster connectivity issue)
    - Concurrent checks via ThreadPoolExecutor — 30+ URLs in parallel

CLI:
    python3 verify_doc_map.py                       # check every URL
    python3 verify_doc_map.py --main-only           # only `main` field URLs (skip `secondary`)
    python3 verify_doc_map.py --json                # machine-readable output
    python3 verify_doc_map.py --quiet               # only print failures

Exit codes:
    0  all URLs OK (200 or 3xx)
    1  one or more URLs broken (404 / 5xx / network error)
    2  the feature_docs file itself is missing or malformed
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import yaml


_FEATURE_DOCS = Path(__file__).resolve().parent.parent / "feature_docs.yaml"

USER_AGENT = "llm-d-autoconfig/verify_doc_map/1.0"
HTTP_TIMEOUT_SECONDS = 15
MAX_WORKERS = 16  # concurrent HTTP checks


@dataclass
class CheckResult:
    url: str
    category: str
    key: str
    role: str  # "main" | "secondary"
    status: int  # HTTP status code, 0 for network error
    final_url: str = ""  # after redirect
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400

    @property
    def redirected(self) -> bool:
        return self.ok and self.final_url and self.final_url != self.url


def _check_url(url: str, category: str, key: str, role: str) -> CheckResult:
    """HEAD probe; fall back to GET on 405 or other HEAD-unfriendly responses.

    Follows redirects (urllib's default). Reports the final URL when redirected
    so the user can update the map.
    """
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(
                url,
                method=method,
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                return CheckResult(
                    url=url,
                    category=category,
                    key=key,
                    role=role,
                    status=resp.status,
                    final_url=resp.geturl(),
                )
        except urllib.error.HTTPError as e:
            # 405 → method not allowed; retry with GET
            if e.code == 405 and method == "HEAD":
                continue
            return CheckResult(
                url=url, category=category, key=key, role=role,
                status=e.code, error=str(e),
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            return CheckResult(
                url=url, category=category, key=key, role=role,
                status=0, error=str(e),
            )
    # Unreachable in practice (the loop only continues on 405-from-HEAD);
    # included to satisfy type checkers.
    return CheckResult(url=url, category=category, key=key, role=role, status=0,
                       error="unreachable code path")


def _load_doc_map() -> dict:
    if not _FEATURE_DOCS.exists():
        print(f"error: {_FEATURE_DOCS} not found", file=sys.stderr)
        sys.exit(2)
    try:
        with _FEATURE_DOCS.open() as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"error: {_FEATURE_DOCS} is malformed: {e}", file=sys.stderr)
        sys.exit(2)


def _iter_entries(doc: dict, main_only: bool):
    """Yield (category, key, role, url) for every URL in the map."""
    for cat, entries in doc.items():
        if cat == "meta" or not isinstance(entries, dict):
            continue
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            main = entry.get("main")
            if isinstance(main, str) and main.startswith("http"):
                yield (cat, key, "main", main)
            if not main_only:
                for url in entry.get("secondary", []) or []:
                    if isinstance(url, str) and url.startswith("http"):
                        yield (cat, key, "secondary", url)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify every URL in feature_docs.yaml.")
    parser.add_argument("--main-only", action="store_true",
                        help="Only check `main` field URLs; skip `secondary`.")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of pretty text.")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Only print failures (and the summary).")
    parser.add_argument("--max-workers", type=int, default=MAX_WORKERS,
                        help=f"Concurrent HTTP checks (default {MAX_WORKERS}).")
    args = parser.parse_args()

    doc = _load_doc_map()
    targets = list(_iter_entries(doc, args.main_only))
    if not targets:
        print("error: no URLs in feature_docs map", file=sys.stderr)
        return 2

    results: list[CheckResult] = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as ex:
        futures = {ex.submit(_check_url, url, cat, key, role): (cat, key, role, url)
                   for cat, key, role, url in targets}
        for fut in as_completed(futures):
            results.append(fut.result())

    # Sort by category then by key for stable reporting
    results.sort(key=lambda r: (r.category, r.key, r.role))

    failed = [r for r in results if not r.ok]
    redirected = [r for r in results if r.redirected]

    if args.json:
        print(json.dumps({
            "total": len(results),
            "ok": len(results) - len(failed),
            "failed": len(failed),
            "redirected": len(redirected),
            "results": [
                {
                    "category": r.category,
                    "key": r.key,
                    "role": r.role,
                    "url": r.url,
                    "status": r.status,
                    "final_url": r.final_url,
                    "error": r.error,
                }
                for r in results
            ],
        }, indent=2))
    else:
        if not args.quiet:
            for r in results:
                if r.ok and not r.redirected:
                    print(f"  ok    [{r.status}] {r.category}/{r.key} ({r.role})")
        if redirected:
            print()
            print("Redirected (consider updating the map):")
            for r in redirected:
                print(f"  → [{r.status}] {r.category}/{r.key} ({r.role})")
                print(f"      {r.url}")
                print(f"      → {r.final_url}")
        if failed:
            print()
            print("FAILED:")
            for r in failed:
                detail = f"HTTP {r.status}" if r.status else f"network: {r.error[:80]}"
                print(f"  ✗ [{detail}] {r.category}/{r.key} ({r.role})")
                print(f"      {r.url}")
        print()
        print(f"summary: {len(results)} total, {len(results) - len(failed)} ok, "
              f"{len(failed)} failed, {len(redirected)} redirected")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

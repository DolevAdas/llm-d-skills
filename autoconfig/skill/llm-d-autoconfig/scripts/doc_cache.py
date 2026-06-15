"""Doc cache for the autoconfig SKILL.

Fetches upstream documentation URLs (the ones listed in
`feature_docs.yaml`), stores them under `<skill-install-dir>/cache/docs/`,
and serves cached content on subsequent reads.

Cache invalidation strategies (any one triggers a re-fetch):
    1. The `meta.skill_version` field in `feature_docs.yaml` changed since
       the cached entry was written.
    2. The cache file is older than `MAX_AGE_HOURS` (default 168 = 7 days).
    3. The agent passes `force=True` (e.g., from a `--refresh-cache` CLI flag).
    4. The cache file is missing.

The cache is intentionally bundled inside the skill install dir so it's
self-contained per agent install (Gemini's copy and Claude Code's copy each
have their own cache; reinstalling clears it).

Filename format: `<sha256(URL)[:16]>__<skill_version>.md`. The skill-version
suffix means a version bump leaves stale files on disk (small cost) but
guarantees a clean re-fetch without us having to walk the cache and prune.

CLI usage:
    # `fetch` ensures one or more URLs are cached and prints LOCAL CACHE
    # PATHs on stdout, one per line, in input order. PREFER passing all
    # your URLs in ONE invocation rather than calling fetch N times — the
    # python-spawn + feature_docs.yaml-parse cost (~100-200ms) is paid
    # once per process, not once per URL.
    #
    # Multi-URL is the common case for Phase 2.5 (reading list of 5-8
    # URLs derived from feature flags):
    #     paths=$(python3 doc_cache.py fetch <url1> <url2> <url3> ...)
    #     # then Read each path in your file-read tool, indexed in input order
    #
    # Single-URL still works (back-compat):
    #     path=$(python3 doc_cache.py fetch <url>) && cat "$path"
    #
    # The cache (under <skill-install-dir>/cache/docs/) is the canonical
    # store — don't redirect to a parallel temp dir.
    python3 doc_cache.py fetch <url> [<url> ...]    # cache + print paths
    python3 doc_cache.py fetch --force <url> ...    # bypass cache hits; re-fetch all
    python3 doc_cache.py fetch --body <url>         # print body to stdout (single-URL only)
    python3 doc_cache.py warm                       # pre-fetch every URL in feature_docs.yaml's main fields
    python3 doc_cache.py warm --include-secondary
    python3 doc_cache.py status                     # report cache size + age + entries
    python3 doc_cache.py clear                      # wipe the cache directory

Library usage:
    from doc_cache import fetch
    text = fetch("https://github.com/llm-d/llm-d/blob/main/guides/optimized-baseline/README.md")

Returns the raw response body (UTF-8 text). For GitHub blob URLs, transparently
rewrites to raw.githubusercontent.com so the cache stores Markdown source
rather than HTML.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Paths + config
# ---------------------------------------------------------------------------

# This file lives at <skill-install-dir>/scripts/doc_cache.py.
# The skill install dir is the parent of `scripts/`; the cache lives next to
# scripts/ at <skill-install-dir>/cache/docs/.
_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILL_DIR = _SCRIPT_DIR.parent
_CACHE_DIR = _SKILL_DIR / "cache" / "docs"

_FEATURE_DOCS = _SKILL_DIR / "feature_docs.yaml"

MAX_AGE_HOURS = 168  # 7 days. Override per-call via fetch(..., max_age_hours=N).
USER_AGENT = "llm-d-autoconfig/doc_cache/1.0"
HTTP_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# URL normalization (GitHub blob → raw)
# ---------------------------------------------------------------------------


def _normalize_url(url: str) -> str:
    """Rewrite GitHub blob URLs to raw.githubusercontent.com.

    The cache wants markdown source, not GitHub's HTML rendering. Other URL
    forms pass through unchanged.

    Examples:
        https://github.com/<owner>/<repo>/blob/<ref>/<path>
        →  https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>
    """
    prefix = "https://github.com/"
    if not url.startswith(prefix):
        return url
    rest = url[len(prefix):]
    parts = rest.split("/")
    if len(parts) < 5 or parts[2] != "blob":
        # Not a blob URL — could be a tree/ URL or org/repo root. Leave it.
        return url
    owner, repo, _blob, ref, *path = parts
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{'/'.join(path)}"


# ---------------------------------------------------------------------------
# Cache key derivation
# ---------------------------------------------------------------------------


def _skill_version() -> str:
    """Read `meta.skill_version` from feature_docs.yaml. Used as part of the
    cache filename so version bumps cleanly invalidate."""
    try:
        with _FEATURE_DOCS.open() as f:
            doc = yaml.safe_load(f)
        return str(doc.get("meta", {}).get("skill_version", "unknown"))
    except (FileNotFoundError, yaml.YAMLError):
        return "unknown"


def _cache_path(url: str) -> Path:
    """Compute the on-disk cache path for a URL."""
    normalized = _normalize_url(url)
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:16]
    version = _skill_version().replace("/", "_")  # paranoid path-safety
    return _CACHE_DIR / f"{digest}__{version}.md"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


class FetchError(Exception):
    """Raised when a URL can't be fetched AND no cached fallback exists."""


def _http_get(url: str) -> str:
    """Plain HTTP GET. Returns response body as UTF-8 text. Raises on non-2xx."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        body = resp.read()
    return body.decode("utf-8", errors="replace")


def fetch(
    url: str,
    *,
    force: bool = False,
    max_age_hours: int = MAX_AGE_HOURS,
) -> str:
    """Fetch a doc URL, using cache when fresh.

    Cache hit conditions:
      - `force=False`
      - cache file exists at the version-stamped path
      - file mtime is within `max_age_hours`

    On any miss: HTTP GET, write to cache, return body.
    On HTTP failure: fall back to a stale cached entry if any exists; else
    raise `FetchError`.
    """
    normalized = _normalize_url(url)
    path = _cache_path(url)

    if not force and path.exists():
        age_seconds = time.time() - path.stat().st_mtime
        if age_seconds < max_age_hours * 3600:
            return path.read_text(encoding="utf-8")

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        body = _http_get(normalized)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        # Fall back to whatever's on disk for this URL key, even if stale or
        # under a different skill_version suffix. Last-resort: raise.
        for fallback in _CACHE_DIR.glob(f"{path.stem.split('__')[0]}__*.md"):
            return fallback.read_text(encoding="utf-8")
        raise FetchError(f"Failed to fetch {normalized}: {e}") from e

    path.write_text(body, encoding="utf-8")
    return body


# ---------------------------------------------------------------------------
# Doc-map iteration helpers
# ---------------------------------------------------------------------------


def _iter_urls(include_secondary: bool = True):
    """Walk feature_docs.yaml and yield every URL.

    Each yielded item is a tuple (category, key, role, url) where role is
    "main" or "secondary".
    """
    if not _FEATURE_DOCS.exists():
        return
    with _FEATURE_DOCS.open() as f:
        doc = yaml.safe_load(f)
    for cat, entries in doc.items():
        if cat == "meta" or not isinstance(entries, dict):
            continue
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            main = entry.get("main")
            if isinstance(main, str) and main.startswith("http"):
                yield (cat, key, "main", main)
            if include_secondary:
                for url in entry.get("secondary", []) or []:
                    if isinstance(url, str) and url.startswith("http"):
                        yield (cat, key, "secondary", url)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_fetch(args: argparse.Namespace) -> int:
    """Ensure each URL is cached. Prints LOCAL CACHE PATHs on stdout, one per
    line, in input order. Failures emit an empty line on stdout and an
    `error:` line on stderr so positional indexing still works for callers.

    Batch invocation amortizes the python-spawn + feature_docs.yaml-parse
    cost across N URLs (one process for the whole reading list instead of
    one per URL). The path-first default is preserved so agents read from
    the cache rather than redirecting fetch output to a parallel temp dir.

    --body mode is single-URL only: streaming N concatenated bodies to
    stdout would be impossible to disambiguate.
    """
    if args.body and len(args.url) > 1:
        print(
            "error: --body only supports a single URL (concatenating N "
            "doc bodies on stdout is ambiguous). Drop --body and read from "
            "the printed paths instead.",
            file=sys.stderr,
        )
        return 2

    overall_exit = 0
    for url in args.url:
        try:
            # fetch() always returns the body, but it also guarantees the cache
            # file is up to date on disk. We just print whichever the caller
            # asked for. The cache file is at _cache_path(url) regardless of
            # cache-hit vs network-fetch.
            fetch(url, force=args.force)
        except FetchError as e:
            print(f"error: {url}: {e}", file=sys.stderr)
            print("")  # placeholder so output index aligns with input index
            overall_exit = 1
            continue
        path = _cache_path(url)
        if args.body:
            sys.stdout.write(path.read_text(encoding="utf-8"))
        else:
            print(path)
    return overall_exit


def cmd_warm(args: argparse.Namespace) -> int:
    """Pre-fetch every URL in feature_docs.yaml. Useful at session start."""
    seen: set[str] = set()
    failed: list[tuple[str, str]] = []
    succeeded = 0
    for cat, key, role, url in _iter_urls(include_secondary=args.include_secondary):
        normalized = _normalize_url(url)
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            fetch(url, force=args.force)
            succeeded += 1
            if args.verbose:
                print(f"  ok   {cat}/{key} {role}: {url}")
        except FetchError as e:
            failed.append((url, str(e)))
            if args.verbose:
                print(f"  FAIL {cat}/{key} {role}: {url} ({e})", file=sys.stderr)

    print(f"warmed {succeeded} URLs; {len(failed)} failed; {len(seen)} unique total")
    if failed:
        print("\nFailures:", file=sys.stderr)
        for url, err in failed:
            print(f"  {url}: {err}", file=sys.stderr)
        return 1
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    if not _CACHE_DIR.exists():
        print(f"cache dir does not exist: {_CACHE_DIR}")
        return 0
    entries = list(_CACHE_DIR.glob("*.md"))
    total_bytes = sum(p.stat().st_size for p in entries)
    print(f"cache dir: {_CACHE_DIR}")
    print(f"  entries: {len(entries)}")
    print(f"  total size: {total_bytes:,} bytes ({total_bytes / 1024 / 1024:.2f} MiB)")
    if entries:
        oldest = min(entries, key=lambda p: p.stat().st_mtime)
        newest = max(entries, key=lambda p: p.stat().st_mtime)
        now = time.time()
        print(f"  oldest entry: {(now - oldest.stat().st_mtime) / 3600:.1f}h old ({oldest.name})")
        print(f"  newest entry: {(now - newest.stat().st_mtime) / 3600:.1f}h old ({newest.name})")
    print(f"  current skill_version: {_skill_version()}")
    return 0


def cmd_clear(_args: argparse.Namespace) -> int:
    if not _CACHE_DIR.exists():
        print("nothing to clear")
        return 0
    n = 0
    for p in _CACHE_DIR.glob("*.md"):
        p.unlink()
        n += 1
    print(f"cleared {n} cache entries from {_CACHE_DIR}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Doc cache for the autoconfig SKILL.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser(
        "fetch",
        help="Cache one or more URLs and print their LOCAL CACHE PATHs "
             "(one per line, in input order; default) or single-URL body (--body).",
    )
    p_fetch.add_argument(
        "url", nargs="+",
        help="One or more URLs. Multiple URLs in one invocation amortizes "
             "the python-spawn + feature_docs.yaml-parse cost — preferred over "
             "calling `fetch` N times for N URLs in your Phase 2.5 reading list.",
    )
    p_fetch.add_argument("--force", action="store_true", help="Bypass cache; always fetch.")
    p_fetch.add_argument(
        "--body", action="store_true",
        help="Stream the document body to stdout instead of printing the cache path. "
             "Single-URL only (N concatenated bodies are ambiguous). Default prints "
             "paths so callers read from the cache rather than redirecting into a "
             "parallel storage location.",
    )
    p_fetch.set_defaults(func=cmd_fetch)

    p_warm = sub.add_parser("warm", help="Pre-fetch every URL in feature_docs.yaml.")
    p_warm.add_argument("--include-secondary", action="store_true", help="Also fetch secondary URLs (default: only main).")
    p_warm.add_argument("--force", action="store_true", help="Bypass cache; re-fetch even hot entries.")
    p_warm.add_argument("--verbose", "-v", action="store_true", help="Print each URL as it's fetched.")
    p_warm.set_defaults(func=cmd_warm)

    p_status = sub.add_parser("status", help="Report cache contents + age.")
    p_status.set_defaults(func=cmd_status)

    p_clear = sub.add_parser("clear", help="Delete every cached entry.")
    p_clear.set_defaults(func=cmd_clear)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

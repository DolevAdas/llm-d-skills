"""Skill bundle invariants: scripts present, SKILL.md structure, feature_docs
lint, doc_cache CLI behavior. These tests protect the contract between the
skill bundle's on-disk layout and the SKILL.md navigation.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "skill" / "llm-d-autoconfig" / "scripts"))

SKILL_SCRIPTS_DIR = _REPO_ROOT / "skill" / "llm-d-autoconfig" / "scripts"
_SKILL_DIR = _REPO_ROOT / "skill" / "llm-d-autoconfig"
_SKILL_MD = _SKILL_DIR / "SKILL.md"
_REFERENCES_DIR = _SKILL_DIR / "references"
_FEATURE_DOCS = _SKILL_DIR / "feature_docs.yaml"

# Reference files SKILL.md must link to. Mirrors the table in SKILL.md's
# "How this skill is organized" section.
_EXPECTED_REFERENCES = [
    "phase-1-cluster-discovery.md",
    "phase-2-discovery-questionnaire.md",
    "phase-2-5-doc-driven-synthesis.md",
    "phase-3-recap.md",
    "phase-4-call-script.md",
    "phase-5-present-recommendation.md",
    "phase-6-deploy.md",
    "phase-7-benchmark.md",
    "pitfalls.md",
]


class BundledScriptsExistTest(unittest.TestCase):
    """Skill bundle must contain the canonical scripts. With the
    single-source-of-truth layout (skill/.../scripts/ is the only copy),
    drift is impossible — but we still check the files exist + aren't
    symlinks, because `cp -r` install on a fresh machine depends on real
    files being present.
    """

    _REQUIRED = ["autoconfig_poc.py", "benchmark.py"]

    def test_required_scripts_present(self) -> None:
        for name in self._REQUIRED:
            with self.subTest(file=name):
                self.assertTrue(
                    (SKILL_SCRIPTS_DIR / name).exists(),
                    f"missing canonical script: skill/llm-d-autoconfig/scripts/{name}",
                )

    def test_required_scripts_are_not_symlinks(self) -> None:
        for name in self._REQUIRED:
            with self.subTest(file=name):
                self.assertFalse(
                    (SKILL_SCRIPTS_DIR / name).is_symlink(),
                    f"{name} is a symlink. Symlinks break under cp -r without -L "
                    "and have caused install failures in the field. Use a real file.",
                )
class SkillStructureTest(unittest.TestCase):
    """SKILL.md is the entry point and navigator; references/ holds per-phase
    detail. These tests pin the structure so edits keep SKILL.md small and
    don't break the reference links.
    """

    def test_skill_md_under_500_lines(self) -> None:
        line_count = len(_SKILL_MD.read_text().splitlines())
        self.assertLess(
            line_count, 500,
            f"SKILL.md is {line_count} lines (limit 500). Move detail into "
            f"references/phase-*.md and link from the navigation table.",
        )

    def test_skill_md_has_frontmatter(self) -> None:
        text = _SKILL_MD.read_text()
        self.assertTrue(text.startswith("---\n"), "SKILL.md missing YAML frontmatter")
        for required_key in ("name:", "description:", "compatibility:"):
            self.assertIn(required_key, text.split("---", 2)[1])

    def test_skill_md_links_every_reference(self) -> None:
        text = _SKILL_MD.read_text()
        for ref in _EXPECTED_REFERENCES:
            with self.subTest(ref=ref):
                self.assertIn(
                    f"references/{ref}", text,
                    f"SKILL.md doesn't link to references/{ref}",
                )

    def test_every_reference_file_exists(self) -> None:
        for ref in _EXPECTED_REFERENCES:
            with self.subTest(ref=ref):
                self.assertTrue(
                    (_REFERENCES_DIR / ref).exists(),
                    f"missing reference file: references/{ref}",
                )

    def test_no_unexpected_reference_files(self) -> None:
        actual = {p.name for p in _REFERENCES_DIR.glob("*.md")}
        expected = set(_EXPECTED_REFERENCES)
        unexpected = actual - expected
        self.assertEqual(
            unexpected, set(),
            f"references/ has files not listed in SKILL.md's navigation: {unexpected}. "
            f"Either add them to the SKILL.md table or remove them.",
        )

    def test_skill_md_cites_phase_2_5_in_hard_rules(self) -> None:
        # The doc-driven recommendation rule lives in SKILL.md and references
        # Phase 2.5. Pin both so an edit can't quietly drop them.
        text = _SKILL_MD.read_text()
        self.assertIn("Phase 2.5", text, "SKILL.md does not reference Phase 2.5")
        self.assertIn(
            "doc-anchored", text,
            "Hard Rule about doc-anchored recommendations is missing from SKILL.md",
        )


class FeatureDocsLintTest(unittest.TestCase):
    """feature_docs.yaml is the URL map Phase 2.5 reads. These tests catch
    structural drift without making any network calls (network verification
    lives in scripts/verify_doc_map.py)."""

    @classmethod
    def setUpClass(cls) -> None:
        import yaml as yaml_mod
        cls.doc = yaml_mod.safe_load(_FEATURE_DOCS.read_text())

    def test_file_loads_as_yaml(self) -> None:
        self.assertIsInstance(self.doc, dict)

    def test_has_meta_block(self) -> None:
        self.assertIn("meta", self.doc)
        self.assertIn("skill_version", self.doc["meta"])

    def test_has_at_least_one_category(self) -> None:
        categories = [k for k in self.doc if k != "meta"]
        self.assertGreater(len(categories), 0, "feature_docs.yaml has no categories")

    def test_every_entry_has_required_fields(self) -> None:
        for category, entries in self.doc.items():
            if category == "meta":
                continue
            self.assertIsInstance(
                entries, dict,
                f"category {category} should be a dict of entries, got {type(entries).__name__}",
            )
            for name, entry in entries.items():
                with self.subTest(category=category, entry=name):
                    self.assertIsInstance(
                        entry, dict,
                        f"{category}.{name} should be a dict, got {type(entry).__name__}",
                    )
                    self.assertIn(
                        "main", entry,
                        f"{category}.{name} missing required 'main' URL",
                    )

    def test_every_url_uses_https(self) -> None:
        for category, entries in self.doc.items():
            if category == "meta":
                continue
            for name, entry in entries.items():
                for key in ("main",) + tuple(k for k in entry if k != "main"):
                    val = entry.get(key)
                    if isinstance(val, str) and val.startswith(("http://", "https://")):
                        with self.subTest(entry=f"{category}.{name}.{key}", url=val):
                            self.assertTrue(
                                val.startswith("https://"),
                                f"{category}.{name}.{key} uses http:// — should be https://",
                            )
                    elif isinstance(val, list):
                        for u in val:
                            if isinstance(u, str) and u.startswith(("http://", "https://")):
                                with self.subTest(entry=f"{category}.{name}.{key}", url=u):
                                    self.assertTrue(
                                        u.startswith("https://"),
                                        f"{category}.{name}.{key} uses http:// — should be https://",
                                    )

    def test_at_least_critical_features_present(self) -> None:
        # Phase 2.5's reading table requires these. Smoke test — if anyone
        # accidentally drops one, Phase 2.5 falls back silently to no doc.
        # Keys in feature_docs.yaml are snake_case (see the file).
        critical = [
            ("guides", "optimized_baseline"),
            ("guides", "pd_disaggregation"),
            ("guides", "predicted_latency_based_scheduling"),
            ("guides", "precise_prefix_cache_aware"),
        ]
        for category, name in critical:
            with self.subTest(entry=f"{category}.{name}"):
                self.assertIn(
                    category, self.doc,
                    f"feature_docs.yaml missing critical category: {category}",
                )
                self.assertIn(
                    name, self.doc[category],
                    f"feature_docs.yaml missing critical entry: {category}.{name}",
                )

class DocCacheBatchFetchTest(unittest.TestCase):
    """`doc_cache.py fetch` accepts N URLs in one invocation. Output is N
    cache paths on stdout (one per line, input order). Failures emit an
    empty placeholder line so positional indexing still works."""

    def _run(self, argv: list[str], stub_fetcher=None):
        """Invoke doc_cache.main with stub fetch() injected (no network)."""
        import io
        import sys as _sys
        import doc_cache as dc

        old_stdin, old_stdout, old_stderr = _sys.stdin, _sys.stdout, _sys.stderr
        old_fetch = dc.fetch
        if stub_fetcher is not None:
            dc.fetch = stub_fetcher
        try:
            _sys.stdin = io.StringIO("")
            _sys.stdout = io.StringIO()
            _sys.stderr = io.StringIO()
            code = dc.main(argv)
            return code, _sys.stdout.getvalue(), _sys.stderr.getvalue()
        finally:
            _sys.stdin, _sys.stdout, _sys.stderr = old_stdin, old_stdout, old_stderr
            dc.fetch = old_fetch

    def test_single_url_backward_compat(self) -> None:
        import doc_cache as dc
        def stub(url, *, force=False, max_age_hours=None):
            # Touch the cache file so _cache_path() returns a real path.
            p = dc._cache_path(url)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("cached body", encoding="utf-8")
            return "cached body"
        code, stdout, _ = self._run(["fetch", "https://example.com/one"], stub_fetcher=stub)
        self.assertEqual(code, 0)
        # Output is one path on its own line
        lines = stdout.strip().split("\n")
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith(".md"))

    def test_batch_multi_url_returns_paths_in_input_order(self) -> None:
        import doc_cache as dc
        urls = ["https://example.com/a", "https://example.com/b", "https://example.com/c"]
        def stub(url, *, force=False, max_age_hours=None):
            p = dc._cache_path(url)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"body of {url}", encoding="utf-8")
            return f"body of {url}"
        code, stdout, _ = self._run(["fetch"] + urls, stub_fetcher=stub)
        self.assertEqual(code, 0)
        lines = stdout.strip().split("\n")
        self.assertEqual(len(lines), 3)
        # Order matches input — verify by reading each path and matching the body
        from pathlib import Path
        for i, url in enumerate(urls):
            self.assertEqual(Path(lines[i]).read_text(), f"body of {url}")

    def test_batch_failure_emits_placeholder_and_nonzero_exit(self) -> None:
        import doc_cache as dc
        def stub(url, *, force=False, max_age_hours=None):
            if "fail" in url:
                raise dc.FetchError(f"404 on {url}")
            p = dc._cache_path(url)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("ok", encoding="utf-8")
            return "ok"
        urls = ["https://example.com/ok1", "https://example.com/fail-here", "https://example.com/ok2"]
        code, stdout, stderr = self._run(["fetch"] + urls, stub_fetcher=stub)
        self.assertEqual(code, 1)
        lines = stdout.split("\n")
        # Three lines + final newline → 4 entries. Middle line is empty (placeholder).
        # First and third are real paths; the failure preserves positional indexing.
        # Filter trailing empty newline from split.
        if lines and lines[-1] == "":
            lines = lines[:-1]
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].endswith(".md"))
        self.assertEqual(lines[1], "")  # placeholder for the failed URL
        self.assertTrue(lines[2].endswith(".md"))
        self.assertIn("fail-here", stderr)
        self.assertIn("404", stderr)

    def test_body_mode_rejects_multi_url(self) -> None:
        code, _, stderr = self._run(
            ["fetch", "--body", "https://example.com/a", "https://example.com/b"],
        )
        self.assertEqual(code, 2)
        self.assertIn("single URL", stderr)




if __name__ == "__main__":
    unittest.main()

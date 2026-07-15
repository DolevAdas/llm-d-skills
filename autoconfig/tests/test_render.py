"""Helm values rendering — llm-d-router chart shape with the EPP config inlined."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "skill" / "llm-d-autoconfig" / "scripts"))

from autoconfig_poc import (  # noqa: E402
    Input,
    Topology,
    build_output,
    render_helm_values,
)


class HelmValuesRenderTest(unittest.TestCase):
    """render_helm_values produces the llm-d-router chart's expected values.yaml
    shape (EPP config under router.epp.pluginsCustomConfig), so deploy scripts
    don't need shell-level $() interpolation.
    """

    def _build(self) -> str:
        inp = Input(
            model="Qwen/Qwen3-32B",
            topology=Topology(mode="agg", replicas=8, tp=2),
        )
        return render_helm_values(build_output(inp))

    def test_renders_expected_top_level_structure(self) -> None:
        rendered = self._build()
        self.assertIn("router:", rendered)
        self.assertIn("  epp:", rendered)
        self.assertIn("    pluginsConfigFile: epp-config.yaml", rendered)
        self.assertIn("    pluginsCustomConfig:", rendered)
        self.assertIn("      epp-config.yaml: |", rendered)

    def test_epp_config_is_a_block_scalar(self) -> None:
        import yaml as yaml_mod
        parsed = yaml_mod.safe_load(self._build())
        epp_str = parsed["router"]["epp"]["pluginsCustomConfig"]["epp-config.yaml"]
        # It's a string (block scalar), and it parses as the EPP config.
        self.assertIsInstance(epp_str, str)
        self.assertIn("apiVersion: llm-d.ai/v1alpha1", epp_str)
        self.assertIn("kind: EndpointPickerConfig", epp_str)

    def test_round_trips_as_valid_yaml(self) -> None:
        import yaml as yaml_mod
        rendered = self._build()
        parsed = yaml_mod.safe_load(rendered)
        self.assertIn("router", parsed)
        self.assertIn("pluginsConfigFile", parsed["router"]["epp"])
        # The nested epp config string parses as YAML in turn
        epp_yaml_str = parsed["router"]["epp"]["pluginsCustomConfig"]["epp-config.yaml"]
        epp_parsed = yaml_mod.safe_load(epp_yaml_str)
        self.assertEqual(epp_parsed["kind"], "EndpointPickerConfig")
        self.assertIn("plugins", epp_parsed)




if __name__ == "__main__":
    unittest.main()

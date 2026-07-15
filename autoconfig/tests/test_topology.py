"""Topology validation: agg vs PD required fields, PD-specific quartet."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "skill" / "llm-d-autoconfig" / "scripts"))

from autoconfig_poc import (  # noqa: E402
    Input,
    SLO,
    Topology,
    Workload,
    build_output,
    parse_input,
)

EXAMPLES = _REPO_ROOT / "examples"


class TopologyTest(unittest.TestCase):
    """Agg + PD validation: required fields differ per mode; PD has its own
    required quartet plus pd_transport."""

    def test_disagg_requires_pd_fields(self) -> None:
        """mode=disagg without prefill_replicas/prefill_tp/etc. → clean error."""
        inp_dict = json.loads((EXAMPLES / "input-balanced-chat.json").read_text())
        inp_dict["topology"] = {"mode": "disagg"}  # nothing else
        with self.assertRaises(ValueError) as cm:
            parse_input(json.dumps(inp_dict))
        self.assertIn("prefill_replicas", str(cm.exception))

    def test_disagg_requires_pd_transport(self) -> None:
        """mode=disagg with all PD fields but no pd_transport → clean error."""
        inp_dict = json.loads((EXAMPLES / "input-balanced-chat.json").read_text())
        inp_dict["topology"] = {
            "mode": "disagg",
            "prefill_replicas": 8, "prefill_tp": 1,
            "decode_replicas": 2, "decode_tp": 4,
        }
        with self.assertRaises(ValueError) as cm:
            parse_input(json.dumps(inp_dict))
        self.assertIn("pd_transport", str(cm.exception))

    def test_zero_replicas_rejected(self) -> None:
        inp_dict = json.loads((EXAMPLES / "input-balanced-chat.json").read_text())
        inp_dict["topology"]["replicas"] = 0
        with self.assertRaises(ValueError) as cm:
            parse_input(json.dumps(inp_dict))
        self.assertIn("replicas", str(cm.exception))


class PDTopologyTest(unittest.TestCase):
    """PD plugin set, two-profile emission, per-feature compatibility, and
    transport-specific warnings."""

    def _pd_input(self, **overrides: object) -> Input:
        topology = overrides.pop("topology", None) or Topology(
            mode="disagg",
            prefill_replicas=8, prefill_tp=1,
            decode_replicas=2, decode_tp=4,
            pd_transport="rdma",
        )
        return Input(model="openai/gpt-oss-120b", topology=topology, **overrides)

    def test_pd_emits_two_profiles(self) -> None:
        out = build_output(self._pd_input())
        cfg = out.decisions["epp"]["endpoint_picker_config"]
        profiles = {p["name"]: p for p in cfg["schedulingProfiles"]}
        self.assertEqual(set(profiles.keys()), {"prefill", "decode"})

    def test_pd_canonical_plugin_set(self) -> None:
        """All 10 canonical PD plugins from pd-disaggregation.values.yaml present."""
        out = build_output(self._pd_input())
        plugin_types = {p["type"] for p in out.decisions["epp"]["endpoint_picker_config"]["plugins"]}
        for required in [
            "disagg-headers-handler", "always-disagg-pd-decider",
            "disagg-profile-handler", "prefill-filter", "decode-filter",
            "prefix-cache-scorer", "queue-scorer", "kv-cache-utilization-scorer",
            "active-request-scorer", "max-score-picker",
        ]:
            self.assertIn(required, plugin_types)

    def test_pd_disagg_profile_handler_links_decider(self) -> None:
        """disagg-profile-handler must reference always-disagg-pd-decider by name."""
        out = build_output(self._pd_input())
        plugins = out.decisions["epp"]["endpoint_picker_config"]["plugins"]
        handler = next(p for p in plugins if p["type"] == "disagg-profile-handler")
        self.assertEqual(handler["parameters"]["deciderPluginName"], "always-disagg-pd-decider")

    def test_pd_prefill_profile_has_prefill_filter_only(self) -> None:
        out = build_output(self._pd_input())
        prefill = next(p for p in out.decisions["epp"]["endpoint_picker_config"]["schedulingProfiles"] if p["name"] == "prefill")
        refs = {p["pluginRef"] for p in prefill["plugins"]}
        self.assertIn("prefill-filter", refs)
        self.assertNotIn("decode-filter", refs)

    def test_pd_decode_profile_has_decode_filter_only(self) -> None:
        out = build_output(self._pd_input())
        decode = next(p for p in out.decisions["epp"]["endpoint_picker_config"]["schedulingProfiles"] if p["name"] == "decode")
        refs = {p["pluginRef"] for p in decode["plugins"]}
        self.assertIn("decode-filter", refs)
        self.assertNotIn("prefill-filter", refs)

    def test_pd_profile_weights_match_canonical(self) -> None:
        """Weights from pd-disaggregation/router/pd-disaggregation.values.yaml verbatim."""
        out = build_output(self._pd_input())
        profiles = {p["name"]: p for p in out.decisions["epp"]["endpoint_picker_config"]["schedulingProfiles"]}
        prefill_w = {p["pluginRef"]: p.get("weight") for p in profiles["prefill"]["plugins"]}
        decode_w = {p["pluginRef"]: p.get("weight") for p in profiles["decode"]["plugins"]}
        self.assertEqual(prefill_w.get("prefix-cache-scorer"), 3)
        self.assertEqual(prefill_w.get("queue-scorer"), 2)
        self.assertEqual(prefill_w.get("kv-cache-utilization-scorer"), 2)
        self.assertEqual(decode_w.get("active-request-scorer"), 2)
        self.assertEqual(decode_w.get("prefix-cache-scorer"), 3)

    def test_pd_tcp_transport_emits_perf_warning(self) -> None:
        out = build_output(self._pd_input(topology=Topology(
            mode="disagg",
            prefill_replicas=8, prefill_tp=1,
            decode_replicas=2, decode_tp=4,
            pd_transport="tcp",
        )))
        joined = " ".join(out.warnings).lower()
        self.assertIn("tcp", joined)
        self.assertIn("rdma", joined)

    def test_pd_rdma_transport_no_perf_warning(self) -> None:
        out = build_output(self._pd_input())  # rdma
        joined = " ".join(out.warnings).lower()
        self.assertNotIn("tcp fallback", joined)

    def test_pd_keeps_latency_predictor_with_role_label(self) -> None:
        """latency-predictor + PD: producer kept, endpointRoleLabel auto-set."""
        from autoconfig_poc import Features
        out = build_output(self._pd_input(features=Features(enable_latency_predictor=True)))
        plugins = out.decisions["epp"]["endpoint_picker_config"]["plugins"]
        producer = next(p for p in plugins if p["type"] == "predicted-latency-producer")
        self.assertEqual(producer["parameters"]["endpointRoleLabel"], "llm-d.ai/role")

    def test_pd_drops_strict_loose_affinity_filters(self) -> None:
        """Under PD + latency-predictor, the affinity filters are NOT emitted."""
        from autoconfig_poc import Features
        out = build_output(self._pd_input(features=Features(enable_latency_predictor=True)))
        all_names = {p.get("name") for p in out.decisions["epp"]["endpoint_picker_config"]["plugins"]}
        self.assertNotIn("strict-affinity-filter", all_names)
        self.assertNotIn("loose-affinity-filter", all_names)

    def test_pd_recommendation_overrides_canonical_default(self) -> None:
        # The agent (SKILL Phase 2) decides which plugins are compatible with
        # PD by reading docs. The script just renders what the agent supplies.
        # This test verifies that agent-supplied recommendation.plugins
        # OVERRIDES the canonical PD default cleanly — no script-side
        # filtering, no warnings injected.
        from autoconfig_poc import Recommendation
        custom_names = [
            "disagg-headers-handler", "always-disagg-pd-decider",
            "disagg-profile-handler", "prefill-filter", "decode-filter",
            "prefix-cache-scorer", "queue-scorer",
            "context-length-aware",  # agent decided to add this
            "max-score-picker",
        ]
        out = build_output(self._pd_input(
            recommendation=Recommendation(
                plugins=[{"type": n} for n in custom_names],
                cited_sources=["https://example.com/decision"],
                summary="Custom PD set including context-length-aware",
            ),
        ))
        plugin_types = [p["type"] for p in out.decisions["epp"]["endpoint_picker_config"]["plugins"]]
        self.assertEqual(plugin_types, custom_names)

    def test_pd_total_replicas_drives_qps_estimate(self) -> None:
        """benchmark sla_validation rates use prefill+decode for pod count."""
        from autoconfig_poc import SLO
        out = build_output(self._pd_input(slo=SLO(ttft_ms=800, tpot_ms=25)))
        sla = out.decisions["benchmark"]["config"]["workload"]["sla_validation"]
        # 8 prefill + 2 decode = 10 total; the rate-derivation string must
        # include "10 replicas", not "2" or "8" alone.
        self.assertIn("10 replicas", sla["_rate_derivation"])




if __name__ == "__main__":
    unittest.main()

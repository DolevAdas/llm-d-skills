"""Phase B feature flags: schema validation, chart toggles, precise-prefix guard."""

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
    output_to_dict,
    parse_input,
    render_helm_values,
)

EXAMPLES = _REPO_ROOT / "examples"


class PhaseBFeaturesSchemaTest(unittest.TestCase):
    """Phase B added new Features + WorkloadTraits flags (tiered_cache,
    flow_control, wide_ep, inference_objective, model_rewrite, autoscaler,
    serving_pattern, multimodal). These are signals to the agent's SKILL
    Phase 2.5 doc reads — the script renders an advisory warning per flag
    and includes them in input_hash for reproducibility, but does NOT
    auto-add plugins (that's the agent's job via recommendation).
    """

    def _build(self, **features_overrides) -> dict:
        from autoconfig_poc import Features
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(**features_overrides),
        )
        return output_to_dict(build_output(inp))

    def test_all_new_flags_accept_defaults(self) -> None:
        from autoconfig_poc import Features
        # All-default Features should validate cleanly and produce no advisory.
        out = self._build()
        joined = " ".join(out["warnings"])
        for forbidden in ("autoscaler", "tiered_cache", "wide_ep",
                          "flow_control", "batch", "async"):
            self.assertNotIn(
                f"enable_{forbidden}: ", joined,
                f"default-Features unexpectedly emitted {forbidden} advisory",
            )

    def test_invalid_autoscaler_rejected(self) -> None:
        inp_dict = json.loads((EXAMPLES / "input-balanced-chat.json").read_text())
        inp_dict["features"] = {"autoscaler": "keda"}  # not in ALLOWED_AUTOSCALER
        with self.assertRaises(ValueError) as cm:
            parse_input(json.dumps(inp_dict))
        self.assertIn("autoscaler", str(cm.exception))

    def test_invalid_serving_pattern_rejected(self) -> None:
        inp_dict = json.loads((EXAMPLES / "input-balanced-chat.json").read_text())
        inp_dict["features"] = {"serving_pattern": "fire-and-forget"}
        with self.assertRaises(ValueError) as cm:
            parse_input(json.dumps(inp_dict))
        self.assertIn("serving_pattern", str(cm.exception))

    def test_wva_emits_advisory(self) -> None:
        out = self._build(autoscaler="wva")
        joined = " ".join(out["warnings"])
        self.assertIn("autoscaler=wva", joined)
        self.assertIn("VariantAutoscaling", joined)
        # Points the agent at the doc map
        self.assertIn("workload_autoscaling_wva", joined)

    def test_hpa_emits_advisory(self) -> None:
        out = self._build(autoscaler="hpa")
        joined = " ".join(out["warnings"])
        self.assertIn("autoscaler=hpa", joined)
        self.assertIn("workload_autoscaling_hpa", joined)

    def test_tiered_cache_emits_advisory(self) -> None:
        out = self._build(enable_tiered_cache=True)
        joined = " ".join(out["warnings"])
        self.assertIn("enable_tiered_cache", joined)
        self.assertIn("tiered_prefix_cache", joined)

    def test_wide_ep_emits_advisory(self) -> None:
        out = self._build(enable_wide_ep=True)
        joined = " ".join(out["warnings"])
        self.assertIn("enable_wide_ep", joined)
        self.assertIn("LeaderWorkerSet", joined)

    def test_flow_control_emits_advisory(self) -> None:
        out = self._build(enable_flow_control=True)
        joined = " ".join(out["warnings"])
        self.assertIn("enable_flow_control", joined)
        self.assertIn("flow_control", joined)

    def test_batch_pattern_emits_advisory(self) -> None:
        out = self._build(serving_pattern="batch")
        joined = " ".join(out["warnings"])
        self.assertIn("serving_pattern=batch", joined)
        self.assertIn("batch_gateway", joined)

    def test_async_pattern_emits_advisory(self) -> None:
        out = self._build(serving_pattern="async")
        joined = " ".join(out["warnings"])
        self.assertIn("serving_pattern=async", joined)
        self.assertIn("asynchronous_processing", joined)

    def test_flag_changes_input_hash(self) -> None:
        from autoconfig_poc import Features
        base = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
        )
        flipped = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(enable_tiered_cache=True),
        )
        h1 = output_to_dict(build_output(base))["input_hash"]
        h2 = output_to_dict(build_output(flipped))["input_hash"]
        self.assertNotEqual(
            h1, h2,
            "enable_tiered_cache didn't change input_hash — flag isn't being hashed",
        )

    def test_multimodal_workload_trait_accepts(self) -> None:
        from autoconfig_poc import WorkloadTraits
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            workload_traits=WorkloadTraits(multimodal=True),
        )
        # Just needs to validate + render without error
        out = build_output(inp)
        self.assertIn("decisions", output_to_dict(out))


class PhaseBChartTogglesTest(unittest.TestCase):
    """Phase B helm-values rendering: chart toggle booleans (e.g.
    router.latencyPredictor.enabled) flow from input.features into
    decisions.epp.chart_toggles and then into the rendered values.yaml
    fragment, nested by their dotted key (e.g. under router.* or top-level).
    """

    def _render(self, **features_overrides) -> tuple[dict, str]:
        from autoconfig_poc import Features
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(**features_overrides),
        )
        out = build_output(inp)
        return output_to_dict(out)["decisions"]["epp"], render_helm_values(out)

    def test_no_toggles_when_features_default(self) -> None:
        epp, helm = self._render()
        self.assertEqual(epp["chart_toggles"], {})
        # No toggles → the only thing under router.epp is our EPP config.
        import yaml as yaml_mod
        parsed = yaml_mod.safe_load(helm)
        self.assertEqual(
            sorted(parsed["router"]["epp"].keys()),
            ["pluginsConfigFile", "pluginsCustomConfig"],
        )

    def test_latency_predictor_emits_chart_toggle(self) -> None:
        epp, helm = self._render(enable_latency_predictor=True)
        # chart_toggles keys carry the FULL dotted path from the router chart's
        # top-level values namespace. latencyPredictor lives under router;
        # httpRoute / provider live at the top.
        self.assertEqual(
            epp["chart_toggles"],
            {"router.latencyPredictor.enabled": True},
        )
        # Helm fragment puts latencyPredictor under router.
        import yaml as yaml_mod
        parsed = yaml_mod.safe_load(helm)
        self.assertIn("latencyPredictor", parsed["router"])
        self.assertEqual(parsed["router"]["latencyPredictor"]["enabled"], True)

    def test_dotted_toggle_keys_nest_properly(self) -> None:
        # render_helm_values turns dotted keys into nested maps. Verify via
        # an injected toggle (simulating future Phase B / Phase C additions).
        from autoconfig_poc import Output
        out = Output(
            input_hash="dummy",
            decisions={
                "workload_class": "balanced-conversational",
                "epp": {
                    "chart": "standalone",
                    "image_tag": "main",
                    "endpoint_picker_config": {
                        "apiVersion": "llm-d.ai/v1alpha1",
                        "kind": "EndpointPickerConfig",
                        "plugins": [],
                        "schedulingProfiles": [],
                    },
                    "chart_toggles": {
                        "router.latencyPredictor.enabled": True,
                        "httpRoute.create": True,
                        "httpRoute.inferenceGatewayName": "llm-d-inference-gateway",
                    },
                },
                "benchmark": {"harness": "guidellm", "harness_image": "x", "config": {}},
                "context": {"namespace": "x", "release_name": "x"},
            },
            rationale=[],
            parameters=[],
        )
        helm = render_helm_values(out)
        import yaml as yaml_mod
        parsed = yaml_mod.safe_load(helm)
        self.assertEqual(parsed["router"]["latencyPredictor"]["enabled"], True)
        self.assertEqual(
            parsed["httpRoute"],
            {"create": True, "inferenceGatewayName": "llm-d-inference-gateway"},
        )


class PreciseFixGuardTest(unittest.TestCase):
    """enable_precise_prefix_cache is an advisory flag — the script doesn't
    add the scorer, but it WARNS when the flag is on and the agent forgot
    to put precise-prefix-cache-scorer in recommendation.plugins.
    """

    def test_flag_on_without_plugin_warns(self) -> None:
        from autoconfig_poc import Features
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(enable_precise_prefix_cache=True),
        )
        joined = " ".join(output_to_dict(build_output(inp))["warnings"])
        self.assertIn("enable_precise_prefix_cache=true", joined)
        self.assertIn("precise-prefix-cache-scorer", joined)

    def test_flag_on_without_correctness_warns(self) -> None:
        from autoconfig_poc import Features, Recommendation
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(enable_precise_prefix_cache=True),
            recommendation=Recommendation(plugins=[{"type": "precise-prefix-cache-scorer"}]),
        )
        joined = " ".join(output_to_dict(build_output(inp))["warnings"])
        self.assertIn("vllm_block_size", joined)
        self.assertIn("vllm_hash_seed", joined)

    def test_flag_on_with_plugin_and_correctness_no_warn(self) -> None:
        from autoconfig_poc import Correctness, Features, Recommendation
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(enable_precise_prefix_cache=True),
            recommendation=Recommendation(plugins=[{"type": "precise-prefix-cache-scorer"}]),
            correctness=Correctness(vllm_block_size=64, vllm_hash_seed="42"),
        )
        joined = " ".join(output_to_dict(build_output(inp))["warnings"])
        self.assertNotIn("enable_precise_prefix_cache=true", joined)
        self.assertNotIn("requires correctness.vllm_block_size", joined)




if __name__ == "__main__":
    unittest.main()

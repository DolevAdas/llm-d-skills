"""Fixture diffs + workload classifier + algorithm invariants.

Covers FixtureDiffTest (the 4 canonical input/output pairs), the
optimized-baseline parity guard, the classifier, null-tolerant inputs,
max-prefix-tokens math, autotune fallback, latency-predictor scaffolding,
correctness questions, and determinism.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "skill" / "llm-d-autoconfig" / "scripts"))

from autoconfig_poc import (  # noqa: E402
    Features,
    Input,
    Runtime,
    SLO,
    Topology,
    Workload,
    build_output,
    classify_workload,
    output_to_dict,
    parse_input,
)

EXAMPLES = _REPO_ROOT / "examples"


def _load_and_run(input_name: str, output_name: str) -> tuple[dict, dict]:
    """Returns (expected_output_dict, actual_output_dict)."""
    inp = parse_input((EXAMPLES / input_name).read_text())
    expected = json.loads((EXAMPLES / output_name).read_text())
    actual = output_to_dict(build_output(inp))
    return expected, actual


class RecommendationPluginFormTest(unittest.TestCase):
    """recommendation.plugins entries are objects with a "type"; malformed
    entries are rejected. Regression for the `TypeError: unhashable type:
    'dict'` crash."""

    def _run(self, plugins) -> dict:
        return output_to_dict(build_output(Input(
            model="m",
            topology=Topology(mode="agg", replicas=2, tp=1),
            recommendation=__import__("autoconfig_poc").Recommendation(plugins=plugins),
        )))

    def _plugin_types(self, out: dict) -> list[str]:
        return [p["type"] for p in out["decisions"]["epp"]["endpoint_picker_config"]["plugins"]]

    def test_dict_form_works(self) -> None:
        out = self._run([{"type": "queue-scorer"}, {"type": "kv-cache-utilization-scorer"}])
        self.assertEqual(self._plugin_types(out), ["queue-scorer", "kv-cache-utilization-scorer"])

    def test_inline_parameters_honored(self) -> None:
        out = self._run([{"type": "queue-scorer", "parameters": {"foo": "bar"}}])
        entry = next(p for p in out["decisions"]["epp"]["endpoint_picker_config"]["plugins"]
                     if p["type"] == "queue-scorer")
        self.assertEqual(entry["parameters"]["foo"], "bar")

    def test_named_instance_keys_on_name(self) -> None:
        # A named instance (type + name) is identified internally by its name,
        # so an inline parameters block attaches to the named instance.
        out = self._run([{
            "type": "prefix-cache-affinity-filter",
            "name": "strict-affinity-filter",
            "parameters": {"affinityThreshold": 0.99},
        }])
        entry = next(p for p in out["decisions"]["epp"]["endpoint_picker_config"]["plugins"]
                     if p.get("name") == "strict-affinity-filter")
        self.assertEqual(entry["type"], "prefix-cache-affinity-filter")
        self.assertEqual(entry["parameters"]["affinityThreshold"], 0.99)

    def test_malformed_entries_rejected(self) -> None:
        for bad in ("queue-scorer", 123, None, ["queue-scorer"], {"parameters": {}}):
            with self.subTest(entry=bad):
                with self.assertRaises(ValueError) as cm:
                    self._run([bad])
                msg = str(cm.exception)
                self.assertIn("recommendation.plugins", msg)
                self.assertIn('{"type": "queue-scorer"}', msg)

    def test_dict_form_via_parse_input(self) -> None:
        # End-to-end through parse_input (the path the agent actually hits).
        raw = json.dumps({
            "model": "m",
            "topology": {"mode": "agg", "replicas": 2, "tp": 1},
            "recommendation": {"plugins": [{"type": "queue-scorer"}]},
        })
        out = output_to_dict(build_output(parse_input(raw)))
        self.assertEqual(self._plugin_types(out), ["queue-scorer"])


class FixtureDiffTest(unittest.TestCase):
    """Each canonical input produces the committed expected output."""

    def test_balanced_chat(self) -> None:
        expected, actual = _load_and_run(
            "input-balanced-chat.json", "output-balanced-chat.json"
        )
        self.assertEqual(actual, expected)

    def test_rag_style(self) -> None:
        expected, actual = _load_and_run(
            "input-rag-style.json", "output-rag-style.json"
        )
        self.assertEqual(actual, expected)

    def test_latency_tight(self) -> None:
        expected, actual = _load_and_run(
            "input-latency-tight.json", "output-latency-tight.json"
        )
        self.assertEqual(actual, expected)

    def test_pd_gateway_features(self) -> None:
        """Phase C regression: PD topology + gateway deploy + WVA autoscaler
        + InferenceObjective + latency-predictor. Exercises every code path
        the previous three fixtures skipped."""
        expected, actual = _load_and_run(
            "input-pd-gateway-features.json", "output-pd-gateway-features.json"
        )
        self.assertEqual(actual, expected)


class OptimizedBaselineParityTest(unittest.TestCase):
    """The balanced-conversational case must reproduce the published optimized-baseline values."""

    def test_weights_match_optimized_baseline_yaml(self) -> None:
        inp = parse_input((EXAMPLES / "input-balanced-chat.json").read_text())
        out = build_output(inp)
        profile = out.decisions["epp"]["endpoint_picker_config"]["schedulingProfiles"][0]
        weights = {p["pluginRef"]: p["weight"] for p in profile["plugins"] if "weight" in p}
        # Published in llm-d/guides/optimized-baseline/router/optimized-baseline.values.yaml
        # — schedulingProfiles[0].plugins[].weight (verbatim).
        self.assertEqual(weights, {
            "queue-scorer": 2,
            "kv-cache-utilization-scorer": 2,
            "prefix-cache-scorer": 3,
            "no-hit-lru-scorer": 2,
        })

    def test_optimized_baseline_plugin_set(self) -> None:
        inp = parse_input((EXAMPLES / "input-balanced-chat.json").read_text())
        out = build_output(inp)
        plugin_types = [
            p["type"]
            for p in out.decisions["epp"]["endpoint_picker_config"]["plugins"]
        ]
        # Verbatim from optimized-baseline.values.yaml — four scorers, in this
        # order. max-score-picker / single-profile-handler are loaded
        # implicitly by the chart's defaults (not listed in plugins[]).
        self.assertEqual(plugin_types, [
            "queue-scorer",
            "kv-cache-utilization-scorer",
            "prefix-cache-scorer",
            "no-hit-lru-scorer",
        ])


class ClassifierTest(unittest.TestCase):
    """Workload classifier picks the right class label from input signals.

    NOTE: classify_workload now returns benchmark-rate hint labels only —
    it does NOT drive plugin/weight selection (agent does that via doc reads).
    Labels: balanced-conversational | high-prefix-share | latency-tight.
    """

    def test_high_prefix_share_labeled(self) -> None:
        wl = Workload(isl=4000, osl=300, prefix_share="high")
        self.assertEqual(classify_workload(wl, SLO()), "high-prefix-share")

    def test_medium_prefix_share_also_labeled_high(self) -> None:
        wl = Workload(prefix_share="medium")
        self.assertEqual(classify_workload(wl, SLO()), "high-prefix-share")

    def test_tight_ttft_labeled_latency_tight(self) -> None:
        wl = Workload()
        self.assertEqual(
            classify_workload(wl, SLO(ttft_ms=200)), "latency-tight"
        )

    def test_default_is_balanced(self) -> None:
        self.assertEqual(
            classify_workload(Workload(), SLO()), "balanced-conversational"
        )


class NullTolerantInputTest(unittest.TestCase):
    """Algorithm proceeds with missing optional inputs; no fabrication."""

    def test_no_sla_no_workload_succeeds(self) -> None:
        # Pure minimum: model + topology only (the Vertex case)
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=4, tp=2),
        )
        out = build_output(inp)
        self.assertEqual(out.decisions["workload_class"], "balanced-conversational")
        # No SLA + no workload → warnings about plugin defaults
        self.assertGreater(len(out.warnings), 0)
        # Plugin set is still the optimized-baseline floor
        plugin_types = [
            p["type"]
            for p in out.decisions["epp"]["endpoint_picker_config"]["plugins"]
        ]
        self.assertIn("queue-scorer", plugin_types)
        self.assertIn("prefix-cache-scorer", plugin_types)

    def test_ttft_weight_falls_to_default_without_inputs(self) -> None:
        # SLO target present (so latency-scorer is included via the canonical
        # SLO-aware plugin set) but TPOT + OSL missing → ttftWeight derivation
        # falls back to the upstream default.
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(enable_latency_predictor=True),
            slo=SLO(ttft_ms=800),
        )
        out = build_output(inp)
        ttft_param = next(
            p for p in out.parameters if p["name"] == "latency-scorer.ttftWeight"
        )
        self.assertEqual(ttft_param["value"], 0.8)
        self.assertEqual(ttft_param["tier"], "T3")
        self.assertIn("upstream default", ttft_param["rationale"])

    def test_ttft_weight_computed_when_inputs_present(self) -> None:
        # SLA TTFT 800, TPOT 25, OSL 500 → 800 / (800 + 25*499) = 0.060 → clamped to 0.10
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            workload=Workload(osl=500),
            slo=SLO(ttft_ms=800, tpot_ms=25),
            features=Features(enable_latency_predictor=True),
        )
        out = build_output(inp)
        ttft_param = next(
            p for p in out.parameters if p["name"] == "latency-scorer.ttftWeight"
        )
        self.assertEqual(ttft_param["tier"], "T1")
        self.assertEqual(ttft_param["value"], 0.1)  # clamped to floor


class MaxPrefixTokensTest(unittest.TestCase):
    """Basic prefix-cache-scorer's maxPrefixTokensToMatch is set from context length."""

    def test_emitted_when_context_length_known(self) -> None:
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            model_context_length=8192,
        )
        out = build_output(inp)
        scorer = next(
            p for p in out.decisions["epp"]["endpoint_picker_config"]["plugins"]
            if p["type"] == "prefix-cache-scorer"
        )
        self.assertEqual(scorer["parameters"]["maxPrefixTokensToMatch"], 8192)
        names = [p["name"] for p in out.parameters]
        self.assertIn("prefix-cache-scorer.maxPrefixTokensToMatch", names)

    def test_omitted_when_context_length_missing(self) -> None:
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            # model_context_length intentionally omitted
        )
        out = build_output(inp)
        scorer = next(
            p for p in out.decisions["epp"]["endpoint_picker_config"]["plugins"]
            if p["type"] == "prefix-cache-scorer"
        )
        # autoTune is always present, but maxPrefixTokensToMatch should NOT be
        self.assertNotIn("maxPrefixTokensToMatch", scorer.get("parameters", {}))
        self.assertEqual(scorer["parameters"], {"autoTune": True})


class AutoTuneFallbackTest(unittest.TestCase):
    """When autotune_supported=False, use runtime values or warn about defaults."""

    def _scorer_params(self, out) -> dict:
        scorer = next(
            p for p in out.decisions["epp"]["endpoint_picker_config"]["plugins"]
            if p["type"] == "prefix-cache-scorer"
        )
        return scorer["parameters"]

    def test_supported_emits_autotune_true(self) -> None:
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(autotune_supported=True),
        )
        params = self._scorer_params(build_output(inp))
        self.assertEqual(params["autoTune"], True)
        self.assertNotIn("blockSizeTokens", params)
        self.assertNotIn("lruCapacityPerServer", params)

    def test_unsupported_with_runtime_values_emits_them(self) -> None:
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(autotune_supported=False),
            runtime=Runtime(block_size_tokens=64, lru_capacity_per_server=20000),
        )
        out = build_output(inp)
        params = self._scorer_params(out)
        self.assertEqual(params["autoTune"], False)
        self.assertEqual(params["blockSizeTokens"], 64)
        self.assertEqual(params["lruCapacityPerServer"], 20000)
        # No fallback warning when values supplied
        names = [p["name"] for p in out.parameters]
        self.assertNotIn("prefix-cache-scorer.fallback", names)

    def test_unsupported_without_runtime_values_warns(self) -> None:
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(autotune_supported=False),
        )
        out = build_output(inp)
        params = self._scorer_params(out)
        self.assertEqual(params["autoTune"], False)
        self.assertNotIn("blockSizeTokens", params)
        self.assertNotIn("lruCapacityPerServer", params)
        # Fallback warning surfaced
        fallback = next(
            p for p in out.parameters if p["name"] == "prefix-cache-scorer.fallback"
        )
        self.assertIn("blockSizeTokens", fallback["value"])
        self.assertIn("lruCapacityPerServer", fallback["value"])


class LatencyPredictorScaffoldingTest(unittest.TestCase):
    """When enable_latency_predictor is on, the full plugin set + prereq questions appear."""

    def _make_input(self, with_slo: bool) -> Input:
        return Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(enable_latency_predictor=True),
            slo=SLO(ttft_ms=200, tpot_ms=15) if with_slo else SLO(),
        )

    def test_includes_producer_and_admitter_with_slo(self) -> None:
        out = build_output(self._make_input(with_slo=True))
        types = [
            p["type"] for p in out.decisions["epp"]["endpoint_picker_config"]["plugins"]
        ]
        self.assertIn("predicted-latency-producer", types)
        self.assertIn("latency-scorer", types)
        self.assertIn("slo-headroom-tier-filter", types)
        self.assertIn("latency-slo-admitter", types)

    def test_omits_admitter_without_slo(self) -> None:
        out = build_output(self._make_input(with_slo=False))
        types = [
            p["type"] for p in out.decisions["epp"]["endpoint_picker_config"]["plugins"]
        ]
        self.assertIn("predicted-latency-producer", types)
        self.assertNotIn("latency-slo-admitter", types)

    def test_admitter_not_in_profile_but_producer_is(self) -> None:
        # Per predicted-latency-slo.values.yaml: predicted-latency-producer IS
        # in the default profile (as an unweighted ref so it runs on every
        # request to publish samples). latency-slo-admitter is wired via
        # featureGates / pre-request handler, NOT through schedulingProfiles.
        out = build_output(self._make_input(with_slo=True))
        profile = out.decisions["epp"]["endpoint_picker_config"]["schedulingProfiles"][0]
        refs = [p["pluginRef"] for p in profile["plugins"]]
        self.assertIn("predicted-latency-producer", refs)
        self.assertNotIn("latency-slo-admitter", refs)

    def test_does_not_surface_bogus_sidecar_prereqs(self) -> None:
        # The chart's latencyPredictor.enabled toggle deploys sidecars + sets env
        # vars automatically. These should NOT appear as user-actionable prereqs.
        out = build_output(self._make_input(with_slo=True))
        params = [q["parameter"] for q in out.unresolved_questions]
        self.assertNotIn("cluster.latency_predictor_sidecars", params)
        self.assertNotIn("epp.env.PREDICTION_SERVER_URL+TRAINING_SERVER_URL", params)

    def test_uses_canonical_picker_and_affinity_filters(self) -> None:
        """When latency-predictor is enabled, the plugin set should match
        predicted-latency-slo.values.yaml: weighted-random-picker (not max),
        plus strict + loose prefix-cache-affinity-filter instances.
        """
        out = build_output(self._make_input(with_slo=True))
        types = [
            p["type"] for p in out.decisions["epp"]["endpoint_picker_config"]["plugins"]
        ]
        # Picker swap
        self.assertIn("weighted-random-picker", types)
        self.assertNotIn("max-score-picker", types)
        # Affinity filter pair (same type, two named instances)
        affinity_entries = [
            p for p in out.decisions["epp"]["endpoint_picker_config"]["plugins"]
            if p["type"] == "prefix-cache-affinity-filter"
        ]
        self.assertEqual(len(affinity_entries), 2)
        names = sorted(p["name"] for p in affinity_entries)
        self.assertEqual(names, ["loose-affinity-filter", "strict-affinity-filter"])
        thresholds = sorted(p["parameters"]["affinityThreshold"] for p in affinity_entries)
        self.assertEqual(thresholds, [0.8, 0.99])
        # Profile references the named instances, not the type
        profile_refs = [
            p["pluginRef"] for p in out.decisions["epp"]["endpoint_picker_config"]["schedulingProfiles"][0]["plugins"]
        ]
        self.assertIn("strict-affinity-filter", profile_refs)
        self.assertIn("loose-affinity-filter", profile_refs)
        self.assertIn("weighted-random-picker", profile_refs)
        self.assertNotIn("max-score-picker", profile_refs)

    def test_warns_with_accurate_constraints(self) -> None:
        out = build_output(self._make_input(with_slo=True))
        joined = " ".join(out.warnings)
        # The chart toggle is the actual deploy mechanism — agent needs to know
        self.assertIn("latencyPredictor.enabled=true", joined)
        # streamingMode is a CHOICE, not a hard requirement
        self.assertIn("streamingMode", joined)
        self.assertIn("non-streaming clients", joined)
        # Real constraints we DO keep
        self.assertIn("homogeneous", joined)
        # Resource numbers come from the chart defaults: 2 sidecar containers,
        # ~8Gi memory requested.
        self.assertIn("2 sidecar containers", joined)
        self.assertIn("8Gi memory", joined)
        # We don't ship an unvalidated QPS-per-pod number, so these must not appear.
        self.assertNotIn("300 QPS", joined)
        self.assertNotIn("900 QPS", joined)
        self.assertNotIn("4 sidecar", joined)
        # The producer is PD-aware via endpointRoleLabel, so there's no
        # "does NOT support PD" warning, and streamingMode is a choice rather
        # than a hard "streaming-only" requirement.
        self.assertNotIn("does NOT support PD", joined)
        self.assertNotIn("streaming-only", joined)


class CorrectnessQuestionTest(unittest.TestCase):
    """When the agent recommends precise-prefix-cache-scorer but doesn't
    supply correctness inputs, the script surfaces an unresolved question.
    """

    def test_precise_prefix_without_block_size_unresolved(self) -> None:
        inp = parse_input(json.dumps({
            "model": "some-model",
            "topology": {"mode": "agg", "replicas": 2, "tp": 1},
            "workload": {"prefix_share": "high"},
            "recommendation": {
                "plugins": [{"type": "precise-prefix-cache-scorer"}],
            },
        }))
        out = build_output(inp)
        # Agent enabled precise-prefix → script asks for block_size + hash_seed
        self.assertEqual(len(out.unresolved_questions), 1)
        self.assertIn("blockSize", out.unresolved_questions[0]["parameter"])

    def test_precise_prefix_with_correctness_no_unresolved(self) -> None:
        inp = parse_input(json.dumps({
            "model": "some-model",
            "topology": {"mode": "agg", "replicas": 2, "tp": 1},
            "workload": {"prefix_share": "high"},
            "recommendation": {
                "plugins": [{"type": "precise-prefix-cache-scorer"}],
            },
            "correctness": {"vllm_block_size": 64, "vllm_hash_seed": "42"},
        }))
        out = build_output(inp)
        self.assertEqual(out.unresolved_questions, [])


class DeterminismTest(unittest.TestCase):
    """Same input → byte-identical output (modulo whitespace in JSON)."""

    def test_byte_identical_runs(self) -> None:
        raw = (EXAMPLES / "input-balanced-chat.json").read_text()
        out1 = output_to_dict(build_output(parse_input(raw)))
        out2 = output_to_dict(build_output(parse_input(raw)))
        self.assertEqual(out1, out2)
        self.assertEqual(out1["input_hash"], out2["input_hash"])


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()

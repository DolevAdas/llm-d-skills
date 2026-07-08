"""Benchmark config builder + deployment manifest + harness-specific shapes.

Covers benchmark.py's config rendering (`decisions.benchmark.config`),
the K8s Job + ConfigMap output for the bench harness, the inference-perf
harness shape, the guidellm harness shape, and parse_bench_results.py.
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
    output_to_dict,
    parse_input,
)

EXAMPLES = _REPO_ROOT / "examples"
SKILL_SCRIPTS_DIR = _REPO_ROOT / "skill" / "llm-d-autoconfig" / "scripts"


class BenchmarkConfigTest(unittest.TestCase):
    """The benchmark config in decisions.benchmark.config must match the EPP
    config on model + namespace (single source of truth). A mismatch is what
    makes a benchmark silently test the wrong model.
    """

    def _build(self, **kwargs) -> dict:
        defaults = dict(
            model="Qwen/Qwen3-32B",
            topology=Topology(mode="agg", replicas=8, tp=2),
        )
        defaults.update(kwargs)
        return output_to_dict(build_output(Input(**defaults)))

    def test_benchmark_always_emitted(self) -> None:
        out = self._build()
        self.assertIn("benchmark", out["decisions"])
        bench = out["decisions"]["benchmark"]
        self.assertEqual(bench["harness"], "guidellm")
        self.assertIn("config", bench)

    def test_benchmark_model_matches_epp_config(self) -> None:
        out = self._build(model="meta-llama/Llama-3.1-8B-Instruct")
        bench_model = out["decisions"]["benchmark"]["config"]["endpoint"]["model"]
        # Every workload in the benchmark must also reference the same model
        for wl_name, wl in out["decisions"]["benchmark"]["config"]["workload"].items():
            self.assertEqual(wl["model"], bench_model, f"workload {wl_name} drifted")
        self.assertEqual(bench_model, "meta-llama/Llama-3.1-8B-Instruct")

    def test_benchmark_endpoints_use_placeholders(self) -> None:
        out = self._build()
        bench = out["decisions"]["benchmark"]["config"]
        # Phase 11 substitutes these at deploy time; they must remain unresolved
        # in the script's output so the agent knows what to fill in.
        self.assertEqual(bench["endpoint"]["base_url"], "${IP}")
        self.assertEqual(bench["endpoint"]["namespace"], "${NAMESPACE}")
        self.assertEqual(bench["harness"]["results_pvc"], "${BENCHMARK_PVC}")

    def test_sanity_workload_always_present(self) -> None:
        out = self._build(slo=SLO())  # no SLAs
        wls = out["decisions"]["benchmark"]["config"]["workload"]
        self.assertIn("sanity", wls)
        self.assertEqual(wls["sanity"]["rate"], [1])
        self.assertEqual(wls["sanity"]["max_seconds"], 30)

    def test_sla_validation_workload_only_when_sla_present(self) -> None:
        out_no_sla = self._build(slo=SLO())
        self.assertNotIn("sla_validation", out_no_sla["decisions"]["benchmark"]["config"]["workload"])
        out_with_sla = self._build(slo=SLO(ttft_ms=800, tpot_ms=25))
        sla_wl = out_with_sla["decisions"]["benchmark"]["config"]["workload"]["sla_validation"]
        self.assertEqual(sla_wl["sla_targets"]["ttft_ms_p95_target"], 800)
        self.assertEqual(sla_wl["sla_targets"]["tpot_ms_p95_target"], 25)
        # Rates are derived from cluster size + workload class — not hardcoded
        self.assertEqual(len(sla_wl["rate"]), 3)
        self.assertTrue(sla_wl["rate"][0] < sla_wl["rate"][1] < sla_wl["rate"][2])
        self.assertEqual(sla_wl["max_seconds"], 120)
        # Surfaced derivation explanation
        self.assertIn("_rate_derivation", sla_wl)

    def test_sla_rates_scale_with_replicas(self) -> None:
        small = self._build(
            topology=Topology(mode="agg", replicas=2, tp=1),
            slo=SLO(ttft_ms=800, tpot_ms=25),
            workload=Workload(isl=1000, osl=500),
        )
        large = self._build(
            topology=Topology(mode="agg", replicas=16, tp=1),
            slo=SLO(ttft_ms=800, tpot_ms=25),
            workload=Workload(isl=1000, osl=500),
        )
        small_rates = small["decisions"]["benchmark"]["config"]["workload"]["sla_validation"]["rate"]
        large_rates = large["decisions"]["benchmark"]["config"]["workload"]["sla_validation"]["rate"]
        # 16 replicas should produce strictly larger rates than 2 replicas at the same workload
        self.assertGreater(large_rates[2], small_rates[2])
        self.assertGreater(large_rates[1], small_rates[1])

    def test_sla_rates_drop_for_long_prompts(self) -> None:
        short = self._build(
            workload=Workload(isl=500, osl=200),
            slo=SLO(ttft_ms=800, tpot_ms=25),
        )
        long = self._build(
            workload=Workload(isl=8000, osl=2000),
            slo=SLO(ttft_ms=800, tpot_ms=25),
        )
        short_top = short["decisions"]["benchmark"]["config"]["workload"]["sla_validation"]["rate"][2]
        long_top = long["decisions"]["benchmark"]["config"]["workload"]["sla_validation"]["rate"][2]
        # Longer prompts/outputs = lower throughput → lower estimated saturation rate
        self.assertGreater(short_top, long_top)

    def test_rag_style_emits_shared_prefix_warning(self) -> None:
        out = self._build(
            workload=Workload(isl=4000, osl=300, prefix_share="high", prefix_len=3000),
        )
        joined = " ".join(out["warnings"])
        self.assertIn("guidellm does not synthesize shared prefixes", joined)
        # Warning points users at the inference-perf harness as the fix
        self.assertIn("inference-perf", joined)
        # And we do NOT emit the invalid shared_prefix_tokens field
        sanity_data = out["decisions"]["benchmark"]["config"]["workload"]["sanity"]["data"]
        self.assertNotIn("shared_prefix_tokens", sanity_data)

    def test_balanced_class_uses_fixed_prompt_length(self) -> None:
        out = self._build(workload=Workload(isl=1500, osl=500, prefix_share="low"))
        data = out["decisions"]["benchmark"]["config"]["workload"]["sanity"]["data"]
        self.assertEqual(data["prompt_tokens"], 1500)
        self.assertEqual(data["output_tokens"], 500)
        self.assertNotIn("shared_prefix_tokens", data)
        # Sanity data block is the simple form — no min/max/stdev
        self.assertNotIn("prompt_tokens_stdev", data)

    def test_sla_validation_uses_richer_distribution_form(self) -> None:
        out = self._build(
            workload=Workload(isl=1000, osl=500),
            slo=SLO(ttft_ms=800, tpot_ms=25),
        )
        data = out["decisions"]["benchmark"]["config"]["workload"]["sla_validation"]["data"]
        # Richer form has min/max/stdev/samples
        for key in ("prompt_tokens_min", "prompt_tokens_max", "prompt_tokens_stdev",
                    "output_tokens_min", "output_tokens_max", "output_tokens_stdev",
                    "samples"):
            self.assertIn(key, data, f"missing {key} in SLA validation data block")
        self.assertEqual(data["prompt_tokens"], 1000)
        self.assertEqual(data["prompt_tokens_stdev"], 100)  # 10% of mean
        self.assertEqual(data["samples"], 1000)

    def test_falls_back_to_template_defaults_when_isl_osl_missing(self) -> None:
        out = self._build(workload=Workload())  # no ISL/OSL
        data = out["decisions"]["benchmark"]["config"]["workload"]["sanity"]["data"]
        # Match the optimized-baseline guidellm.yaml template defaults
        self.assertEqual(data["prompt_tokens"], 50)
        self.assertEqual(data["output_tokens"], 50)


class BenchmarkDeploymentTest(unittest.TestCase):
    """build_benchmark_deployment produces a complete K8s YAML ready for
    `kubectl apply -f` (ConfigMap + Job, substitution done at call time).
    """

    def _build(self, *, pvc=None, **input_kwargs):
        import yaml as yaml_mod
        from benchmark import build_benchmark, build_benchmark_deployment
        defaults = dict(
            model="Qwen/Qwen3-32B",
            topology=Topology(mode="agg", replicas=8, tp=2),
            slo=SLO(ttft_ms=800, tpot_ms=25),
            workload=Workload(isl=1000, osl=500),
        )
        defaults.update(input_kwargs)
        inp = Input(**defaults)
        bench = build_benchmark(inp, "balanced-conversational")
        return list(yaml_mod.safe_load_all(
            build_benchmark_deployment(
                bench["config"],
                target_url="http://10.0.0.1",
                namespace="prod-chat",
                pvc_name=pvc,
            )
        ))

    def test_emits_two_documents(self) -> None:
        docs = self._build()
        self.assertEqual(len(docs), 2)
        kinds = sorted(d["kind"] for d in docs)
        self.assertEqual(kinds, ["ConfigMap", "Job"])

    def test_substitutes_placeholders_at_render_time(self) -> None:
        docs = self._build()
        cm = next(d for d in docs if d["kind"] == "ConfigMap")
        # The workload YAML in the ConfigMap has all placeholders resolved.
        # Filename is "autoconfig-workload.yaml" (referenced by the Job's -w arg).
        bench_text = cm["data"]["autoconfig-workload.yaml"]
        self.assertNotIn("${IP}", bench_text)
        self.assertNotIn("${NAMESPACE}", bench_text)
        self.assertNotIn("${BENCHMARK_PVC}", bench_text)
        self.assertIn("http://10.0.0.1", bench_text)
        self.assertIn("prod-chat", bench_text)

    def test_uses_emptydir_when_no_pvc_provided(self) -> None:
        docs = self._build()  # no _pvc kwarg
        job = next(d for d in docs if d["kind"] == "Job")
        volumes = job["spec"]["template"]["spec"]["volumes"]
        results_vol = next(v for v in volumes if v["name"] == "results")
        self.assertIn("emptyDir", results_vol)
        self.assertNotIn("persistentVolumeClaim", results_vol)

    def test_uses_pvc_when_provided(self) -> None:
        docs = self._build(pvc="my-bench-pvc")
        job = next(d for d in docs if d["kind"] == "Job")
        volumes = job["spec"]["template"]["spec"]["volumes"]
        results_vol = next(v for v in volumes if v["name"] == "results")
        self.assertEqual(results_vol["persistentVolumeClaim"]["claimName"], "my-bench-pvc")
        self.assertNotIn("emptyDir", results_vol)

    def test_job_name_is_deterministic(self) -> None:
        # Same inputs produce the same Job name (idempotent kubectl apply).
        docs1 = self._build()
        docs2 = self._build()
        name1 = next(d for d in docs1 if d["kind"] == "Job")["metadata"]["name"]
        name2 = next(d for d in docs2 if d["kind"] == "Job")["metadata"]["name"]
        self.assertEqual(name1, name2)
        self.assertTrue(name1.startswith("autoconfig-bench-"))

    def test_job_mounts_configmap(self) -> None:
        docs = self._build()
        cm = next(d for d in docs if d["kind"] == "ConfigMap")
        job = next(d for d in docs if d["kind"] == "Job")
        volumes = job["spec"]["template"]["spec"]["volumes"]
        config_vol = next(v for v in volumes if v["name"] == "config")
        self.assertEqual(config_vol["configMap"]["name"], cm["metadata"]["name"])
        # Container is wrapped with sh -c so the harness's results cat to stdout
        # post-completion. The wrapped runner_cmd carries the original guidellm
        # entrypoint invocation.
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], ["sh", "-c"])
        joined_args = " ".join(container["args"])
        self.assertIn("-l guidellm", joined_args)
        self.assertIn("autoconfig-workload.yaml", joined_args)
        self.assertIn("BENCHMARK RESULTS", joined_args)
        # ConfigMap mounts inside the harness's profile dir so the entrypoint finds it
        config_mount = next(
            m for m in container["volumeMounts"] if m["name"] == "config"
        )
        self.assertEqual(config_mount["mountPath"], "/profiles/guidellm")


class InferencePerfHarnessTest(unittest.TestCase):
    """The inference-perf path uses:
    - Native flat schema (no endpoint/control/harness/workload wrappers)
    - Native inference-perf image (not the llm-d-benchmark wrapper)
    - HF_TOKEN + RAYON_NUM_THREADS env injected
    - Container wrapped with sh -c to cat results to stdout post-completion
    """

    def _build_config(self, **input_kwargs):
        from benchmark import build_benchmark
        from autoconfig_poc import Context
        defaults = dict(
            model="openai/gpt-oss-120b",
            topology=Topology(mode="agg", replicas=8, tp=2),
            workload=Workload(isl=5000, osl=500),
            slo=SLO(ttft_ms=1500, tpot_ms=30),
            # HF Secret opt-in (Q0.5 = "scaffold new") so HF_TOKEN env wires through.
            context=Context(hf_secret_name="llm-d-hf-token"),
        )
        defaults.update(input_kwargs)
        return build_benchmark(Input(**defaults), "balanced-conversational", harness="inference-perf")

    def _build_deployment(self, **input_kwargs):
        import yaml as yaml_mod
        from benchmark import build_benchmark_deployment
        bench = self._build_config(**input_kwargs)
        return list(yaml_mod.safe_load_all(
            build_benchmark_deployment(
                bench["config"],
                target_url="http://10.0.0.1",
                namespace="prod-chat",
                pvc_name=None,
            )
        ))

    def test_flat_schema_no_workload_wrapper(self) -> None:
        """The native inference-perf binary expects load/api/server/etc at the
        top level, not nested under workload.<name>."""
        cfg = self._build_config()["config"]
        # Wrapper keys MUST NOT be present
        for forbidden in ("endpoint", "control", "harness", "workload"):
            self.assertNotIn(forbidden, cfg)
        # Native top-level keys MUST be present
        for required in ("load", "api", "server", "tokenizer", "data", "report", "storage"):
            self.assertIn(required, cfg)

    def test_load_combines_warmup_plus_sla_stages(self) -> None:
        """Sanity (rate=1, 30s) + 3 SLA stages combined into ONE multi-stage load."""
        cfg = self._build_config()["config"]
        stages = cfg["load"]["stages"]
        self.assertEqual(len(stages), 4)  # 1 warmup + 3 SLA stages
        self.assertEqual(stages[0], {"rate": 1, "duration": 30})
        # Subsequent stages are 120s each, monotonic increasing rate
        for s in stages[1:]:
            self.assertEqual(s["duration"], 120)
        rates = [s["rate"] for s in stages]
        self.assertEqual(rates, sorted(rates))

    def test_load_warmup_only_when_no_slo(self) -> None:
        cfg = self._build_config(slo=SLO())["config"]
        stages = cfg["load"]["stages"]
        self.assertEqual(stages, [{"rate": 1, "duration": 30}])

    def test_uses_native_inference_perf_image(self) -> None:
        """Bypassing the llm-d-benchmark wrapper — native image only."""
        docs = self._build_deployment()
        job = next(d for d in docs if d["kind"] == "Job")
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("quay.io/inference-perf/inference-perf", container["image"])
        self.assertNotIn("llm-d-benchmark", container["image"])

    def test_native_command_and_config_flag(self) -> None:
        docs = self._build_deployment()
        container = docs[1]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], ["sh", "-c"])
        joined = " ".join(container["args"])
        # Native inference-perf invocation
        self.assertIn("inference-perf --config_file /etc/config/config.yml", joined)
        # Cat-to-stdout wrapper for post-completion results retrieval
        self.assertIn("BENCHMARK RESULTS", joined)

    def test_configmap_at_etc_config_with_native_filename(self) -> None:
        docs = self._build_deployment()
        cm = next(d for d in docs if d["kind"] == "ConfigMap")
        # Native filename is config.yml (not autoconfig-workload.yaml)
        self.assertIn("config.yml", cm["data"])
        self.assertNotIn("autoconfig-workload.yaml", cm["data"])
        # Mount path matches the inference-perf helm chart's expectation
        container = docs[1]["spec"]["template"]["spec"]["containers"][0]
        config_mount = next(m for m in container["volumeMounts"] if m["name"] == "config")
        self.assertEqual(config_mount["mountPath"], "/etc/config")

    def test_results_mount_avoids_workspace_for_inference_perf(self) -> None:
        """Mount-over bug: the inference-perf image has WORKDIR
        /workspace + venv at /workspace/.venv. Mounting any volume at /workspace
        shadows the binary, giving 'sh: inference-perf: not found'. Results MUST mount
        at /results (or anywhere outside /workspace)."""
        docs = self._build_deployment()
        container = docs[1]["spec"]["template"]["spec"]["containers"][0]
        results_mount = next(m for m in container["volumeMounts"] if m["name"] == "results")
        self.assertEqual(results_mount["mountPath"], "/results")
        self.assertNotEqual(results_mount["mountPath"], "/workspace")

    def test_storage_path_in_config_matches_results_mount(self) -> None:
        """The storage.local_storage.path in the inference-perf workload config
        must match where the results volume is mounted, otherwise inference-perf
        writes results into the venv (or fails) instead of the volume."""
        cfg = self._build_config()["config"]
        self.assertEqual(cfg["storage"]["local_storage"]["path"], "/results")

    def test_cat_to_stdout_searches_results_path(self) -> None:
        """The post-completion result dump must look in /results (where the
        volume is mounted), not /workspace."""
        docs = self._build_deployment()
        joined_args = " ".join(docs[1]["spec"]["template"]["spec"]["containers"][0]["args"])
        self.assertIn("find /results", joined_args)
        self.assertNotIn("find /workspace", joined_args)

    def test_injects_hf_token_and_rayon_env(self) -> None:
        docs = self._build_deployment()
        env = docs[1]["spec"]["template"]["spec"]["containers"][0].get("env", [])
        env_by_name = {e["name"]: e for e in env}
        self.assertIn("RAYON_NUM_THREADS", env_by_name)
        self.assertEqual(env_by_name["RAYON_NUM_THREADS"]["value"], "4")
        # HF_TOKEN sourced from the hf_token_secret sentinel, marked optional
        self.assertIn("HF_TOKEN", env_by_name)
        secret_ref = env_by_name["HF_TOKEN"]["valueFrom"]["secretKeyRef"]
        self.assertEqual(secret_ref["name"], "llm-d-hf-token")
        self.assertTrue(secret_ref.get("optional"))

    def test_configmap_strips_sentinel_keys(self) -> None:
        """Internal `_*` keys (sentinels) must not leak into the ConfigMap YAML."""
        docs = self._build_deployment()
        cm = next(d for d in docs if d["kind"] == "ConfigMap")
        rendered = cm["data"]["config.yml"]
        for sentinel in ("_hf_token_secret", "_rate_derivation", "_sla_targets"):
            self.assertNotIn(sentinel, rendered)


class GuidellmHarnessTest(unittest.TestCase):
    """The guidellm path uses the llm-d-benchmark wrapper image (the wrapper
    handles guidellm correctly), with an sh -c wrap so results cat to stdout
    and an optional HF_TOKEN env injection."""

    def _build(self):
        import yaml as yaml_mod
        from benchmark import build_benchmark, build_benchmark_deployment
        from autoconfig_poc import Context
        inp = Input(
            model="Qwen/Qwen3-32B",
            topology=Topology(mode="agg", replicas=8, tp=2),
            workload=Workload(isl=1000, osl=500),
            slo=SLO(ttft_ms=800, tpot_ms=25),
            # HF Secret opt-in (Q0.5 = "scaffold new") so HF_TOKEN env wires through.
            context=Context(hf_secret_name="llm-d-hf-token"),
        )
        bench = build_benchmark(inp, "balanced-conversational", harness="guidellm")
        return list(yaml_mod.safe_load_all(
            build_benchmark_deployment(
                bench["config"],
                target_url="http://10.0.0.1",
                namespace="prod-chat",
                pvc_name=None,
            )
        ))

    def test_keeps_wrapper_image(self) -> None:
        docs = self._build()
        container = docs[1]["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("ghcr.io/llm-d/llm-d-benchmark", container["image"])

    def test_results_mount_stays_at_workspace_for_guidellm(self) -> None:
        """The wrapper image expects /workspace as its data path and has no
        venv conflict there. Don't migrate guidellm to /results just because
        inference-perf needed to."""
        docs = self._build()
        container = docs[1]["spec"]["template"]["spec"]["containers"][0]
        results_mount = next(m for m in container["volumeMounts"] if m["name"] == "results")
        self.assertEqual(results_mount["mountPath"], "/workspace")

    def test_keeps_nested_wrapper_schema(self) -> None:
        """guidellm path KEEPS the wrapper's endpoint/control/harness/workload structure."""
        from benchmark import build_benchmark
        inp = Input(
            model="Qwen/Qwen3-32B",
            topology=Topology(mode="agg", replicas=8, tp=2),
        )
        cfg = build_benchmark(inp, "balanced-conversational", harness="guidellm")["config"]
        for required in ("endpoint", "control", "harness", "workload"):
            self.assertIn(required, cfg)

    def test_tokenizer_override_propagates_to_endpoint(self) -> None:
        """context.bench_tokenizer_override → wrapper config's
        endpoint.tokenizer (mirrors inference-perf's tokenizer override)."""
        import yaml as yaml_mod
        from autoconfig_poc import Context
        from benchmark import build_benchmark
        inp = Input(
            model="company/proprietary-llama-finetune",
            topology=Topology(mode="agg", replicas=2, tp=1),
            context=Context(bench_tokenizer_override="meta-llama/Llama-3.1-8B-Instruct"),
        )
        cfg = build_benchmark(inp, "balanced-conversational", harness="guidellm")["config"]
        # Override emitted under endpoint.tokenizer; endpoint.model still names
        # the served model (the wrapper uses model for routing, tokenizer for HF).
        self.assertEqual(cfg["endpoint"]["model"], "company/proprietary-llama-finetune")
        self.assertEqual(cfg["endpoint"]["tokenizer"], "meta-llama/Llama-3.1-8B-Instruct")

    def test_no_tokenizer_key_when_override_unset(self) -> None:
        """When bench_tokenizer_override is unset (the common case), the
        endpoint block omits the `tokenizer` key entirely so wrapper
        versions that don't recognize it stay clean."""
        from benchmark import build_benchmark
        inp = Input(
            model="Qwen/Qwen3-32B",
            topology=Topology(mode="agg", replicas=2, tp=1),
        )
        cfg = build_benchmark(inp, "balanced-conversational", harness="guidellm")["config"]
        self.assertNotIn("tokenizer", cfg["endpoint"])

    def test_injects_hf_token_optional(self) -> None:
        docs = self._build()
        env = docs[1]["spec"]["template"]["spec"]["containers"][0].get("env", [])
        env_by_name = {e["name"]: e for e in env}
        self.assertIn("HF_TOKEN", env_by_name)
        # guidellm doesn't need RAYON_NUM_THREADS
        self.assertNotIn("RAYON_NUM_THREADS", env_by_name)

    def test_wrapper_invocation_in_args(self) -> None:
        docs = self._build()
        container = docs[1]["spec"]["template"]["spec"]["containers"][0]
        joined = " ".join(container["args"])
        self.assertIn("-l guidellm", joined)
        self.assertIn("autoconfig-workload.yaml", joined)


class ParseBenchResultsTest(unittest.TestCase):
    """parse_bench_results.py reads `kubectl logs` output, extracts the JSON
    blocks after the BENCHMARK RESULTS delimiter, normalizes inference-perf
    vs guidellm shapes, renders a markdown table, and exits 0/1/2."""

    _DELIMITER = "---BENCHMARK RESULTS (json files in /results)---"

    def _logs_with_inference_perf_json(self) -> str:
        """Canned `kubectl logs` output: harness summary lines, the bench
        delimiter, then `find -print -exec cat` pairs of (path, JSON body)."""
        bench_json = json.dumps({
            "successes": {
                "count": 100,
                "latency": {
                    "request_latency": {"mean": 1.2, "median": 1.1, "p95": 1.8, "p99": 2.4},
                    "time_to_first_token": {"mean": 0.3, "median": 0.28, "p95": 0.6, "p99": 0.9},
                    "time_per_output_token": {"mean": 0.02, "median": 0.018, "p95": 0.04, "p99": 0.06},
                },
                "throughput": {
                    "requests_per_sec": 10.5,
                    "input_tokens_per_sec": 2000,
                    "output_tokens_per_sec": 1500,
                    "total_tokens_per_sec": 3500,
                },
            },
            "failures": {"count": 2},
        }, indent=2)
        return (
            "inference-perf started...\n"
            "Stage 1 complete\n"
            "Stage 2 complete\n"
            f"{self._DELIMITER}\n"
            "/results/summary_lifecycle_metrics.json\n"
            f"{bench_json}\n"
            "/results/per_request_lifecycle.json\n"
            '{"unrelated": "json that does not match either format"}\n'
        )

    def _logs_with_guidellm_json(self) -> str:
        bench_json = json.dumps({
            "benchmarks": [
                {
                    "successful_requests": 50,
                    "errored_requests": 0,
                    "requests_per_second": 5.0,
                    "prompt_tokens_per_second": 1000,
                    "output_tokens_per_second": 800,
                    "metrics": {
                        "request_latency": {"mean": 800, "median": 750, "p95": 1200, "p99": 1500},
                        "time_to_first_token_ms": {"mean": 200, "median": 180, "p95": 400, "p99": 550},
                        "inter_token_latency_ms": {"mean": 15, "median": 14, "p95": 28, "p99": 35},
                    },
                },
                {
                    "successful_requests": 100,
                    "errored_requests": 5,
                    "requests_per_second": 10.0,
                    "prompt_tokens_per_second": 2000,
                    "output_tokens_per_second": 1500,
                    "metrics": {
                        "request_latency": {"mean": 1100, "median": 1000, "p95": 1700, "p99": 2100},
                        "time_to_first_token_ms": {"mean": 300, "median": 280, "p95": 650, "p99": 850},
                        "inter_token_latency_ms": {"mean": 22, "median": 20, "p95": 38, "p99": 50},
                    },
                },
            ],
        }, indent=2)
        return (
            "guidellm wrapper running...\n"
            f"{self._DELIMITER}\n"
            "/workspace/benchmarks_aggregator.json\n"
            f"{bench_json}\n"
        )

    def _run(self, stdin_text: str, *argv: str):
        """Invoke parse_bench_results.main with stdin replaced."""
        import io
        import sys as _sys
        # parse_bench_results is at skill/.../scripts/parse_bench_results.py;
        # conftest's sys.path insert means we can import it directly.
        import parse_bench_results
        old_stdin, old_stdout, old_stderr = _sys.stdin, _sys.stdout, _sys.stderr
        try:
            _sys.stdin = io.StringIO(stdin_text)
            _sys.stdout = io.StringIO()
            _sys.stderr = io.StringIO()
            code = parse_bench_results.main(list(argv))
            return code, _sys.stdout.getvalue(), _sys.stderr.getvalue()
        finally:
            _sys.stdin, _sys.stdout, _sys.stderr = old_stdin, old_stdout, old_stderr

    def test_inference_perf_parse_no_slas_returns_0(self) -> None:
        code, stdout, _ = self._run(self._logs_with_inference_perf_json())
        self.assertEqual(code, 0)
        self.assertIn("inference-perf", stdout)
        self.assertIn("Requests:", stdout)
        # Latencies converted from seconds to ms in the table
        self.assertIn("600.0", stdout)  # ttft p95 = 0.6s → 600 ms
        self.assertIn("40.0", stdout)   # tpot p95 = 0.04s → 40 ms

    def test_guidellm_parse_no_slas_returns_0(self) -> None:
        code, stdout, _ = self._run(self._logs_with_guidellm_json())
        self.assertEqual(code, 0)
        self.assertIn("guidellm", stdout)
        # Parser picks the LAST benchmark (highest-rate / most loaded)
        self.assertIn("100", stdout)  # successful_requests from second stage
        self.assertIn("650", stdout)  # ttft p95 from second stage (string match)

    def test_sla_within_target_returns_0(self) -> None:
        code, stdout, _ = self._run(
            self._logs_with_guidellm_json(),
            "--ttft-sla", "800", "--tpot-sla", "50", "--e2e-sla", "2000",
        )
        self.assertEqual(code, 0)
        self.assertIn("within target", stdout)
        self.assertNotIn("EXCEEDS", stdout)

    def test_sla_breach_returns_1(self) -> None:
        # ttft_p95=650 vs target=500 → breach
        code, stdout, _ = self._run(
            self._logs_with_guidellm_json(),
            "--ttft-sla", "500",
        )
        self.assertEqual(code, 1)
        self.assertIn("EXCEEDS", stdout)
        self.assertIn("TTFT", stdout)

    def test_missing_delimiter_returns_2(self) -> None:
        # No delimiter at all — most common failure mode (truncated logs).
        code, _, stderr = self._run("just some random logs without the marker\n")
        self.assertEqual(code, 2)
        self.assertIn("delimiter", stderr.lower())

    def test_delimiter_present_but_no_recognizable_json_returns_2(self) -> None:
        text = (
            f"{self._DELIMITER}\n"
            '/results/foo.json\n'
            '{"some_unknown_format": "we cannot parse this"}\n'
        )
        code, _, stderr = self._run(text)
        self.assertEqual(code, 2)
        self.assertIn("none matched", stderr.lower())

    def test_brace_walk_handles_nested_objects(self) -> None:
        """Brace-counting must handle nested JSON correctly (don't trip on
        inner `{` / `}`). The inference-perf JSON has deep nesting."""
        code, _, _ = self._run(self._logs_with_inference_perf_json())
        self.assertEqual(code, 0)

    def test_strings_with_braces_dont_confuse_parser(self) -> None:
        """Brace-walk skips `{` and `}` inside string literals."""
        text = (
            f"{self._DELIMITER}\n"
            '{"benchmarks": [{"metrics": {"x": "value with } and { inside"}, "successful_requests": 1}]}\n'
        )
        # Should parse without raising, but the format is incomplete →
        # _normalize_guidellm returns None for missing keys → exit 2.
        # The point of this test: brace-walk doesn't crash or mis-count.
        code, _, _ = self._run(text)
        self.assertIn(code, (0, 2))  # parses successfully; format may not validate




if __name__ == "__main__":
    unittest.main()

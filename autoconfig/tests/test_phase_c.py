"""Phase C bundle: context schema + bundle renderer + CRD ingestion + bundle-dir."""

from __future__ import annotations

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
    render_helm_values,
)

EXAMPLES = _REPO_ROOT / "examples"


class PhaseCContextSchemaTest(unittest.TestCase):
    """Phase C added Context.deploy_mode + Context.gateway_provider. These
    drive bundle rendering (chart variant + HTTPRoute emission)."""

    def _parse(self, ctx: dict) -> Input:
        from autoconfig_poc import Context
        return Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            context=Context(**ctx),
        )

    def test_default_context_is_standalone(self) -> None:
        from autoconfig_poc import Context
        c = Context()
        self.assertEqual(c.deploy_mode, "standalone")
        self.assertIsNone(c.gateway_provider)

    def test_gateway_requires_provider(self) -> None:
        inp = self._parse({"deploy_mode": "gateway"})
        with self.assertRaises(ValueError) as cm:
            inp.validate()
        self.assertIn("gateway_provider", str(cm.exception))

    def test_invalid_deploy_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._parse({"deploy_mode": "sidecar"}).validate()

    def test_invalid_gateway_provider_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._parse({"deploy_mode": "gateway", "gateway_provider": "nginx"}).validate()

    def test_gateway_picks_gateway_chart(self) -> None:
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            context=__import__("autoconfig_poc").Context(
                deploy_mode="gateway", gateway_provider="istio",
            ),
        )
        out = build_output(inp)
        self.assertEqual(out.decisions["epp"]["chart"], "gateway")
        # The gateway chart toggle is on so the chart manages the HTTPRoute.
        self.assertEqual(
            out.decisions["epp"]["chart_toggles"].get("httpRoute.create"),
            True,
        )

    def test_standalone_picks_standalone_chart(self) -> None:
        inp = Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
        )
        out = build_output(inp)
        self.assertEqual(out.decisions["epp"]["chart"], "standalone")


class PhaseCBundleRendererTest(unittest.TestCase):
    """render_bundle() composes helm template output with hand-rendered
    resources. Tests inject a fake subprocess runner so helm doesn't need
    to be installed."""

    def _fake_helm(self, stdout: str):
        """Return a callable that mimics subprocess.run, capturing the
        helm-template invocation for inspection."""
        captured = {}

        def runner(cmd, *, input=None, capture_output=False, text=False, check=False):
            import types
            captured["cmd"] = cmd
            captured["input"] = input
            return types.SimpleNamespace(
                stdout=stdout, stderr="", returncode=0,
            )
        return runner, captured

    def _build(self, **ctx_overrides):
        from autoconfig_poc import Context
        return build_output(Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            context=Context(**ctx_overrides),
        ))

    def test_calls_helm_template_with_release_and_namespace(self) -> None:
        from autoconfig_poc import render_bundle
        out = self._build(namespace="prod-chat", release_name="chat")
        runner, captured = self._fake_helm(
            "apiVersion: v1\nkind: Deployment\nmetadata: {name: epp}\n",
        )
        render_bundle(out, subprocess_runner=runner, include_crds=False)
        self.assertEqual(captured["cmd"][0:3], ["helm", "template", "chat"])
        self.assertIn("--namespace", captured["cmd"])
        ns_idx = captured["cmd"].index("--namespace")
        self.assertEqual(captured["cmd"][ns_idx + 1], "prod-chat")

    def test_pipes_helm_values_via_stdin(self) -> None:
        from autoconfig_poc import render_bundle, render_helm_values
        out = self._build()
        runner, captured = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        render_bundle(out, subprocess_runner=runner, include_crds=False)
        # Three layered -f flags: recipe base values URL, guide values URL,
        # autoconfig values via stdin. Last one is "-" (stdin) so our
        # pluginsCustomConfig wins.
        f_positions = [i for i, a in enumerate(captured["cmd"]) if a == "-f"]
        self.assertEqual(len(f_positions), 3)
        self.assertEqual(captured["cmd"][f_positions[-1] + 1], "-")
        # Stdin payload is our helm-values fragment verbatim
        self.assertEqual(captured["input"], render_helm_values(out))
        self.assertEqual(captured["input"], render_helm_values(out))

    def test_picks_chart_oci_by_deploy_mode(self) -> None:
        from autoconfig_poc import render_bundle
        # Standalone
        out_sa = self._build()
        runner_sa, captured_sa = self._fake_helm("k: v")
        render_bundle(out_sa, subprocess_runner=runner_sa, include_crds=False)
        self.assertIn("llm-d-router-standalone", captured_sa["cmd"][3])
        # Gateway → llm-d-router-gateway chart
        out_gw = self._build(deploy_mode="gateway", gateway_provider="istio")
        runner_gw, captured_gw = self._fake_helm("k: v")
        render_bundle(out_gw, subprocess_runner=runner_gw, include_crds=False)
        self.assertIn("llm-d-router-gateway", captured_gw["cmd"][3])

    def test_appends_httproute_for_standalone_with_provider(self) -> None:
        from autoconfig_poc import render_bundle, Context
        # Standalone with a gateway provider set → standalone chart doesn't
        # emit HTTPRoute, so the renderer adds one.
        out = build_output(Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            context=Context(
                deploy_mode="standalone",
                gateway_provider="istio",
                release_name="my-rel",
                namespace="my-ns",
            ),
        ))
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = list(yaml_mod.safe_load_all(bundle))
        kinds = [d.get("kind") for d in docs if isinstance(d, dict)]
        self.assertIn("HTTPRoute", kinds)
        route = next(d for d in docs if isinstance(d, dict) and d.get("kind") == "HTTPRoute")
        self.assertEqual(route["metadata"]["name"], "my-rel-route")
        self.assertEqual(route["metadata"]["namespace"], "my-ns")
        backend_ref = route["spec"]["rules"][0]["backendRefs"][0]
        self.assertEqual(backend_ref["name"], "my-rel")
        self.assertEqual(backend_ref["kind"], "InferencePool")

    def test_no_httproute_for_pure_standalone(self) -> None:
        from autoconfig_poc import render_bundle
        out = self._build()  # standalone, no provider
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = list(yaml_mod.safe_load_all(bundle))
        kinds = [d.get("kind") for d in docs if isinstance(d, dict)]
        self.assertNotIn("HTTPRoute", kinds)

    def test_no_httproute_for_gateway_mode(self) -> None:
        # gateway mode uses inferencepool chart which emits its own HTTPRoute;
        # the renderer must NOT also add one or kubectl gets duplicate-name conflict.
        from autoconfig_poc import render_bundle
        out = self._build(deploy_mode="gateway", gateway_provider="istio")
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        # Bundle should contain only what the fake chart emitted, no extra HTTPRoute
        self.assertNotIn("HTTPRoute", bundle)

    def test_wva_renders_variant_autoscaling_cr(self) -> None:
        from autoconfig_poc import Features, render_bundle
        out = build_output(Input(
            model="meta-llama/Llama-3.1-8B-Instruct",
            topology=Topology(mode="agg", replicas=4, tp=2),
            features=Features(autoscaler="wva"),
        ))
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
        kinds = [d.get("kind") for d in docs]
        self.assertIn("VariantAutoscaling", kinds)
        va = next(d for d in docs if d["kind"] == "VariantAutoscaling")
        self.assertEqual(va["spec"]["modelID"], "meta-llama/Llama-3.1-8B-Instruct")
        self.assertEqual(va["spec"]["variants"][0]["replicas"], 4)
        self.assertEqual(va["spec"]["variants"][0]["modelServerArgs"]["tensorParallelSize"], 2)

    def test_wva_pd_emits_two_variants(self) -> None:
        from autoconfig_poc import Features, render_bundle
        out = build_output(Input(
            model="openai/gpt-oss-120b",
            topology=Topology(
                mode="disagg",
                prefill_replicas=8, prefill_tp=1,
                decode_replicas=2, decode_tp=4,
                pd_transport="rdma",
            ),
            features=Features(autoscaler="wva"),
        ))
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
        va = next(d for d in docs if d.get("kind") == "VariantAutoscaling")
        variant_names = sorted(v["name"] for v in va["spec"]["variants"])
        self.assertEqual(variant_names, ["decode", "prefill"])

    def test_hpa_renders_horizontalpodautoscaler(self) -> None:
        from autoconfig_poc import Features, render_bundle
        out = build_output(Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(autoscaler="hpa"),
        ))
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
        kinds = [d.get("kind") for d in docs]
        self.assertIn("HorizontalPodAutoscaler", kinds)

    def test_tiered_cache_emits_advisory_not_kustomization(self) -> None:
        # The tiered-cache overlay forks by tier/accelerator/connector, so the
        # script can't emit one valid Kustomization. It emits a comment-only
        # advisory instead (no kubectl-applicable resource).
        from autoconfig_poc import Features, render_bundle
        out = build_output(Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(enable_tiered_cache=True),
        ))
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
        self.assertNotIn("Kustomization", [d.get("kind") for d in docs])
        self.assertIn("enable_tiered_cache", bundle)
        self.assertIn("guides.tiered_prefix_cache", bundle)

    def test_wide_ep_emits_advisory_not_kustomization(self) -> None:
        from autoconfig_poc import Features, render_bundle
        out = build_output(Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(enable_wide_ep=True),
        ))
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
        self.assertNotIn("Kustomization", [d.get("kind") for d in docs])
        self.assertIn("enable_wide_ep", bundle)
        self.assertIn("guides.wide_ep_lws", bundle)

    def test_no_feature_resources_when_no_phase_b_flags(self) -> None:
        from autoconfig_poc import render_bundle
        out = self._build()
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
        # No HF Secret (Q0.5 not answered), no Phase B features, no
        # modelserver scaffold (bundle is EPP + gateway only).
        kinds = [d.get("kind") for d in docs]
        self.assertEqual(kinds, ["Deployment"])

    def test_modelserver_intentionally_omitted_from_bundle(self) -> None:
        """Modelservers are intentionally OUT of the bundle (see render_bundle
        docstring + Phase 6.3 of the SKILL). modelserver_deploy_planned is
        consumed by Phase 3 (skips the schedulability audit when False) but
        does not affect bundle rendering. The bundle is EPP + gateway only;
        modelserver deploy is a separate `kubectl apply -k` step against the
        upstream overlay."""
        from autoconfig_poc import Context, render_bundle
        for planned in (True, False):
            with self.subTest(modelserver_deploy_planned=planned):
                out = build_output(Input(
                    model="some-model",
                    topology=Topology(mode="agg", replicas=2, tp=1),
                    context=Context(modelserver_deploy_planned=planned),
                ))
                runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
                bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
                import yaml as yaml_mod
                docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
                # No Kustomization resource pointing at any modelserver overlay
                kustomizations = [d for d in docs if d.get("kind") == "Kustomization"]
                modelserver_refs = [
                    k for k in kustomizations
                    if any("modelserver" in r for r in k.get("resources", []))
                ]
                self.assertEqual(modelserver_refs, [])

    def test_gateway_resource_emitted_when_provider_set(self) -> None:
        """Standalone+provider OR gateway-mode → bundle includes a Gateway
        named `llm-d-inference-gateway` so HTTPRoute's parentRefs binds."""
        from autoconfig_poc import Context, render_bundle
        for mode in ("standalone", "gateway"):
            with self.subTest(deploy_mode=mode):
                out = build_output(Input(
                    model="some-model",
                    topology=Topology(mode="agg", replicas=2, tp=1),
                    context=Context(
                        deploy_mode=mode,
                        gateway_provider="istio",
                    ),
                ))
                runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
                bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
                import yaml as yaml_mod
                docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
                gateways = [d for d in docs if d.get("kind") == "Gateway"]
                self.assertEqual(len(gateways), 1)
                gw = gateways[0]
                self.assertEqual(gw["metadata"]["name"], "llm-d-inference-gateway")
                self.assertEqual(gw["spec"]["gatewayClassName"], "istio")

    def test_gateway_class_name_maps_per_provider(self) -> None:
        """Each gateway_provider maps to the matching gatewayClassName."""
        from autoconfig_poc import Context, render_bundle
        cases = {
            "istio":                            "istio",
            "kgateway":                         "kgateway",
            "agentgateway":                     "agentgateway",
            "gke-l7-rilb":                      "gke-l7-rilb",
            "gke-l7-regional-external-managed": "gke-l7-regional-external-managed",
        }
        for provider, expected_class in cases.items():
            with self.subTest(provider=provider):
                out = build_output(Input(
                    model="some-model",
                    topology=Topology(mode="agg", replicas=2, tp=1),
                    context=Context(
                        deploy_mode="gateway",
                        gateway_provider=provider,
                    ),
                ))
                runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
                bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
                import yaml as yaml_mod
                docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
                gw = next(d for d in docs if d.get("kind") == "Gateway")
                self.assertEqual(gw["spec"]["gatewayClassName"], expected_class)

    def test_no_gateway_when_no_provider(self) -> None:
        """Pure standalone (no provider) doesn't need a Gateway."""
        from autoconfig_poc import render_bundle
        out = build_output(Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
        ))
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
        kinds = [d.get("kind") for d in docs]
        self.assertNotIn("Gateway", kinds)

    def test_hf_secret_scaffold_renders_when_opted_in(self) -> None:
        """Phase 2 Q0.5 = 'scaffold new' → context.hf_secret_name set to
        autoconfig's default name → scaffold rendered. Without the opt-in,
        no scaffold (prevents clobbering existing tokens on re-apply)."""
        from autoconfig_poc import Context, render_bundle
        out = build_output(Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            context=Context(hf_secret_name="llm-d-hf-token", hf_secret_exists=False),
        ))
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
        secret = next(d for d in docs if d.get("kind") == "Secret")
        self.assertEqual(secret["metadata"]["name"], "llm-d-hf-token")
        self.assertEqual(secret["stringData"], {"HF_TOKEN": ""})
        annotations = secret["metadata"]["annotations"]
        self.assertIn("llm-d.ai/scaffold", annotations)
        self.assertIn("HF_TOKEN", annotations["llm-d.ai/scaffold"])

    def test_hf_secret_scaffold_skipped_when_secret_already_exists(self) -> None:
        """Critical: Q0.5 = 'scaffold new' BUT Phase 1 found the secret
        already exists → render_bundle MUST NOT emit the scaffold (would
        clobber a real token with empty stringData on re-apply)."""
        from autoconfig_poc import Context, render_bundle
        out = build_output(Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            context=Context(hf_secret_name="llm-d-hf-token", hf_secret_exists=True),
        ))
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
        kinds = [d.get("kind") for d in docs]
        self.assertNotIn("Secret", kinds)

    def test_hf_secret_scaffold_skipped_when_user_picks_existing(self) -> None:
        """Q0.5 = 'use existing X' → hf_secret_name points at a
        non-default name → no scaffold rendered (we don't manage user's
        existing secrets)."""
        from autoconfig_poc import Context, render_bundle
        out = build_output(Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            context=Context(hf_secret_name="my-existing-hf-token", hf_secret_exists=True),
        ))
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
        kinds = [d.get("kind") for d in docs]
        self.assertNotIn("Secret", kinds)

    def test_namespace_emitted_for_non_default_namespace(self) -> None:
        from autoconfig_poc import render_bundle
        out = self._build(namespace="prod-chat", release_name="chat")
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
        kinds = [d.get("kind") for d in docs]
        self.assertEqual(kinds[0], "Namespace")  # First doc — created before secret
        ns = docs[0]
        self.assertEqual(ns["metadata"]["name"], "prod-chat")

    def test_no_namespace_doc_for_default_namespace(self) -> None:
        from autoconfig_poc import render_bundle
        out = self._build()  # default namespace
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
        kinds = [d.get("kind") for d in docs]
        self.assertNotIn("Namespace", kinds)

    def test_inference_objective_cr_when_flag_on(self) -> None:
        from autoconfig_poc import Features, render_bundle
        out = build_output(Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(enable_inference_objective=True),
            context=__import__("autoconfig_poc").Context(release_name="my-rel"),
        ))
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
        obj = next(d for d in docs if d.get("kind") == "InferenceObjective")
        self.assertEqual(obj["apiVersion"], "inference.networking.x-k8s.io/v1alpha2")
        self.assertEqual(obj["spec"]["poolRef"]["name"], "my-rel")
        # Priority default is 0 (user can edit later)
        self.assertEqual(obj["spec"]["priority"], 0)

    def test_model_rewrite_cr_when_flag_on(self) -> None:
        from autoconfig_poc import Features, render_bundle
        out = build_output(Input(
            model="meta-llama/Llama-3.1-8B-Instruct",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(enable_model_rewrite=True),
            context=__import__("autoconfig_poc").Context(release_name="my-rel"),
        ))
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\n")
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = [d for d in yaml_mod.safe_load_all(bundle) if isinstance(d, dict)]
        rewrite = next(d for d in docs if d.get("kind") == "InferenceModelRewrite")
        self.assertEqual(rewrite["apiVersion"], "inference.networking.x-k8s.io/v1alpha2")
        self.assertEqual(rewrite["spec"]["poolRef"]["name"], "my-rel")
        # Default rule rewrites all requests to the canonical model
        self.assertEqual(
            rewrite["spec"]["rules"][0]["targets"][0]["modelRewrite"],
            "meta-llama/Llama-3.1-8B-Instruct",
        )

    def test_bundle_is_valid_multi_document_yaml(self) -> None:
        from autoconfig_poc import render_bundle
        out = self._build(deploy_mode="standalone", gateway_provider="istio")
        runner, _ = self._fake_helm(
            "apiVersion: v1\nkind: Deployment\nmetadata:\n  name: epp\n"
            "---\napiVersion: v1\nkind: Service\nmetadata:\n  name: epp-svc\n",
        )
        bundle = render_bundle(out, subprocess_runner=runner, include_crds=False)
        import yaml as yaml_mod
        docs = list(yaml_mod.safe_load_all(bundle))
        # 2 from "helm" + 1 Gateway + 1 HTTPRoute = 4 (no Secret since no Q0.5 opt-in;
        # no modelserver scaffold since modelservers are out of bundle scope).
        self.assertEqual(len(docs), 4)


class PhaseCBundleCRDsTest(unittest.TestCase):
    """include_crds=True (default) prepends Gateway API + GIE CRDs to the
    bundle via `kubectl kustomize`. include_crds=False skips. Provider-
    specific CRDs (istio) only fire when gateway_provider matches.
    """

    def _fake_dual_runner(self, helm_stdout: str, kustomize_chunks: list[str]):
        """Runner that returns different stdout based on whether the call is
        helm or kubectl kustomize. Tracks the sequence of calls."""
        calls = []
        kustomize_iter = iter(kustomize_chunks)

        def runner(cmd, *, input=None, capture_output=False, text=False, check=False):
            import types
            calls.append(list(cmd))
            if cmd[0] in ("helm", "/usr/bin/helm") or "helm" in cmd[0]:
                return types.SimpleNamespace(stdout=helm_stdout, stderr="", returncode=0)
            # Assume kubectl kustomize
            try:
                stdout = next(kustomize_iter)
            except StopIteration:
                stdout = ""
            return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)
        return runner, calls

    def _build(self, **ctx_overrides):
        from autoconfig_poc import Context
        return build_output(Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            context=Context(**ctx_overrides),
        ))

    def test_default_fetches_gateway_api_and_gie_crds(self) -> None:
        from autoconfig_poc import render_bundle
        out = self._build()
        runner, calls = self._fake_dual_runner(
            "apiVersion: v1\nkind: Deployment\nmetadata:\n  name: epp\n",
            ["apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata:\n  name: httproutes.gateway.networking.k8s.io\n",
             "apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata:\n  name: inferencepools.inference.networking.x-k8s.io\n"],
        )
        bundle = render_bundle(out, subprocess_runner=runner)
        # Two kustomize calls (gateway-api + gie) then one helm call
        kustomize_calls = [c for c in calls if c[0:2] == ["kubectl", "kustomize"]]
        self.assertEqual(len(kustomize_calls), 2)
        sources = [c[2] for c in kustomize_calls]
        self.assertTrue(any("crds-gateway-api" in s for s in sources))
        self.assertTrue(any("crds-gie" in s for s in sources))
        # CRDs appear in the bundle, before the Deployment
        self.assertLess(bundle.find("CustomResourceDefinition"), bundle.find("Deployment"))

    def test_istio_gateway_adds_istio_crds(self) -> None:
        from autoconfig_poc import render_bundle
        out = self._build(deploy_mode="gateway", gateway_provider="istio")
        runner, calls = self._fake_dual_runner(
            "apiVersion: v1\nkind: Deployment\nmetadata:\n  name: epp\n",
            ["kind: CustomResourceDefinition\n"] * 3,
        )
        render_bundle(out, subprocess_runner=runner)
        kustomize_calls = [c for c in calls if c[0:2] == ["kubectl", "kustomize"]]
        self.assertEqual(len(kustomize_calls), 3)  # gateway-api + gie + istio
        sources = [c[2] for c in kustomize_calls]
        self.assertTrue(any("crds-istio" in s for s in sources))

    def test_non_istio_gateway_skips_istio_crds(self) -> None:
        from autoconfig_poc import render_bundle
        out = self._build(deploy_mode="gateway", gateway_provider="gke-l7-rilb")
        runner, calls = self._fake_dual_runner(
            "apiVersion: v1\nkind: Deployment\nmetadata:\n  name: epp\n",
            ["kind: CustomResourceDefinition\n"] * 2,
        )
        render_bundle(out, subprocess_runner=runner)
        kustomize_calls = [c for c in calls if c[0:2] == ["kubectl", "kustomize"]]
        self.assertEqual(len(kustomize_calls), 2)  # gateway-api + gie only
        sources = [c[2] for c in kustomize_calls]
        self.assertFalse(any("crds-istio" in s for s in sources))

    def test_no_crds_skips_all_kustomize(self) -> None:
        from autoconfig_poc import render_bundle
        out = self._build()
        runner, calls = self._fake_dual_runner("apiVersion: v1\nkind: Deployment\n", [])
        render_bundle(out, subprocess_runner=runner, include_crds=False)
        kustomize_calls = [c for c in calls if c[0:2] == ["kubectl", "kustomize"]]
        self.assertEqual(len(kustomize_calls), 0)

    def test_crd_filenames_sort_first_in_dir_mode(self) -> None:
        from autoconfig_poc import render_bundle_dir
        import tempfile
        from pathlib import Path
        out = self._build()
        runner, _ = self._fake_dual_runner(
            "apiVersion: v1\nkind: Deployment\nmetadata:\n  name: epp\n",
            ["apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata:\n  name: httproutes.gateway.networking.k8s.io\n",
             "apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata:\n  name: inferencepools.inference.networking.x-k8s.io\n"],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            created = render_bundle_dir(
                out, parent_dir=Path(tmpdir),
                timestamp="20260519T120000",
                subprocess_runner=runner,
            )
            files = sorted(f.name for f in created.glob("*.yaml"))
            # CRDs sort to rank 0 (`00-*-customresourcedefinition-*.yaml`),
            # before the namespace/secret/etc.
            self.assertTrue(files[0].startswith("00-"))
            self.assertIn("customresourcedefinition", files[0])
            self.assertIn("customresourcedefinition", files[1])


class PhaseCBundleDirTest(unittest.TestCase):
    """render_bundle_dir() splits the bundle into one file per resource
    inside a timestamped sub-directory + a README.md."""

    def _fake_helm(self, stdout: str):
        captured = {}
        def runner(cmd, *, input=None, capture_output=False, text=False, check=False):
            import types
            captured["cmd"] = cmd
            captured["input"] = input
            return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)
        return runner, captured

    def _build_out(self, **ctx_overrides):
        from autoconfig_poc import Context
        return build_output(Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            context=Context(**ctx_overrides),
        ))

    def test_creates_timestamped_subdir(self) -> None:
        import tempfile
        from pathlib import Path
        from autoconfig_poc import render_bundle_dir
        out = self._build_out()
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\nmetadata:\n  name: epp\n")
        with tempfile.TemporaryDirectory() as tmpdir:
            created = render_bundle_dir(
                out, parent_dir=Path(tmpdir), include_crds=False,
                timestamp="20260519T120000",
                subprocess_runner=runner,
            )
            self.assertTrue(created.exists())
            self.assertEqual(created.name, "autoconfig-20260519T120000")
            self.assertEqual(created.parent, Path(tmpdir))

    def test_one_file_per_resource(self) -> None:
        import tempfile
        from pathlib import Path
        from autoconfig_poc import render_bundle_dir
        # Opt into HF Secret scaffold so this test covers the full prereq set.
        out = self._build_out(namespace="prod-chat", release_name="chat",
                              deploy_mode="standalone", gateway_provider="istio",
                              hf_secret_name="llm-d-hf-token", hf_secret_exists=False)
        runner, _ = self._fake_helm(
            "apiVersion: v1\nkind: Deployment\nmetadata:\n  name: epp\n"
            "---\napiVersion: v1\nkind: Service\nmetadata:\n  name: epp-svc\n",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            created = render_bundle_dir(
                out, parent_dir=Path(tmpdir), include_crds=False,
                timestamp="20260519T120000",
                subprocess_runner=runner,
            )
            yaml_files = sorted(created.glob("*.yaml"))
            kinds = []
            import yaml as yaml_mod
            for f in yaml_files:
                doc = yaml_mod.safe_load(f.read_text())
                kinds.append(doc["kind"])
            # Prereqs (Namespace, Secret) + 2 from "helm" + 1 Gateway + 1 HTTPRoute = 6 docs.
            # Alphabetical filename sort: Namespace(05), Secret(07), Service(20),
            # Deployment(30), Gateway(45), HTTPRoute(50).
            self.assertEqual(len(yaml_files), 6)
            self.assertEqual(kinds, ["Namespace", "Secret", "Service", "Deployment", "Gateway", "HTTPRoute"])

    def test_filenames_are_apply_ordered(self) -> None:
        import tempfile
        from pathlib import Path
        from autoconfig_poc import render_bundle_dir
        # Opt into HF Secret scaffold so the Secret appears for this ordering test.
        out = self._build_out(namespace="prod-chat",
                              hf_secret_name="llm-d-hf-token", hf_secret_exists=False)
        runner, _ = self._fake_helm(
            "apiVersion: v1\nkind: Deployment\nmetadata:\n  name: a\n"
            "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: b\n"
            "---\napiVersion: v1\nkind: Service\nmetadata:\n  name: c\n",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            created = render_bundle_dir(
                out, parent_dir=Path(tmpdir), include_crds=False,
                timestamp="20260519T120000",
                subprocess_runner=runner,
            )
            sorted_files = sorted(f.name for f in created.glob("*.yaml"))
            # Alphabetical sort (== kubectl apply order) must produce kind order:
            # Namespace (05), Secret (07), ConfigMap (10), Service (20), Deployment (30).
            names_by_kind = {kind: name for name in sorted_files
                             for kind in ("namespace", "secret", "configmap", "service", "deployment")
                             if f"-{kind}-" in name}
            order = [names_by_kind[k] for k in ("namespace", "secret", "configmap", "service", "deployment")]
            self.assertEqual(order, sorted(order))

    def test_readme_emitted_with_metadata(self) -> None:
        import tempfile
        from pathlib import Path
        from autoconfig_poc import render_bundle_dir
        out = self._build_out(namespace="prod-chat", release_name="chat")
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\nmetadata:\n  name: epp\n")
        with tempfile.TemporaryDirectory() as tmpdir:
            created = render_bundle_dir(
                out, parent_dir=Path(tmpdir), include_crds=False,
                timestamp="20260519T120000",
                subprocess_runner=runner,
            )
            readme = (created / "README.md").read_text()
            self.assertIn("Generated: `20260519T120000`", readme)
            self.assertIn(f"Input hash: `{out.input_hash}`", readme)
            self.assertIn("Namespace: `prod-chat`", readme)
            self.assertIn("kubectl apply -f .", readme)
            # The HF secret edit warning is always emitted (always-on prereq).
            self.assertIn("HF token", readme)

    def test_scaffold_notes_filter_excludes_helm_source_comments(self) -> None:
        """Helm template injects `# Source: <chart>/templates/...` headers
        between docs. Those should NOT appear in the README's scaffold-notes
        section — only OUR scaffold blocks (with feature_docs.yaml refs)
        belong there."""
        import tempfile
        from pathlib import Path
        from autoconfig_poc import render_bundle_dir
        out = self._build_out()
        runner, _ = self._fake_helm(
            "# Source: standalone/templates/inferenceextension.yaml\n"
            "apiVersion: v1\nkind: Deployment\nmetadata:\n  name: epp\n"
            "---\n# Source: standalone/templates/rbac.yaml\n"
            "apiVersion: v1\nkind: Service\nmetadata:\n  name: epp-svc\n",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            created = render_bundle_dir(
                out, parent_dir=Path(tmpdir), include_crds=False,
                timestamp="20260519T120000",
                subprocess_runner=runner,
            )
            readme = (created / "README.md").read_text()
            self.assertNotIn("inferenceextension.yaml", readme)
            self.assertNotIn("rbac.yaml", readme)

    def test_scaffold_notes_kept_when_phase_b_flag_on(self) -> None:
        from autoconfig_poc import Features, render_bundle_dir
        import tempfile
        from pathlib import Path
        # batch serving pattern emits a comment-only scaffold (no K8s resource)
        out = build_output(Input(
            model="some-model",
            topology=Topology(mode="agg", replicas=2, tp=1),
            features=Features(serving_pattern="batch"),
        ))
        runner, _ = self._fake_helm("apiVersion: v1\nkind: Deployment\nmetadata:\n  name: epp\n")
        with tempfile.TemporaryDirectory() as tmpdir:
            created = render_bundle_dir(
                out, parent_dir=Path(tmpdir), include_crds=False,
                timestamp="20260519T120000",
                subprocess_runner=runner,
            )
            readme = (created / "README.md").read_text()
            self.assertIn("Phase B feature notes", readme)
            self.assertIn("guides.batch_gateway", readme)




if __name__ == "__main__":
    unittest.main()

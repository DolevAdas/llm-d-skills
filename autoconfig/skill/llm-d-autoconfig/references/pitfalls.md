# Pitfalls KB (apply-step error catalog)

*Reference for SKILL.md Phase 6 / Phase 7 error handling. Each entry: symptom → diagnosis → fix. Search before retrying any failed kubectl/helm command.*

When an apply step fails or post-deploy validation shows an issue, pattern-match against these. If matched, narrate the diagnosis and offered fix; do not auto-execute the fix.

## First check the shared deploy troubleshooting guide

**Generic Kubernetes / llm-d deploy + runtime failures are catalogued once in the `deploy-llm-d` skill, not duplicated here.** Before scanning this file, consult the shared troubleshooting guide for the common cases:

- **In-repo:** [`skills/deploy-llm-d/references/troubleshooting.md`](../../../../skills/deploy-llm-d/references/troubleshooting.md)
- **URL (when this skill was copied out standalone):** https://github.com/llm-d-incubation/llm-d-skills/blob/main/skills/deploy-llm-d/references/troubleshooting.md

That guide covers, among others: HuggingFace token / auth failures, pod `OOMKilled`, `ImagePullBackOff`, RBAC-denied applies, namespace mismatches, pod scheduling / GPU-allocation problems, pod explosion from missing kustomize `replicas`, generic Gateway/HTTPRoute routing failures, and the accelerator-family modelserver overlay layout. **This file keeps only the autoconfig-specific entries** — EPP config, EPP gateway-mode routing (istio / GAIE), PD/NIXL, the modelserver install path, and the benchmark harness — that the shared guide does not cover.

---

## Autoconfig-specific entries

### ConfigMap not mounted in EPP pod
- **Symptom:** EPP pod logs show `failed to load config: file not found` or similar
- **Diagnosis:** `kubectl exec <epp-pod> -- ls /config` shows missing files; OR ConfigMap exists but volumeMount points elsewhere
- **Fix:** check `pluginsConfigFile` value in gaie chart values matches the ConfigMap key; check volumeMount path matches what EPP expects

### precise-prefix-cache silent miss
- **Symptom:** EPP deployed; metrics show prefix-cache hit rate near 0 even for repeated identical prompts
- **Diagnosis:** vLLM's `PYTHONHASHSEED` doesn't match the value in EPP config, OR `--block-size` doesn't match
- **Fix:** verify both values in vLLM pod env (`kubectl get pod <vllm-pod> -o yaml | grep -A2 PYTHONHASHSEED`) and in EPP config; redeploy EPP if mismatched

### Gateway resource never `PROGRAMMED=True`
- **Symptom:** `kubectl get gateway <name> -n <ns>` shows `PROGRAMMED: False` indefinitely (or `kubectl wait --for=condition=Programmed` times out at 180s). The `Gateway` resource exists but no controller has provisioned the data plane.
- **Diagnosis:** check what controller should be handling the GatewayClass and whether it's actually running:
  ```bash
  kubectl get gateway <name> -n <ns> -o jsonpath='{.spec.gatewayClassName}'
  kubectl get gatewayclass <class-name> -o yaml | grep -E "controllerName|status:"
  ```
  Common causes:
  - **istiod not installed**: Gateway with `gatewayClassName: istio` requires `istiod` running. Search ALL namespaces (revised installs may use a non-standard ns like `llm-d-istio-system`): `kubectl get pods -A -l app=istiod`. If empty, return to Phase 6.1's istio install path.
  - **istio revision mismatch**: istiod was installed with a revision (e.g. `istio.io/rev=llm-d-gateway`) but the Gateway resource isn't labeled with the matching revision. Revised istiod controllers ignore unlabeled Gateways. Check: `kubectl get pods -A -l app=istiod -o jsonpath='{.items[*].metadata.labels.istio\.io/rev}'` — if non-empty, the Gateway needs `istio.io/rev=<rev>` label. Fix: `kubectl label gateway llm-d-inference-gateway -n <ns> istio.io/rev=<rev> --overwrite`.
  - **GKE Gateway API not enabled**: GKE-managed classes (`gke-l7-rilb`, `gke-l7-regional-external-managed`) need the cluster-level Gateway API feature enabled via `gcloud container clusters update <cluster> --gateway-api=standard`.
  - **`provider.name` mismatch**: helm install used `--set provider.name=istio` but the actual GatewayClass on the cluster is `kgateway` or `gke-l7-rilb`. The Gateway resource is created with the wrong `gatewayClassName` and no controller picks it up.
  - **Controller pods are crashlooping**: `kubectl get pods -A | grep -E "(istio|kgateway|agentgateway)"` shows pods in CrashLoopBackOff or Pending.
- **Fix:** depends on cause — see Phase 6.1's BLOCKING istio install path for missing istiod; for revision mismatch, run the `kubectl label` above; for GKE, run the `--gateway-api=standard` cluster update; for provider mismatch, return to Phase 6.4 with the corrected value. Don't hand-edit the Gateway's `gatewayClassName` — re-run the helm install with the right `--set provider.name=...`.

### Istio Gateway accepted requests but `InferencePool` routing silently fails
- **Symptom:** `Gateway` shows `PROGRAMMED=True`, `HTTPRoute` shows `Accepted=True`, the smoke test curl gets a non-error HTTP response — but it's a 404 / 502 / "no upstream" rather than a real model response. Or requests just hang.
- **Diagnosis:** istio is installed but missing the GAIE inference-extension feature flag. Check istiod's env (search ALL namespaces; revised installs may not be in `istio-system`):
  ```bash
  kubectl get pods -A -l app=istiod -o jsonpath='{.items[*].spec.containers[0].env}' | grep -o 'ENABLE_GATEWAY_API_INFERENCE_EXTENSION[^}]*'
  ```
  If empty or `value:false`, that's the bug — istio's Envoy proxy doesn't know how to translate `kind: InferencePool` backendRefs into the ext_proc-based routing the EPP expects.
- **Fix:** reinstall istiod with the flag (non-disruptive — istioctl reconciles in place):
  ```bash
  istioctl install -y --set values.pilot.env.ENABLE_GATEWAY_API_INFERENCE_EXTENSION=true
  ```

### Helm chart pull fails with 403 (`llm-d-incubation/llm-d-modelservice`)
- **Symptom:** `helm install ms-... oci://ghcr.io/llm-d-incubation/llm-d-modelservice/...` returns `unexpected status code 403: denied: requested access to the resource is denied`
- **Diagnosis:** the modelserver is NOT installed via Helm in the current optimized-baseline guide. The `llm-d-modelservice` Helm chart exists as a separate project but isn't the canonical install path. The current guide uses `kubectl apply -k` against Kustomize overlays (see Phase 6.3).
- **Fix:** abandon the Helm install. Re-run Phase 6.3 using `kubectl apply -k "https://github.com/llm-d/llm-d.git/guides/optimized-baseline/modelserver/<accelerator>/vllm/?ref=main"` — kubectl's `-k` flag accepts public git URLs and clones into a temporary working directory; no local repo needed. (See the shared deploy troubleshooting for the accelerator-FAMILY overlay layout — H100/H200/A100/B200 all share `gpu/vllm/`.)

### NIXL silent TCP fallback (PD)
- **Symptom:** PD prefill + decode pods start cleanly; throughput is worse than agg on the same hardware. EPP routes correctly, but inter-pod latency is dominated by KV transfer time.
- **Diagnosis:** the cluster has no RDMA/RoCE wired (no DPv2, no `Network` CRs, no `rdma/ib` resource on GPU nodes). NIXL falls back to TCP for KV transfer over the standard pod network — orders of magnitude slower than RoCE/Infiniband.
- **Fix:** acceptable if you knew this going in (the autoconfig script's TCP-transport warning surfaced it in Phase 5). For RDMA, recreate the cluster with DPv2 + multi-networking + `--additional-node-network` flags (see GCP "AI Hypercompute custom cluster" docs); the gke/ overlay is then sufficient. On GKE, the [`create-gke-infra-llm-d` skill](../../../../skills/create-gke-infra-llm-d/) automates this cluster recreation (both the DRA/DRANET and the multi-networking paths).

### gIB NCCL tuner crash on GKE (PD)
- **Symptom:** prefill or decode pod CrashLoopBackOff right after vLLM starts. Logs show `NCCL WARN No NCCL_TUNER_CONFIG_PATH provided` followed by `NCCL error: internal error - please report this issue to the NCCL developers`.
- **Diagnosis:** the cluster has Google Infiniband (gIB) installed (`ls /home/kubernetes/bin/gib` on a node returns non-empty) but the model server hasn't been told to skip the gIB tuner plugin.
- **Fix:** the `pd-disaggregation/modelserver/gpu/vllm/gke/` kustomize overlay (the one Phase 6.3's PD step picks) automatically adds `NCCL_TUNER_PLUGIN=none` + `NCCL_NET_PLUGIN=""`. If you used `gpu/vllm/base/` directly, switch to the gke/ overlay. If you can't, add the env vars manually to both Deployments.

### `inference-perf` rejects nested config ("model server client config missing")
- **Symptom:** the benchmark Job pod logs `Exception: model server client config missing` and exits non-zero. `kubectl describe pod` shows a clean image pull and start.
- **Diagnosis:** something gave inference-perf a wrapper-style config with `endpoint` / `control` / `harness` / `workload.<name>` keys at the top, instead of the native flat schema (`load`, `api`, `server`, `tokenizer`, `data`, `report`, `storage` at top level). The native binary doesn't unwrap.
- **Fix:** the autoconfig script handles this automatically — inference-perf gets the native flat schema + the native `quay.io/inference-perf/inference-perf` image (NOT the llm-d-benchmark wrapper). If you see this with a hand-written config, flatten it: drop the wrapper keys and put the workload's children at the top level. Reference: https://github.com/kubernetes-sigs/inference-perf/blob/main/config.yml.

### `kubectl logs / wait` fails immediately after `kubectl apply` ("at least one resource must be specified")
- **Symptom:** `kubectl wait` or `kubectl logs` invoked right after `kubectl apply -f bench-deployment.yaml` returns `error: at least one resource must be specified to use a selector`. The Job exists, but no Pod yet.
- **Diagnosis:** the Job controller takes 1-5 seconds to materialize a Pod for a freshly-created Job. Selector-based commands fail because no pod matches the label yet.
- **Fix:** add the wait-for-pod loop from Phase 7.3 between apply and wait/logs. Don't retry the same kubectl call without checking — the issue is timing, not flag syntax.

### NIXL_SIDE_CHANNEL_HOST not set (PD)
- **Symptom:** prefill pod CrashLoopBackOff at NIXL init; logs reference `VLLM_NIXL_SIDE_CHANNEL_HOST` or "side channel host required" / "no peer address".
- **Diagnosis:** vLLM with `--kv-transfer-config '{"kv_connector":"NixlConnector","kv_role":"kv_both"}'` requires the env var `VLLM_NIXL_SIDE_CHANNEL_HOST` set to the pod's IP. The PD base kustomization sets this from `status.podIP` via `valueFrom.fieldRef`. If a custom patch removed the env vars list (rather than appending), the var is missing.
- **Fix:** ensure the env var is present on both the prefill and decode containers. Inspect with `kubectl get deployment <pd-prefill> -o yaml | grep -A4 VLLM_NIXL_SIDE_CHANNEL_HOST`. If absent, re-apply the base PD overlay (it sets this) or patch it back in.

For anything not in this list, check the shared deploy troubleshooting guide (linked above); if it's not there either, surface the raw error and ask the user if they want to investigate further.

---

## Caveats to always surface

- This is a POC v0.2. Three workload classes; agg AND PD topologies; no WVA / tiered cache.
- PD support: TCP-fallback path is end-to-end validated. The RDMA path is plumbed in the codegen + skill but only tested against the PD guide's published recipe — it has not been validated against a freshly-set-up RDMA cluster yet.
- The recommendation matches published llm-d guides where applicable (optimized-baseline for agg, pd-disaggregation for PD); computed parameters use math derivations; defaults come from plugin source. We don't make up values.
- Apply step has safety rails but is not battle-tested. For production, review each step.
- For the full coverage matrix (which guides are/aren't supported, gaps in each, recommended next-steps), see `docs/SUPPORT.md` in the autoconfig directory on GitHub. The POC validates the architecture; the MVP adds coverage and benchmarks.

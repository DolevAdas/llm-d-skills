# Phase 1 — Cluster discovery (read-only)

*Detailed runbook for SKILL.md Phase 1. Read this when starting cluster discovery; the SKILL.md overview only gives the one-line summary.*


Ask the user (yesno):

```json
[{"header": "kubectl access", "question": "Do you have kubectl access to the target cluster?", "type": "yesno"}]
```

If "no", skip this phase entirely and rely on user answers in Phase 2.

If "yes", run these read-only commands BEFORE Phase 2:

```bash
kubectl config current-context
kubectl get nodes -o json | jq '.items[].status.allocatable | with_entries(select(.key | test("nvidia|gpu")))'
# Per-node CPU + RAM allocatable — required input to Phase 3's schedulability audit.
kubectl get nodes -o json | jq '.items[] | {name: .metadata.name, cpu: .status.allocatable.cpu, memory: .status.allocatable.memory, gpu: .status.allocatable["nvidia.com/gpu"]}'
# CRD detection — records CRDS_INSTALLED for Phase 4 Step 2's bundle CRD question.
# Look specifically for inferencepools.inference.networking.x-k8s.io (the GIE
# CRD that the bundle's InferencePool resource depends on). If present →
# CRDS_INSTALLED=true (informs Phase 4's question default). If absent →
# CRDS_INSTALLED=false (greenfield default, include CRDs in bundle).
kubectl get crd inferencepools.inference.networking.x-k8s.io -o name 2>/dev/null && echo "CRDS_INSTALLED=true" || echo "CRDS_INSTALLED=false"
kubectl get crd | grep -E 'inference|variantautoscaling' || true
kubectl get gatewayclass -o name
kubectl get gateway -A -o name
kubectl get configmap -A | grep -i epp || true
```

**Model server discovery — try multiple paths**, since users don't always label their pods. Run all three; union the results:

```bash
# Path 1: llm-d standard label (most reliable when present)
kubectl get deployment -A -l llm-d.ai/inference-serving=true -o name 2>/dev/null

# Path 2: image-based — find Deployments running vLLM, SGLang, or TGI containers
kubectl get deployment -A -o json | jq -r '.items[] | select(.spec.template.spec.containers[]?.image | test("vllm|sglang|tgi"; "i")) | "\(.metadata.namespace)/\(.metadata.name)"'

# Path 3: container-name-based — common conventions (`vllm`, `inference-server`, etc.)
kubectl get deployment -A -o json | jq -r '.items[] | select(.spec.template.spec.containers[]?.name | test("vllm|sglang|inference"; "i")) | "\(.metadata.namespace)/\(.metadata.name)"'
```

If all three return empty, treat as greenfield (no existing model servers). If any return results, dedupe and treat each unique deployment as a candidate for Q0.

Extract:
- GPU type + count per node
- **GatewayClass present?** vs **Gateway resource present?** Both matter and they're different things. A GatewayClass without a Gateway means a provider is installed but no usable Gateway has been deployed yet — so the only viable deploy mode is standalone (or the user installs a Gateway first). Don't conflate the two.
- **Gateway controller readiness.** A GatewayClass entry alone doesn't prove the controller can actually provision a Gateway — the CRDs + control plane (`istiod`) can be present while the actual ingress proxy Deployment is missing. Verify the controller's data-plane pods are running:
  ```bash
  # Istio: ingress gateway pods (provisioned per Gateway resource on demand
  # by the new Gateway API, OR pre-existing if using the legacy install)
  kubectl get pods -A -l app=istio-ingressgateway --no-headers 2>&1 | head -3
  kubectl get pods -A -l istio=ingressgateway --no-headers 2>&1 | head -3
  # Kgateway:
  kubectl get pods -A -l app.kubernetes.io/name=kgateway --no-headers 2>&1 | head -3
  # GKE-managed (gke-l7-rilb / gke-l7-gxlb): controller is in-cluster GKE
  # infrastructure, no app-pod check needed; the GKE control plane handles it.
  ```
  If a GatewayClass is `istio` or `kgateway` but the corresponding pods aren't running → mark as "**non-functional GatewayClass**". Don't recommend it as a deploy target. Surface to user: "GatewayClass `<X>` is registered but its controller has no pods running — choose a different gateway provider or fall back to standalone."
- Existing EPP ConfigMaps + the namespaces they live in
- Model server pods/deployments and their namespaces

**Orphaned ConfigMap detection — BLOCKING ASK.** If you find an EPP ConfigMap in namespace X but no model server deployments in X (after the broader discovery above), the install is likely orphaned. **Do NOT proceed to Phase 2** until the user answers:

> "I found EPP ConfigMaps in namespace `<X>` but no model server pods there. They're either:
> (a) stale leftovers from a previous deploy I can ignore
> (b) the active install, with model servers in a different namespace I haven't located yet
> (c) something else — you tell me
>
> Which is it?"

If (b), ask which namespace the model servers are in and run the broader discovery against that namespace before continuing. If (a), record the orphans as "to be cleaned up later" but don't let them mislead Phase 2's namespace defaults.

This is blocking because skipping the question leads to a documented failure mode: agent assumes the orphaned-CM namespace is active, hunts for nonexistent pods there, then deploys into the wrong namespace.

**Per-deployment value extraction** (only if you found model server deployments). Pull every value Section 1 (Model & Topology) would ask the user for. Run for each model server deployment:

```bash
# Get the full deployment spec for arg extraction
kubectl get deployment <ms-deploy> -n <ns> -o yaml
```

From the YAML, extract the fields below. vLLM accepts args in **four** different shapes — your parser MUST handle all of them or you'll silently miss values:

| Arg shape | Example | Common for |
|---|---|---|
| `--flag <value>` (space-separated) | `--model Qwen/Qwen3-32B` | older vLLM invocations |
| `--flag=<value>` (equals-joined) | `--tensor-parallel-size=8` | modern vLLM / Helm-rendered |
| Positional first arg (the model) | `["Qwen/Qwen3-32B", "--tensor-parallel-size=8", ...]` | vllm-openai container default |
| Env var | `MODEL_NAME=Qwen/Qwen3-32B` in `env[]` | some serving stacks |

Fields to extract:

- **Model HF ID** — try in order: (a) `--model <id>` or `--model=<id>` flag; (b) the first positional arg in `args[]` (if it doesn't start with `-`, it's the model — this is the `vllm serve` convention); (c) `MODEL_NAME` env var. If none match, the model is unknown — ask the user in Phase 2 Q1 without a default.
- **Tensor parallelism** — `--tensor-parallel-size <N>` or `--tensor-parallel-size=<N>`. Default 1 if absent.
- **Replicas** — `spec.replicas`.
- **GPUs per replica** — `spec.template.spec.containers[0].resources.limits["nvidia.com/gpu"]`.
- **Max model context length** — `--max-model-len <N>` or `--max-model-len=<N>` if present. If absent, fall back to the HF `config.json` fetch (next block) — do NOT default silently.
- **Block size** — `--block-size <N>` or `--block-size=<N>` if present.
- **PYTHONHASHSEED** — from the env vars list (not the args list — it's an env var, not a CLI flag).
- **max-num-seqs** — `--max-num-seqs <N>` or `--max-num-seqs=<N>` if present.
- **KV-events configured** — look for `--kv-events-config` in args (any form). Presence indicates the modelserver can feed `precise-prefix-cache-scorer`; absence means Phase 2.5 should NOT recommend that scorer.

A `jq` one-liner that handles all four arg shapes for the model:

```bash
kubectl get deployment <ms-deploy> -n <ns> -o json | jq -r '
  .spec.template.spec.containers[0] as $c |
  ([$c.args[]?] + [$c.env[]? | select(.name=="MODEL_NAME") | .value]) as $candidates |
  ($c.args[]? | select(. | startswith("--model=")) | sub("^--model="; "")) //
  ($c.args | to_entries[] | select(.value=="--model") | $c.args[.key+1]) //
  ($c.args[0] // "") |
  if startswith("-") then "UNKNOWN" else . end
'
```

Record per-deployment so Phase 2 can use the values directly. If you find multiple model server deployments (different models or different namespaces), list them all; Phase 2 will ask the user which one autoconfig is targeting.

**Model context length fetch (mandatory when model ID is known).** As soon as you know the model HF ID (either from cluster discovery here, or from the user's first answer in Phase 2), fetch the model's `config.json` from Hugging Face and record `max_position_embeddings` as the context length. Do this BEFORE asking the user any context-length question — the answer is in `config.json` for any public model and saves the user from looking it up.

```bash
curl -fsSL "https://huggingface.co/<model-id>/raw/main/config.json" 2>/dev/null | jq -r '.max_position_embeddings // empty'
```

If the curl returns a number, use it as Q5's default in Phase 2 (the user can override). If it 404s (private/gated model) or returns empty, leave context length unknown and let Q5 fall through to asking the user.

**Record the curl outcome.** If `config.json` returned a 200/numeric value, mark the model as findable on HF (no Phase 2 Q5.5 needed). If it 404'd, mark the model as NOT-on-HF — Phase 2 Q5.5 (tokenizer fallback for the benchmark) will fire to ask for a public tokenizer override.

**HF token Secret enumeration (mandatory before Phase 2 Q0.5).** Phase 2 Q0.5 asks the user which HF token Secret autoconfig should reference. Enumerate plausible candidates in the target namespace so the question can present them as picklist options rather than free-form text. Use a best-effort filter on the name; never hardcode meaning into any specific name.

```bash
# List Secrets whose name looks HF-related. Best-effort filter — autoconfig
# does NOT bake convention into any name beyond its own scaffold default
# (`llm-d-hf-token`). The user picks which one (or "scaffold new" or "skip")
# at Q0.5.
kubectl get secret -n <ns> -o name 2>/dev/null | grep -iE '/(hf|hugging)' | sed 's|secret/||'
```

Also record specifically whether a Secret literally named `llm-d-hf-token` already exists in the target namespace — this becomes `context.hf_secret_exists` in the input JSON and prevents render_bundle from clobbering a real token with an empty scaffold on re-apply:

```bash
kubectl get secret llm-d-hf-token -n <ns> -o name 2>&1 | head -1
# Returns the secret name if it exists; "Error from server (NotFound)" otherwise.
```

Carry both findings into Phase 2 Q0.5:
- The list of discovered candidate Secret names → become picklist options
- The `llm-d-hf-token`-exists boolean → constrains the "scaffold new" option (if already present, the question explains "scaffold would skip render to avoid clobbering — autoconfig will reference the existing Secret instead")

**autoTune metric check.** Prefer `kubectl exec` over `kubectl port-forward` — port-forwarding is brittle in agent runtimes (background processes, race conditions on the curl):

```bash
kubectl exec -n <ns> <vllm-pod> -- curl -s http://localhost:8000/metrics 2>/dev/null | grep '^vllm:cache_config_info'
# Expected: vllm:cache_config_info{block_size="16",num_gpu_blocks="12345"} 1
```

If the metric exists → `features.autotune_supported: true` (default) in Phase 2 inputs. If absent → `false`, and use Phase 2's autoTune-fallback questions to gather replacement values.

**Do NOT pre-check for latency-predictor sidecars here.** Latency-predictor has no cluster-side prereqs — the chart toggle deploys everything. Pre-checking confuses the flow on greenfield clusters.

**RDMA capability sweep (mandatory).** Determine whether the cluster supports PD-with-RDMA so Phase 2 can answer the user's PD question with the right transport. The transport choice has no follow-up questions for the user — it's set automatically by what these checks find. Run all three; record the boolean `RDMA_AVAILABLE = (DPv2) AND (multi-net) AND (per-pod RDMA resource)`:

```bash
# Check 1: Dataplane V2 (the eBPF dataplane). Required for multi-networking.
# DPv2 cannot be enabled in-place — it's set at cluster create time. If
# anetd/cilium is absent and only netd is running, the cluster is on the
# legacy dataplane and PD-with-RDMA is impossible without recreating the cluster.
kubectl get pods -n kube-system -o name 2>/dev/null | grep -E '^pod/(anetd|cilium)' | head -1 || echo "NO_DPV2"

# Check 2: GKE multi-networking CRDs (Network + GKENetworkParamSet) are
# present, AND at least one Network CR is configured. CRDs alone don't prove
# the cluster has RDMA networks attached.
kubectl api-resources --api-group=networking.gke.io 2>/dev/null | grep -wE '(networks|gkenetworkparamsets)' | head -2
kubectl get networks -o name 2>/dev/null | head -3 || echo "NO_NETWORKS"

# Check 3: a GPU node exposes either GKE-style RDMA NIC resources OR coreweave-
# style rdma/ib. Pick any one node with cloud.google.com/gke-accelerator set.
kubectl get nodes -l 'cloud.google.com/gke-accelerator' -o jsonpath='{.items[0].status.allocatable}' 2>/dev/null | python3 -c "import json,sys; d=json.loads(sys.stdin.read() or '{}'); print('RDMA_RESOURCES:', [k for k in d if 'rdma' in k.lower() or 'networking.gke.io' in k] or 'NONE')"
```

`RDMA_AVAILABLE = True` only when all three return positive (DPv2 pods present + at least one Network CR + at least one rdma/ib or `networking.gke.io.networks/...IP` resource on a GPU node). Anything missing → `RDMA_AVAILABLE = False`. Record as a single boolean for Phase 2's PD question.

**Surface in the discovery summary** (one line):

> "RDMA-capable: yes" — when all three checks pass (PD-with-RDMA can be deployed).
> "RDMA-capable: no — missing DPv2" / "no — missing multi-net Network CRs" / "no — no rdma/ib resource on GPU nodes" — name the specific gap so the user knows what would need to change.

The user is NEVER asked which transport to use. PD opt-in is yes/no; transport is implied: `rdma` if `RDMA_AVAILABLE`, else `tcp`. If the user picks PD on a non-RDMA cluster, the script's TCP-fallback warning will surface in Phase 5.

**GKE Gateway prereqs sweep (mandatory on GKE clusters; skip on non-GKE).** Q8.5 (gateway provider choice) needs accurate "ready / needs install" status to render its option descriptions. Without these checks, the agent guesses.

```bash
# Check 1: GKE Gateway API enabled? Look for the gke-managed GatewayClasses.
# If `kubectl get gatewayclass` shows entries with controllerName starting
# with `networking.gke.io/`, the Gateway API is enabled on the cluster.
kubectl get gatewayclass -o jsonpath='{range .items[*]}{.metadata.name}{" -> "}{.spec.controllerName}{"\n"}{end}' 2>&1 | grep "networking.gke.io" || echo "GKE_GATEWAY_API_NOT_ENABLED"

# Check 2: Proxy-only subnet present in the cluster's region? GKE regional
# LBs (both gke-l7-rilb and gke-l7-regional-external-managed) require a
# subnet with purpose=REGIONAL_MANAGED_PROXY in the cluster's region per VPC.
# Requires gcloud — best-effort. Skip cleanly if gcloud is unavailable.
which gcloud >/dev/null 2>&1 && gcloud compute networks subnets list --filter="purpose=REGIONAL_MANAGED_PROXY" --format="value(name,region,network)" 2>/dev/null || echo "GCLOUD_UNAVAILABLE_OR_NO_PROXY_SUBNET"
```

Record the findings as `GKE_GATEWAY_API_ENABLED` (boolean) and `PROXY_ONLY_SUBNET_PRESENT` (boolean per cluster region). Surface in the summary line:

> "GKE Gateway: API enabled (yes/no), proxy-only subnet in `<region>` (yes/no/unknown)."

Q8.5 uses these to pick the GKE option's status string ("Ready to use" / "Cluster ready, proxy-only subnet missing — autoconfig will create one" / "GKE Gateway API not enabled — autoconfig will enable" / "Needs install"). Phase 6.1 has the BLOCKING install branch when the user picks a GKE provider with prereqs missing.

**Report findings as a structured bullet list** — per SKILL.md Hard Rule #9, NOT as a wall-of-text paragraph. Phase 2 consumes this:

> **Phase 1 — cluster discovery summary**
>
> - **Cluster context:** `gke_my-project_us-central1_cluster-1`
> - **GPUs:** 16 H100s across 2 nodes (smallest node: 208 CPU, 880 Gi mem, 8 GPU)
> - **Gateway:** GatewayClass `istio` present; no Gateway resources deployed yet
> - **EPP install:** none detected
> - **GIE CRDs:** not installed (Phase 4 Step 2a will ask whether to include them in the bundle — default 'yes' on greenfield)
> - **RDMA capability:** no — missing DPv2 (PD with RDMA would need cluster recreation; PD with TCP fallback is still possible)
> - **HF Secret candidates in target ns:** none found (Phase 2 Q0.5 will offer "scaffold new" / "skip-public" defaults)
> - **GKE Gateway prereqs:** API enabled — yes; proxy-only subnet in region — no (autoconfig will create one if you pick a GKE provider)
>
> **Existing model servers found (1):**
> - `vllm-qwen3-32b` in namespace `default`
>   - model: `Qwen/Qwen3-32B` · TP=2 · 8 replicas · 2 GPUs/replica · `--max-model-len 32768`
>   - autoTune metric (`vllm:cache_config_info`) verified

Use the same bullet structure verbatim. Skip rows that don't apply (e.g., drop the "Existing model servers" subsection on a greenfield cluster). Don't combine the rows back into prose; the user is going to skim, not read.

If model servers ARE present, Phase 2's first question is "are you configuring autoconfig for these existing pods, or deploying a new set?" That answer determines whether Section 1 (Model & Topology) gets skipped.

**Per-node CPU + RAM allocatable** (carried into Phase 3's schedulability audit). Record these from the node JSON dump above. Typical values:
- `a3-ultragpu-8g` (H200): ~224 CPU, ~3022 Gi memory, 8 GPU
- `a3-mega-8g` (H100): ~208 CPU, ~1864 Gi memory, 8 GPU
- `a3-highgpu-8g` (H100): ~208 CPU, ~880 Gi memory, 8 GPU

**Note the smallest** (most-constrained) GPU node's CPU + RAM. Phase 3 uses these as the denominator in the density math. If the cluster has heterogeneous GPU nodes, surface that explicitly to the user — autoconfig assumes a homogeneous pool today.

---

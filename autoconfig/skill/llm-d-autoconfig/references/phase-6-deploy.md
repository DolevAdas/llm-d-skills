# Phase 6 — Deploy (end-to-end)

*Detailed runbook for SKILL.md Phase 6. Splits into pre-flight (6.0), prereq install (6.1), namespace + secrets (6.2), modelservice install (6.3), EPP install (6.4), Gateway resource (6.5), smoke test (6.6), final report (6.7). Read this only if the user opted into deploy in Phase 5.*


**Offer to deploy via the agent's question primitive (yesno) — don't ask if the user wants to walk through the steps themselves.** This skill's whole point is that the agent IS the deployer.

```json
[{"header": "Deploy", "question": "Want me to deploy this to your cluster? I'll handle each step: pre-flight checks, installing any missing CRDs, applying the modelservice, installing the EPP with our generated config, wiring up the route, and running a smoke test. Each step is gated by your confirmation, so you can stop or redirect at any point.", "type": "yesno"}]
```

Do NOT phrase it as "Would you like me to walk through the deployment steps to apply this configuration?" — that wording implies the user does the work and you narrate. We do the work; the user approves each step.

**Critical rule: NEVER apply, install, or modify cluster state without explicit per-step user confirmation. Always show what will change first, wait for "yes apply" (or equivalent), then proceed. If any step fails, consult the pitfalls KB and offer a fix — do not auto-fix.**

If user agrees, **first decide which branch you're on** based on what Phase 1 cluster discovery found:

- **Greenfield** (no existing EPP configmap, no existing modelserver Deployment for this model in the target namespace): proceed with Phase 6.0 → 10.7 below as a fresh install.
- **Existing deployment** (Phase 1 found an EPP configmap and/or modelserver pods): jump to "Phase 6-U — Update existing deployment" below instead. Don't run the greenfield install path on top of an existing deployment — at best you'll get duplicate releases, at worst you'll clobber state.

If you're not sure which branch applies, ASK the user: "I see existing resources in `<ns>` from a prior install — `<list them>`. Are you trying to (a) update that deployment with the new config, (b) add a parallel deployment alongside it, or (c) replace it entirely?" Each answer maps to a different flow:

| User intent | Flow |
|---|---|
| Update existing | Phase 6-U |
| Parallel deployment | Greenfield path with a new release name + namespace; warn that modelserver labels may need to be distinct so the EPP routes correctly |
| Replace entirely | `helm uninstall` the old release first (with explicit confirmation), then run greenfield path |

### Phase 6-U — Update an existing deployment

Use this when Phase 1 found an existing EPP configmap or modelserver pods in the target namespace.

#### 10-U.1 — Diff the existing EPP config against the proposed config

Multi-step (avoid `$()` and process substitution `<()` — both blocked by some agent runtimes):

```bash
# Step 1: list configmaps for the release. Capture the configmap name from stdout.
kubectl get configmap -n <ns> -l app.kubernetes.io/instance=<release-name> -o name
```

```bash
# Step 2: agent uses the configmap name from step 1, fetches its data, writes to a file.
kubectl get <configmap-name> -n <ns> -o jsonpath='{.data}' > <work-dir>/existing-cm-data.json
```

```bash
# Step 3: extract the EPP config string from the configmap data (Python, not shell).
python3 -c "import json; d=json.load(open('<work-dir>/existing-cm-data.json')); k=next(k for k in d if 'plugins' in k or 'epp-config' in k); open('<work-dir>/existing-epp.yaml','w').write(d[k])"
```

```bash
# Step 4: render the proposed EPP config to a file via the script.
python3 <skill-install-dir>/scripts/autoconfig_poc.py \
    --input <work-dir>/autoconfig-input.json --render-yaml 2>/dev/null \
    > <work-dir>/proposed-epp.yaml
```

Wait — `--render-yaml` writes to stderr. Use `--helm-values-out` or just have the script render the EPP YAML directly via:

```bash
# Step 4 corrected: dump the proposed EPP YAML to a file (extract from JSON output).
python3 <skill-install-dir>/scripts/autoconfig_poc.py \
    --input <work-dir>/autoconfig-input.json --output <work-dir>/proposed.json
python3 -c "import json,yaml; print(yaml.safe_dump(json.load(open('<work-dir>/proposed.json'))['decisions']['epp']['endpoint_picker_config'], sort_keys=False))" > <work-dir>/proposed-epp.yaml
```

```bash
# Step 5: diff
diff -u <work-dir>/existing-epp.yaml <work-dir>/proposed-epp.yaml
```

Surface the diff to the user. Categorize the changes:

- **Safe in-place tweaks**: scorer weight values, `maxPrefixTokensToMatch`, `autoTune` settings, adding/removing scorers within the same workload class. These are pure config; pods don't restart.
- **Risky changes**: switching workload class (e.g. balanced → latency-tight), enabling latency-predictor (requires sidecars), enabling precise-prefix-cache (requires vLLM `--kv-events-config` flag). Warn the user that these may require modelserver-side changes too.
- **Breaking changes**: changing the model ID, tp size, or replica count. These aren't EPP config changes — they're modelserver redeploys. Use the "Replace entirely" path instead.

#### 10-U.2 — Apply the config update (helm upgrade)

If the user accepts the diff:

```bash
helm upgrade "$RELEASE_NAME" \
    oci://ghcr.io/llm-d/charts/llm-d-router-standalone-dev \
    -f "https://raw.githubusercontent.com/llm-d/llm-d/${LLM_D_REF}/guides/recipes/router/base.values.yaml" \
    -f "https://raw.githubusercontent.com/llm-d/llm-d/${LLM_D_REF}/guides/${GUIDE_NAME}/router/${GUIDE_NAME}.values.yaml" \
    -f "$VALUES_FILE" \
    -n "$NAMESPACE" --version "$ROUTER_CHART_VERSION" \
    --reuse-values=false   # important: don't merge with old values, replace them
```

`helm upgrade` is idempotent — running it on a release that's already at the target state is a no-op. The EPP pod restarts and reloads the new config; modelserver pods are unaffected.

#### 10-U.3 — Verify and smoke test

Same Phase 6.6 smoke test as the greenfield path. The model ID hasn't changed, so the same curl payload should still work; if it doesn't, the change you applied broke routing somehow — surface to the user.

If the user wants to roll back: `helm rollback "$RELEASE_NAME" -n "$NAMESPACE"` reverts to the previous release version.

---

### Phase 6.0 — Pre-flight (cluster discovery, no changes)

Run these read-only commands and report findings:

```bash
kubectl config current-context
kubectl get nodes -o json | jq '.items[].status.allocatable | with_entries(select(.key | test("nvidia|gpu")))'
kubectl get crd gateways.gateway.networking.k8s.io 2>&1 | head -1
kubectl get crd inferencepools.inference.networking.k8s.io 2>&1 | head -1
kubectl get gatewayclass -o name 2>&1
kubectl get pods -A -l app=nvidia-device-plugin-daemonset --no-headers 2>&1 | head -3
kubectl get namespace <ns> 2>&1
kubectl auth can-i create configmap -n <ns>
kubectl auth can-i create deployment -n <ns>
# HF token Secret check — Phase 6.2 needs to know whether to create or skip.
# Default secret name is llm-d-hf-token (matches the modelserver overlays).
kubectl get secret llm-d-hf-token -n <ns> -o name 2>&1
# Istio control plane check — search ALL namespaces, not just istio-system.
# Production istio is often installed in a custom namespace (e.g.
# llm-d-istio-system) with a revision label (e.g. istio.io/rev=llm-d-gateway).
# Capture: namespace, revision (or empty for default), GAIE flag presence.
kubectl get pods -A -l app=istiod -o jsonpath='{range .items[*]}{.metadata.namespace}{"|"}{.metadata.labels.istio\.io/rev}{"|"}{.spec.containers[0].env}{"\n"}{end}' 2>&1 | head -5
```

Parse the istiod row(s) into:
- `ISTIOD_NAMESPACE` (the namespace istiod is in; may be `istio-system`, `llm-d-istio-system`, or anything else)
- `ISTIOD_REVISION` (the value of `istio.io/rev` label on the istiod pod, or empty for a default install)
- `ISTIOD_GAIE_FLAG` (boolean: does the env contain `ENABLE_GATEWAY_API_INFERENCE_EXTENSION` set to true?)

If multiple istiod pods are returned (multi-revision install), record each row and surface all to the user — they MUST tell you which revision the Gateway should bind to.

Report a summary: "Found GPUs `<types>`, gateway-class `<name>` (or none), CRDs present `<list>`, namespace exists `<yes/no>`, HF token Secret `llm-d-hf-token` present `<yes/no>`, istiod present in `<ISTIOD_NAMESPACE>` (revision: `<ISTIOD_REVISION or default>`, GAIE flag: `<yes/no>`), you can create resources `<yes/no>`."

**Carry the HF-secret-present finding into Phase 6.2.** If present → Phase 6.2 verifies it and skips the create flow. If absent → Phase 6.2 MUST run the create flow before Phase 6.3.

**Carry the istiod findings into Phase 6.1's gateway-mode branch.** Four states matter:
- istiod absent → must install if user picks `provider=istio` (BLOCKING in Phase 6.1)
- istiod present but `ENABLE_GATEWAY_API_INFERENCE_EXTENSION` not set → must reinstall with the flag (BLOCKING in Phase 6.1; the flag is required for InferencePool backendRef to work)
- istiod present + flag set + no revision (default install) → ready to use; skip install
- istiod present + flag set + has revision → ready to use, BUT Phase 6.5 Step 1 must patch the Gateway with `istio.io/rev: <ISTIOD_REVISION>` after applying the recipe; otherwise the revised istiod ignores the Gateway and it sits at PROGRAMMED=False forever

### Phase 6.1 — Install missing prereqs

For each prereq missing from pre-flight, ask the user before installing. The skill tracks upstream **`main`** (the `llm-d-router` chart and its router-schema guide values live on main; upstream has not cut a release for this path yet). Set the refs together so the script and the kubectl/helm commands stay coherent:

```bash
export GAIE_VERSION=v1.5.0          # GAIE CRDs only (InferencePool/InferenceObjective) — kubernetes-sigs/gateway-api-inference-extension
export ROUTER_CHART_VERSION=v0      # llm-d-router chart (rolling -dev OCI tag; no immutable release yet)
export LLM_D_REF=main               # llm-d guide values + modelserver kustomize overlays
export LLM_D_ROUTER_REF=main        # llm-d-router CRD components
```

`main` is a moving target — if upstream restructures, an install can break. If the user wants reproducibility, they can override any ref with a tag or SHA (the autoconfig script accepts matching `--llm-d-ref` / `--llm-d-router-ref` / `--chart-version` flags), but note that the router chart + its router-schema guide values only exist on `main` today, so a pinned tag may fall back to the older GIE path. Engine-awareness (non-vLLM) requires the router chart, hence main.

**Gateway API + GAIE CRDs** (two separate installs — the GAIE kustomization does NOT include the base Gateway API CRDs):

```json
[{"header": "Install CRDs", "question": "Gateway API and Inference Extension CRDs need to be installed. Install both?", "type": "yesno"}]
```

```bash
GATEWAY_API_VERSION=v1.5.1
GAIE_VERSION=v1.5.0

# Base Gateway API CRDs (Gateway, GatewayClass, HTTPRoute, etc.).
# On GKE, these come from `--gateway-api=standard` instead — skip if Phase 6.0 found GKE_GATEWAY_API_ENABLED.
kubectl apply -k "https://github.com/kubernetes-sigs/gateway-api/config/crd?ref=${GATEWAY_API_VERSION}"

# Inference Extension CRDs (InferencePool, InferenceObjective, etc.).
kubectl apply -k "https://github.com/kubernetes-sigs/gateway-api-inference-extension/config/crd?ref=${GAIE_VERSION}"
```

Verify both CRD groups:

```bash
kubectl api-resources --api-group=gateway.networking.k8s.io
kubectl api-resources --api-group=inference.networking.k8s.io
```

**Deploy mode (gateway provider)** — this is a decision the user must make explicitly. Ask:

> "How do you want to expose the EPP?
>
> 1. **Standalone Mode** (recommended for testing/dev): the EPP runs as a regular Kubernetes Service. No Gateway provider needed, no HTTPRoute. You reach it via ClusterIP or `kubectl port-forward`. Simplest path; works on any cluster.
> 2. **Gateway Mode** (production-style): the EPP sits behind a Kubernetes Gateway managed by Istio, Kgateway, or another provider. Needs a Gateway provider installed AND an HTTPRoute connecting the gateway to the InferencePool. If you already have a Functional GatewayClass (Phase 6.0 verified its controller pods are running), this is straightforward; otherwise add an extra prereq install step.
>
> Which mode?"

When prompting on a GKE cluster (cluster-context starts with `gke_`), surface GKE's managed L7 ILB as a third recommended option in the gateway-mode follow-up:

> "On GKE, the easiest gateway-mode path is `gke-l7-rilb` (managed L7 internal LB) or `gke-l7-gxlb` (managed external). The GKE control plane provisions the ingress; nothing extra to install. Pick that over Istio/Kgateway unless you have an existing service mesh you're integrating with."

**Record the user's answer as `DEPLOY_MODE` (one of `standalone` or `gateway`).** Subsequent phases branch on this:

- Phase 6.4 picks the chart variant (`llm-d-router-standalone` vs `llm-d-router-gateway`)
- Phase 6.5 (HTTPRoute) is **skipped entirely** in standalone mode
- Phase 6.6 (smoke test) uses port-forward in standalone mode, gateway IP in gateway mode

**The gateway provider was already chosen in Phase 2 Q8.5** (recorded as `GATEWAY_PROVIDER`) and confirmed in the Phase 3 recap. Don't re-prompt here — the user already answered. Just consume the value:

- If `GATEWAY_PROVIDER` is `gke-l7-rilb` or `gke-l7-regional-external-managed` → no controller install needed; skip to Phase 6.4.
- If `GATEWAY_PROVIDER = istio` → run the Istio install BLOCKING flow below.
- If `GATEWAY_PROVIDER = agentgateway` → install via the agentgateway helm chart (see https://github.com/llm-d/llm-d/blob/main/guides/prereq/gateways/agentgateway.md).

**Hard rule (per Hard Rule #7): if Phase 6.0 marked `GATEWAY_PROVIDER` as non-functional or its controller missing, do NOT silently fall back to standalone or to a different provider.** Surface the gap, return to Phase 2 Q8.5 (re-ask), and let the user pick again with the new constraint visible.

#### GKE Gateway prereqs (BLOCKING when `GATEWAY_PROVIDER` starts with `gke-` and Phase 1 found gaps)

Source: [`guides/prereq/gateways/gke.md`](https://github.com/llm-d/llm-d/blob/main/guides/prereq/gateways/gke.md). Two prereqs both `gke-l7-rilb` and `gke-l7-regional-external-managed` need:

**1. Gateway API enabled on the cluster.** If Phase 1 set `GKE_GATEWAY_API_NOT_ENABLED`, run the `gcloud container clusters update` command. Show the command first, then ask:

```bash
# CLUSTER_NAME and REGION come from `kubectl config current-context` parsing
# (gke_<project>_<region>_<cluster>).
gcloud container clusters update CLUSTER_NAME --location=REGION --gateway-api=standard
```

```json
[{"header": "Enable GW API", "question": "GKE Gateway API isn't enabled on this cluster. Enable it? (Cluster-level update; ~1-2 min, non-disruptive to running workloads.)", "type": "yesno"}]
```

After enabling, wait for the GKE-managed GatewayClasses to appear:

```bash
kubectl wait --for=jsonpath='{.spec.controllerName}'=networking.gke.io/gateway --timeout=120s gatewayclass/gke-l7-rilb 2>&1 | head -1
```

**2. Proxy-only subnet in the cluster's region.** If Phase 1 set `PROXY_ONLY_SUBNET_PRESENT=false` (or the gcloud check was skipped and the agent confirmed via a fresh check here), create one. The CIDR range needs to be ≥ /26 (≥64 IPs); GCP recommends /23 (sources: `cloud.google.com/load-balancing/docs/proxy-only-subnets`, `cloud.google.com/vpc/docs/vpc`).

**Pre-check (mandatory): is the cluster's VPC auto-mode?** Auto-mode VPCs reserve `10.128.0.0/9` for auto-created subnets — any CIDR in that block will be rejected at create time. The default VPC is always auto-mode, so this trap fires on the most common setup. Run this before constructing the create command:

```bash
# VPC_NETWORK from `gcloud container clusters describe ... --format='value(network)'`.
gcloud compute networks describe VPC_NETWORK --format='value(autoCreateSubnetworks)'
# Returns: True (auto-mode — avoid 10.128.0.0/9) or False (custom-mode — any non-overlapping CIDR works).
```

If `True`, the agent picks the default CIDR from outside `10.128.0.0/9` (the suggestion below already does this). If `False`, any non-overlapping CIDR is fine, including ones inside `10.128.0.0/9`.

Then show the command first, then ask:

```bash
# Pick a CIDR that doesn't overlap with the cluster's pod/service ranges.
# Default 192.168.0.0/23: RFC1918, outside auto-mode's 10.128.0.0/9 reservation,
# and doesn't conflict with typical GKE pod (10.x) or services (34.118.x) ranges.
# Non-RFC1918 is also valid per GCP docs if 192.168.x is already taken.
# REGION = cluster region (from kubectl context). VPC_NETWORK = the VPC the
# cluster lives in (from `gcloud container clusters describe ... --format='value(network)'`).
gcloud compute networks subnets create llm-d-proxy-subnet \
  --purpose=REGIONAL_MANAGED_PROXY \
  --role=ACTIVE \
  --region=REGION \
  --network=VPC_NETWORK \
  --range=192.168.0.0/23
```

```json
[{"header": "Create subnet", "question": "GKE Gateway needs a proxy-only subnet (purpose=REGIONAL_MANAGED_PROXY) in this cluster's region. Create one named llm-d-proxy-subnet with /23 CIDR 192.168.0.0/23? You can override the name and CIDR if 192.168.0.0/23 conflicts with your VPC ranges.", "type": "yesno"}]
```

If the user says "override", ask follow-ups for `subnet_name` and `cidr_range` (text fields). When suggesting alternates on an auto-mode VPC, stick to `192.168.0.0/16`, `172.16.0.0/12`, or `10.0.0.0/9` (the lower half) — anything in `10.128.0.0/9` will be rejected.

If the user has Shared VPC, the subnet creation must happen in the host project — surface that and ask the user to run the command themselves rather than running it from the cluster project context.

After creating, verify:

```bash
gcloud compute networks subnets describe llm-d-proxy-subnet --region=REGION
```

**Hard rule (per Hard Rule #7):** if either prereq install fails, do NOT proceed to Phase 6.5 (Gateway resource creation). Surface the failure, return to Q8.5 (gateway provider re-pick) so the user can switch providers if they don't want to grant the gcloud permissions.

#### Istio install (BLOCKING when user picks `provider=istio` and istiod is missing or misconfigured)

The Gateway resource must NOT be created before istiod is installed and configured with the GAIE flag — the Gateway will never reach `PROGRAMMED=True` and the smoke test will fail confusingly.

Source for the install steps below: [`guides/prereq/gateways/istio.md`](https://github.com/llm-d/llm-d/blob/main/guides/prereq/gateways/istio.md). Fetch that doc for non-default scenarios (different version, custom values, multi-cluster mesh).

Branch on what Phase 6.0 found about istiod:

**Case A: istiod is absent.** Ask explicit confirmation (yesno), then run the install. Show the exact commands inline so the user knows what they're authorizing.

The exact commands the agent will run if the user confirms:
```bash
ISTIO_VERSION=1.29.2
curl -L https://istio.io/downloadIstio | ISTIO_VERSION=${ISTIO_VERSION} sh -
export PATH="$PWD/istio-${ISTIO_VERSION}/bin:$PATH"
istioctl install -y --set values.pilot.env.ENABLE_GATEWAY_API_INFERENCE_EXTENSION=true
```

Display those commands in chat first, then ask:

```json
[{"header": "Install Istio", "question": "You picked Gateway Mode with provider=istio, but istiod isn't running in istio-system. Install Istio with the GAIE inference-extension flag (cluster-wide install)? The first command pipes a remote installer script into sh — if that's restricted in your environment, you can install istioctl yourself and skip the curl step.", "type": "yesno"}]
```

After the install, verify istiod is Ready before moving on:

```bash
kubectl wait --for=condition=Available --timeout=120s deployment/istiod -n istio-system
```

**Case B: istiod is present but `ENABLE_GATEWAY_API_INFERENCE_EXTENSION` is not set.** This is silent — the Gateway will accept traffic but won't honor `InferencePool` backendRef. Without that env var, istiod doesn't know how to route to the inference extension. BLOCKING.

Show the command inline, then ask:

```bash
istioctl install -y --set values.pilot.env.ENABLE_GATEWAY_API_INFERENCE_EXTENSION=true
```

```json
[{"header": "Reinstall Istio", "question": "Istio is installed but missing the GAIE inference-extension flag. Without ENABLE_GATEWAY_API_INFERENCE_EXTENSION=true, the Gateway will accept requests but InferencePool routing will silently fail. Reinstall istiod with the flag (non-disruptive — istioctl reconciles in place)?", "type": "yesno"}]
```

**Case C: istiod is present with the flag.** Skip the install; proceed to Phase 6.5 (Gateway resource + HTTPRoute) when ready.

**Do NOT proceed past this step in any case where istiod is missing or misconfigured AND the user picked provider=istio.** The deploy will land in a confusing half-broken state otherwise (Gateway exists, never PROGRAMMED, smoke test fails with no clear cause).

**NVIDIA device plugin** (if not running on GPU nodes):

```json
[{"header": "Install NVIDIA", "question": "NVIDIA device plugin not detected. Install it? (Required to expose GPUs as nvidia.com/gpu resources)", "type": "yesno"}]
```

```bash
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.14.5/nvidia-device-plugin.yml
```

After each install, wait for readiness (`kubectl wait --for=condition=Established crd/...` or pod-ready as appropriate) and confirm before moving on.

#### Agentgateway install (BLOCKING when `GATEWAY_PROVIDER=agentgateway` and the controller isn't running)

Source: [`guides/prereq/gateways/agentgateway.md`](https://github.com/llm-d/llm-d/blob/main/guides/prereq/gateways/agentgateway.md). Two helm releases — CRDs first, then the controller — both with `inferenceExtension.enabled=true`.

Show the commands first, then ask:

```bash
AGENTGATEWAY_VERSION=v1.1.0

helm upgrade --install agentgateway-crds \
  oci://cr.agentgateway.dev/charts/agentgateway-crds \
  --namespace agentgateway-system \
  --create-namespace \
  --version ${AGENTGATEWAY_VERSION}

helm upgrade --install agentgateway \
  oci://cr.agentgateway.dev/charts/agentgateway \
  --namespace agentgateway-system \
  --create-namespace \
  --version ${AGENTGATEWAY_VERSION} \
  --set inferenceExtension.enabled=true
```

```json
[{"header": "Install agentgw", "question": "You picked Gateway Mode with provider=agentgateway, but the agentgateway controller isn't running. Install via the two helm releases above (CRDs + controller, both into agentgateway-system namespace, with inferenceExtension.enabled=true)?", "type": "yesno"}]
```

After install, verify the controller and GatewayClass are ready:

```bash
kubectl get pods -n agentgateway-system
kubectl wait --for=jsonpath='{.spec.controllerName}'=agentgateway.dev/agentgateway --timeout=120s gatewayclass/agentgateway
```

If verification fails, STOP and surface the failure. Don't proceed to Phase 6.5.

### Phase 6.2 — Namespace and secrets

```bash
kubectl create namespace <ns>
```

For HF token Secret:

> "I need to create an HF token Secret for the modelserver to pull the weights. Two options — pick whichever you're comfortable with:
>
> **Option A (you create it yourself, recommended for production):** I'll print the command; you run it locally with your token. Nothing about your token reaches the chat history.
> ```
> kubectl create secret generic <hf-token-secret-name> -n <ns> --from-literal=HF_TOKEN=<paste-your-token-here>
> ```
> Tell me when the secret exists and I'll continue.
>
> **Option B (paste the token to me):** Paste your HF token in the next message and I'll run the command. The token isn't logged or stored beyond the kubectl call, but it WILL be in this chat history.
>
> Which do you prefer?"

Default to A unless the user explicitly chooses B. If they choose A, wait for their confirmation that the secret exists before proceeding to Phase 6.3 — verify with `kubectl get secret <hf-token-secret-name> -n <ns> -o name` (read-only, safe).

If the model is not gated (e.g. public Qwen/Llama variants), the secret is still required by some vLLM HF client paths but the token can be an empty string. Mention this to the user — sometimes they don't have an HF account and don't realize they can use an empty token for public models.

### Phase 6.3 — Install the modelservice

**Modelservers are NOT in the autoconfig bundle.** The bundle (Phase 4 Step 2 / Phase 6.4) is EPP + gateway only. Modelserver deploys are a separate `kubectl apply -k` step against an upstream kustomize overlay — different hardware (NVIDIA/AMD/TPU) and different model servers (vLLM/SGLang/TRT-LLM) need different overlays, and inlining the wrong one into the bundle would produce broken YAML for many users.

**Skip this phase entirely** if `context.modelserver_deploy_planned = false` (the user picked Phase 2 Q0 = "configure for existing pods" — model servers are already running, we're only deploying the EPP/gateway layer on top).

**Run this phase** when `modelserver_deploy_planned = true`. It installs the model server pods BEFORE Phase 6.4 (EPP install), so the InferencePool's `matchLabels` selector finds running pods to route to.

The modelservice is installed via **Kustomize overlays** fetched directly from the upstream `llm-d` repo — no local clone required, no Helm chart involved.

All upstream references in this phase go through `kubectl apply -k <https-git-url>` (per Hard Rule #1). The Kustomize overlays are fetched directly from the upstream `llm-d` repo URL — no clone, no local file lookup.

**Branch on `topology.mode`:**

- **agg**: use `optimized-baseline/modelserver/<accelerator>/<engine>/[<infra>/]` (the table below). Single deployment, patched to user's tp/replicas.
- **disagg (PD)**: use `pd-disaggregation/modelserver/gpu/vllm/gke` — the GKE infra overlay on top of the PD base (which deploys two Deployments named `prefill` and `decode`). On non-GKE clusters use `gpu/vllm/base` (or `gpu/vllm/coreweave` for RoCE). Patch BOTH the prefill and decode Deployments separately for replicas/TP.

The remaining steps describe the agg path. PD-specific steps follow at the end of this phase (Step 5 onward).

**Step 1: pick the right hardware overlay.** The layout under `optimized-baseline/modelserver/` is by accelerator FAMILY (not GPU model). H100, H200, A100, B200 all use the same `gpu/` overlays. NVIDIA GPU overlays fork by infra (`base` for vanilla clusters, `gke` for GKE); the other accelerators are single kustomize roots:

| User's hardware | Overlay path |
|---|---|
| NVIDIA GPU + vLLM (default for chat) | `modelserver/gpu/vllm/<infra>` (`<infra>` = `base` or `gke`) |
| NVIDIA GPU + SGLang | `modelserver/gpu/sglang/<infra>` (`base` or `gke`) |
| AMD GPU | `modelserver/amd/vllm` |
| AMD GPU + SGLang | `modelserver/amd/sglang` |
| Intel XPU (Data Center GPU Max 1550+) | `modelserver/xpu/vllm` |
| Intel Gaudi (HPU) | `modelserver/hpu/vllm` |
| Google TPU v6e | `modelserver/tpu-v6/vllm` |
| Google TPU v7 | `modelserver/tpu-v7/vllm` |
| CPU-only (testing) | `modelserver/cpu/vllm` |

For any NVIDIA GPU, default to `gpu/vllm/base` (or `gpu/vllm/gke` on GKE). There is no per-GPU-model granularity (`h100/`, `h200/`, etc. do not exist).

**Step 2: apply the overlay directly from the upstream repo URL.**

```bash
GUIDE_NAME="optimized-baseline"
HARDWARE_OVERLAY="gpu/vllm/base"   # adjust per the table above (gpu/vllm/gke on GKE)
NAMESPACE="<ns>"

kubectl apply -n "$NAMESPACE" \
    -k "https://github.com/llm-d/llm-d.git/guides/${GUIDE_NAME}/modelserver/${HARDWARE_OVERLAY}/?ref=${LLM_D_REF}"
```

`kubectl apply -k <https-url>` clones the referenced repo path into a temporary working directory and applies the kustomization — no on-disk repo required. The `?ref=${LLM_D_REF}` query string pins to the ref set above (default `main`). On GKE, prefer the `gpu/vllm/gke` overlay, which carries the NCCL-tuner-disable patch.

**Step 3: post-deploy patching when defaults don't match user request.** The upstream overlay hardcodes baseline values: model `Qwen/Qwen3-32B`, `--tensor-parallel-size=2`, GPU limits `nvidia.com/gpu=2`. If the user requested anything different (different model, different tp size, different replica count), you MUST patch the Deployment after applying the overlay, BEFORE waiting for it to become Ready.

**Critical: tp size and GPU resource limits must match.** vLLM crashes with a `ValueError` if `--tensor-parallel-size=N` but the container has fewer than N GPUs allocated. When patching tp, ALWAYS update both `resources.limits.nvidia.com/gpu` and `resources.requests.nvidia.com/gpu` to the same N.

Example patch for "deploy Qwen2.5-72B with tp=8 across 1 replica per node":

```bash
DEPLOY_NAME="<name-from-kustomize-output>"
kubectl patch deployment "$DEPLOY_NAME" -n "$NAMESPACE" --type=json -p '[
    {"op": "replace", "path": "/spec/replicas", "value": 1},
    {"op": "replace", "path": "/spec/template/spec/containers/0/args", "value": [
        "--model", "Qwen/Qwen2.5-72B-Instruct",
        "--tensor-parallel-size", "8",
        "--port", "8000"
    ]},
    {"op": "replace", "path": "/spec/template/spec/containers/0/resources/limits/nvidia.com~1gpu", "value": "8"},
    {"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/nvidia.com~1gpu", "value": "8"}
]'
```

Read the deployment first (`kubectl get deployment $DEPLOY_NAME -o yaml | grep -E 'model|tensor-parallel|nvidia.com/gpu'`) to see the actual default args before patching, then construct a patch that replaces only what the user changed. Surface the diff to the user before applying.

**Step 4: wait for modelservice pods to be Ready.** Model download can take 5-15 minutes for first install:

```bash
kubectl rollout status deployment -n "$NAMESPACE" -l llm-d.ai/role=decode --timeout=20m
```

If a pod enters CrashLoopBackOff, check `kubectl logs <pod>` immediately. Common causes documented in the pitfalls KB.

If hardware isn't represented in the table (e.g. user has a CSP-specific accelerator), surface this to the user and offer to:
- Use `gpu/vllm/` as the closest fit (works for most NVIDIA hardware)
- Skip modelservice install and let the user supply their own modelservice deployment
- Point them at `https://github.com/llm-d/llm-d/tree/main/guides/optimized-baseline/modelserver/` for the published hardware list

---

**PD-specific modelserver install (skip if `topology.mode = "agg"`).**

PD ships TWO Deployments per replica role: `prefill` and `decode`. The base kustomization bakes in canonical sizes (8 prefill × TP=1, 2 decode × TP=4 for gpt-oss-120b on H200) and the GKE overlay just layers a NCCL-tuner-disable patch on top.

**Step 5 (PD): apply the PD overlay.**

```bash
GUIDE_NAME="pd-disaggregation"
HARDWARE_OVERLAY="gpu/vllm/gke"   # GKE-specific PD overlay (handles gIB-NCCL conflict)
NAMESPACE="<ns>"

kubectl apply -n "$NAMESPACE" \
    -k "https://github.com/llm-d/llm-d.git/guides/${GUIDE_NAME}/modelserver/${HARDWARE_OVERLAY}/?ref=${LLM_D_REF}"
```

If you're not on GKE (e.g. coreweave with already-exposed `rdma/ib`), use `gpu/vllm/coreweave` instead. For non-GKE, non-coreweave clusters, use `gpu/vllm/base` (TCP fallback only — same as GKE without RDMA configured).

**Step 6 (PD): patch BOTH Deployments to match user-requested sizes.**

The PD base hardcodes `openai/gpt-oss-120b` with prefill TP=1 / decode TP=4 / 8 prefill replicas / 2 decode replicas. If the user picked a different model OR different sizes in Q4.6, patch each Deployment separately. Use the `pd-disaggregation-nvidia-gpu-vllm-` namePrefix the kustomization adds.

```bash
DEPLOY_PREFIX="pd-disaggregation-nvidia-gpu-vllm"

# Prefill patch — adjust args, replicas, GPU count
kubectl patch deployment "${DEPLOY_PREFIX}-prefill" -n "$NAMESPACE" --type=json -p '[
    {"op": "replace", "path": "/spec/replicas", "value": <prefill_replicas>},
    {"op": "replace", "path": "/spec/template/spec/containers/0/args/0", "value": "<model-id>"},
    {"op": "replace", "path": "/spec/template/spec/containers/0/args/2", "value": "--tensor-parallel-size=<prefill_tp>"},
    {"op": "replace", "path": "/spec/template/spec/containers/0/resources/limits/nvidia.com~1gpu", "value": "<prefill_tp>"},
    {"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/nvidia.com~1gpu", "value": "<prefill_tp>"}
]'

# Decode patch — same shape, decode-side values
kubectl patch deployment "${DEPLOY_PREFIX}-decode" -n "$NAMESPACE" --type=json -p '[
    {"op": "replace", "path": "/spec/replicas", "value": <decode_replicas>},
    {"op": "replace", "path": "/spec/template/spec/containers/0/args/0", "value": "<model-id>"},
    {"op": "replace", "path": "/spec/template/spec/containers/0/args/2", "value": "--tensor-parallel-size=<decode_tp>"},
    {"op": "replace", "path": "/spec/template/spec/containers/0/resources/limits/nvidia.com~1gpu", "value": "<decode_tp>"},
    {"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/nvidia.com~1gpu", "value": "<decode_tp>"}
]'
```

Read each Deployment first (`kubectl get deployment ${DEPLOY_PREFIX}-prefill -o yaml | grep -E 'tensor-parallel|nvidia.com/gpu'`) to confirm the args[] index is right (the canonical patch has model at args[0], `--disable-access-log...` at args[1], `--tensor-parallel-size=N` at args[2]). Surface the diff before patching.

**Step 7 (PD, RDMA-only): apply the GKE RDMA pod-resource patch.**

Skip this step if `topology.pd_transport = "tcp"`. When RDMA is available, both PD Deployments need RDMA NIC annotations and per-NIC resource requests AND the topology-aware podAffinity per the GCP `configure-pod-manifests-rdma` doc. The GKE overlay does NOT add these (unlike coreweave's overlay) — they must be templated against the cluster's actual Network resource names.

Two-step pattern (Hard Rule #4: never wrap in `$()`). Step (a) lists the cluster's Network names; the agent reads stdout. Step (b) constructs and applies the patch using the names captured from step (a):

```bash
# Step (a): list the cluster's Network resource names. Agent reads stdout
# (typical output: a3ultra-rdma-net-0 ... a3ultra-rdma-net-7).
kubectl get networks -o name
```

Filter the names client-side (in agent code, not in shell) to the RDMA networks only — typically the ones whose name contains `rdma` / `a3ultra` / `a4high`. Then construct the JSON-patch overlay using those names as input:

- `networking.gke.io.networks: <comma-separated network names>` annotation on the pod template
- `networking.gke.io.networks/<name>.IP: 1` resource per NIC (limits + requests)
- `podAffinity preferredDuringScheduling` on `cloud.google.com/gce-topology-block`

```bash
# Step (b): write the patch JSON to a file in <work-dir> from agent code,
# then apply with kubectl. <patch-file> is the path the agent created.
kubectl patch deployment "${DEPLOY_PREFIX}-prefill" -n "$NAMESPACE" --type=json --patch-file=<patch-file>
kubectl patch deployment "${DEPLOY_PREFIX}-decode"  -n "$NAMESPACE" --type=json --patch-file=<patch-file>
```

The exact JSON-patch shape is on the GCP doc (`configure-pod-manifests-rdma`). If the network names returned by step (a) don't match the canonical a3-ultra/a4-high pattern, fall back to TCP and ask the user to verify their cluster setup before retrying. Surface the constructed patch to the user before applying.

**Step 8 (PD): wait for both Deployments to be Ready.**

```bash
kubectl rollout status deployment "${DEPLOY_PREFIX}-prefill" -n "$NAMESPACE" --timeout=20m
kubectl rollout status deployment "${DEPLOY_PREFIX}-decode"  -n "$NAMESPACE" --timeout=20m
```

If either pool stays in CrashLoopBackOff, check logs for the NIXL-related pitfalls in the KB section (TCP fallback, gIB tuner, NIXL_SIDE_CHANNEL_HOST).

### Phase 6.4 — Install the EPP (with our generated config)

#### Bundle-apply path (Phase C — preferred over step-by-step helm install)

The script's `--bundle-dir` renderer (Phase C) produces a complete EPP deploy — chart-templated resources from the llm-d-router chart PLUS hand-rendered Gateway / HTTPRoute / Phase B feature resources (WVA CR, HPA, InferenceObjective, tiered-cache kustomization, etc.) — as one YAML per resource inside `<parent>/autoconfig-<TIMESTAMP>/`. `kubectl apply -f <dir>` ingests directly, replacing the entire Step 1 + Step 2 + Phase 6.5 sequence.

helm runs at GENERATION time only (the script invokes `helm template` internally to render the chart YAMLs). At APPLY time the user only needs `kubectl` — there's no `helm install` step.

Use the bundle path UNLESS:
- `helm` is not on PATH at generation time (the bundle renderer shells out to `helm template`).
- The user explicitly wants to walk the step-by-step helm install themselves (rare — the bundle path produces the same resources).
- You're debugging a chart-side problem and need the layered `-f` values to be visible at install time.

```bash
# After Phase 4 wrote autoconfig-input.json + the user confirmed the recap.
# The script defaults to the llm-d-router chart on main; pass --chart-version /
# --llm-d-ref / --llm-d-router-ref only if the user asked to pin a rev.
python3 <skill-install-dir>/scripts/autoconfig_poc.py \
    --input <work-dir>/autoconfig-input.json \
    --bundle-dir <work-dir> \
    --chart-version "${ROUTER_CHART_VERSION}" \
    --llm-d-ref "${LLM_D_REF}" \
    --llm-d-router-ref "${LLM_D_ROUTER_REF}"
# Script prints `wrote bundle to <work-dir>/autoconfig-<TIMESTAMP>` on stderr.
# Capture it: BUNDLE_DIR=$(ls -td <work-dir>/autoconfig-* | head -1)

# Sanity-check what's about to be applied:
ls "$BUNDLE_DIR"/*.yaml
cat "$BUNDLE_DIR/README.md"

# Optional: edit the HF token Secret in place (or delete that file and apply
# the Secret separately) before apply:
$EDITOR "$BUNDLE_DIR"/07-*-secret-llm-d-hf-token.yaml

# Apply (still pause for user confirmation per Hard Rule #5):
kubectl apply -f "$BUNDLE_DIR" -n <ns>
```

The bundle is byte-deterministic for a given input + chart version (modulo the timestamp directory name), so it's safe to commit to source control as a record of what was deployed. The `autoconfig-input.json` + `autoconfig-<TIMESTAMP>/` directory pair fully captures the deploy state.

**Step 0 is still BLOCKING** even on the bundle path — if `DEPLOY_MODE = gateway`, the gateway-provider controller must be running before `kubectl apply` succeeds (apply doesn't validate controller readiness; resources just sit in pending). Run Step 0's checks below; if any fail, return to Phase 6.1.

For Phase B features in the bundle:
- `autoscaler=wva` → bundle includes a `VariantAutoscaling` CR. WVA operator MUST be installed separately first (see `guides.workload_autoscaling_wva`).
- `autoscaler=hpa` → bundle includes a `HorizontalPodAutoscaler`. Requires Prometheus Adapter or custom-metrics-apiserver serving the `epp_queue_depth_avg` Pods metric.
- `enable_tiered_cache` / `enable_wide_ep` → bundle includes a `Kustomization` pointing at the upstream guide. Run `kubectl apply -k <bundled-kustomization>` separately AFTER the modelservice base.
- `serving_pattern=batch` / `async` → bundle includes only a comment scaffold; these patterns need separate gateway-style install.

If using the bundle path, skip "Step 1: render the helm values" and "Step 2: install the EPP via helm" below — the bundle did both. Resume at "Step 3: wait for the EPP pod Ready."

If NOT using the bundle path (helm unavailable, debugging, or the user opted into step-by-step), use Steps 1-3 below as the original sequence.

---

#### Step 0: BLOCKING gateway-provider precondition check

If `DEPLOY_MODE = gateway`, you MUST verify that `GATEWAY_PROVIDER`'s controller is actually running before doing anything else in Phase 6.4. The agent has historically skipped Phase 6.1's istio install and proceeded to helm install + HTTPRoute, leaving the user with a non-functional Gateway. Don't repeat that.

Run the right check for the chosen provider:

```bash
# GATEWAY_PROVIDER=istio — search ALL namespaces (revised installs may use a
# non-standard ns like llm-d-istio-system). Capture namespace + revision +
# GAIE flag for use by Phase 6.5 Step 1's revision-label patch.
kubectl get pods -A -l app=istiod -o jsonpath='{range .items[*]}{.metadata.namespace}{"|"}{.metadata.labels.istio\.io/rev}{"|"}{.spec.containers[0].env}{"\n"}{end}' 2>&1 | head -5
# Anything in stdout = istiod found. Empty = not installed. Then check the
# env field for ENABLE_GATEWAY_API_INFERENCE_EXTENSION=true.

# GATEWAY_PROVIDER=gke-l7-rilb or gke-l7-regional-external-managed
kubectl get gatewayclass <provider> -o jsonpath='{.spec.controllerName}' 2>&1
# and confirm proxy-only subnet exists in cluster region
which gcloud >/dev/null 2>&1 && gcloud compute networks subnets list --filter="purpose=REGIONAL_MANAGED_PROXY" --format="value(name,region)" 2>/dev/null

# GATEWAY_PROVIDER=agentgateway
kubectl get pods -A -l app.kubernetes.io/name=agentgateway --no-headers 2>&1 | head -3
```

**If any check fails, STOP. Return to Phase 6.1's matching install subsection** ("GKE Gateway prereqs" or "Istio install") and run those BLOCKING install steps to completion. Do NOT proceed to Step 1 below until the controller is verified running and (for istio) configured with the GAIE flag. Never silently fall back to standalone or to a different provider — surface the gap and re-confirm with the user per Hard Rule #7.

If `DEPLOY_MODE = standalone`, skip this step entirely and proceed to Step 1.

#### Step 1: render the helm values

The canonical optimized-baseline install layers two values files on the llm-d-router chart: the recipe base values + the guide-specific values. We slot OUR generated EndpointPickerConfig in by writing a third values file with the rendered config under `router.epp.pluginsCustomConfig`.

Render via the script's `--render-helm-values` flag. This emits the router chart's expected shape (with the EPP config nested under `router.epp.pluginsCustomConfig` as a string literal) — no shell-level `$()` interpolation, no heredoc, no `sed`. Use `<work-dir>` from Phase 4 Step 0 — don't make a new temp dir.

```bash
# Substitute <work-dir> with the path captured at Phase 4 Step 0.
python3 <skill-install-dir>/scripts/autoconfig_poc.py \
    --input <work-dir>/autoconfig-input.json \
    --render-helm-values --helm-values-out <work-dir>/autoconfig-values.yaml
```

The output file at that path is now a complete, valid helm values fragment ready for `helm install -f`.

#### Step 2: install the EPP via helm

Pick the chart and flags based on `DEPLOY_MODE` from Phase 2 Q8.

Both upstream values files come from `raw.githubusercontent.com` URLs (helm's `-f` flag accepts HTTPS URLs natively). The local overlay file (the path you got from mktemp + the script's `--helm-values-out`) goes last so its `pluginsCustomConfig` wins over the guide's defaults.

The agent constructs this command by substituting `<work-dir>` with the path mktemp returned in Step 1a, `<release-name>` with the user's release name, `<ns>` with the namespace, and `<version>` with `${ROUTER_CHART_VERSION}` (default `v0`).

**Branch on `topology.mode`** to pick the GUIDE_NAME and the values URL the helm command layers in:

| `topology.mode` | `GUIDE_NAME` | Guide values URL (the second `-f`) |
|---|---|---|
| `agg` | `optimized-baseline` | `.../optimized-baseline/router/optimized-baseline.values.yaml` |
| `disagg` | `pd-disaggregation` | `.../pd-disaggregation/router/pd-disaggregation.values.yaml` |

The user's autoconfig values file (`<work-dir>/autoconfig-values.yaml`) goes LAST so its `router.epp.pluginsCustomConfig` overrides the guide's defaults — that's where the script's per-workload tuning lives. The guide values still set things outside it (chart-level config, `router.modelServers.matchLabels`, etc.).

**If `DEPLOY_MODE=standalone`** (Phase 6.5 will be skipped, Phase 6.6 uses port-forward):

```bash
helm install <release-name> \
    oci://ghcr.io/llm-d/charts/llm-d-router-standalone-dev \
    -f "https://raw.githubusercontent.com/llm-d/llm-d/${LLM_D_REF}/guides/recipes/router/base.values.yaml" \
    -f "https://raw.githubusercontent.com/llm-d/llm-d/${LLM_D_REF}/guides/${GUIDE_NAME}/router/${GUIDE_NAME}.values.yaml" \
    -f <work-dir>/autoconfig-values.yaml \
    -n <ns> --version <version>
```

**If `DEPLOY_MODE=gateway`** (Phase 6.5 applies an HTTPRoute, Phase 6.6 uses gateway IP). Substitute `<provider>` with the user's gateway provider (`istio`, `kgateway`, `gke`, `agentgateway`, etc.):

```bash
helm install <release-name> \
    oci://ghcr.io/llm-d/charts/llm-d-router-gateway-dev \
    -f "https://raw.githubusercontent.com/llm-d/llm-d/${LLM_D_REF}/guides/recipes/router/base.values.yaml" \
    -f "https://raw.githubusercontent.com/llm-d/llm-d/${LLM_D_REF}/guides/${GUIDE_NAME}/router/${GUIDE_NAME}.values.yaml" \
    -f <work-dir>/autoconfig-values.yaml \
    --set provider.name=<provider> \
    --set httpRoute.create=true \
    --set httpRoute.inferenceGatewayName=llm-d-inference-gateway \
    -n <ns> --version <version>
```

The `httpRoute.create=true` flag tells the gateway chart to **create the HTTPRoute as part of the helm release** — named `<release>`, with parentRefs to `llm-d-inference-gateway` and backendRefs to the InferencePool. Phase 6.5 verifies this chart-managed HTTPRoute and does NOT create a separate one. (The chart's `httpRoute.requestTimeout` defaults to 300s; override with `--set httpRoute.requestTimeout=<dur>` if your completions need longer.)

**Step 2a: when latency-predictor is enabled** (the user said yes in Phase 6 to `enable_latency_predictor`), add `--set router.latencyPredictor.enabled=true` to the helm install above. This single flag deploys the training + prediction sidecars in-pod and wires up the env vars on the EPP container.

After install, verify the sidecars came up (5 containers per EPP pod when this flag is on — epp + 1 training + 3 prediction). Substitute `<release-name>` and `<ns>` with the values you used:

```bash
kubectl get pod -n <ns> -l app.kubernetes.io/instance=<release-name> -o jsonpath='{.items[0].spec.containers[*].name}'
# Expected output: epp training-server prediction-server-1 prediction-server-2 prediction-server-3
```

This works on a fresh machine — no `git clone` step, no `LLMD_REPO` env var. If the user does happen to have the repo cloned locally, that's coincidence; do NOT switch the command to local paths because "they're there." The skill must be portable.

If you want gateway-managed mode (vs standalone), use `oci://ghcr.io/llm-d/charts/llm-d-router-gateway-dev` instead — see `llm-d/guides/optimized-baseline/README.md` "Gateway Mode" section. Standalone is sufficient for the smoke test.

**Step 3: wait for the EPP pod Ready.**

```bash
kubectl rollout status deployment -n "$NAMESPACE" -l app.kubernetes.io/instance="$GUIDE_NAME" --timeout=5m
```

### Phase 6.5 — Create the Gateway resource + verify HTTPRoute (gateway mode only)

**Skip this entire phase if `DEPLOY_MODE=standalone`.** Standalone mode exposes the EPP directly as a Service — there's no gateway to bind a route to. Move on to Phase 6.6.

**Re-verify the gateway provider's controller is running** (defense-in-depth — Phase 6.4 Step 0 should have already gated this, but a Gateway without a controller silently fails at PROGRAMMED time):

```bash
# istio: istiod must be Running with GAIE flag.
# gke-l7-*: GKE Gateway API + proxy-only subnet must be present (Phase 6.0 verified).
# agentgateway: agentgateway controller pods must be Running in agentgateway-system.
```

If verification fails, STOP and return to Phase 6.1's matching install subsection.

#### Step 1: Apply the Gateway recipe (creates the `Gateway` resource)

The llm-d guides ship per-provider Gateway recipes that overlay a common base (`recipes/gateway/base/gateway.yaml`) and patch in the right `gatewayClassName`. The Gateway resource is named `llm-d-inference-gateway` in all recipes. Pick the recipe matching `GATEWAY_PROVIDER`:

| `GATEWAY_PROVIDER` | Recipe path |
|---|---|
| `istio` | `guides/recipes/gateway/istio/` |
| `gke-l7-rilb` | `guides/recipes/gateway/gke-l7-rilb/` |
| `gke-l7-regional-external-managed` | `guides/recipes/gateway/gke-l7-regional-external-managed/` |
| `agentgateway` | `guides/recipes/gateway/agentgateway/` |

Apply the recipe directly from the upstream URL — no clone required:

```bash
kubectl apply -n <ns> -k "https://github.com/llm-d/llm-d.git/guides/recipes/gateway/<GATEWAY_PROVIDER>/?ref=${LLM_D_REF}"
```

**Istio revision patch (only when `GATEWAY_PROVIDER=istio` AND Phase 6.0 found `ISTIOD_REVISION` non-empty).** Revised istio installs ignore Gateways unless they're labeled with the matching revision. The recipe doesn't include this label, so patch it on after apply:

```bash
# Substitute <ISTIOD_REVISION> with the value captured in Phase 6.0.
kubectl label gateway llm-d-inference-gateway -n <ns> istio.io/rev=<ISTIOD_REVISION> --overwrite
```

For a default (unrevised) istio install, skip this step entirely.

#### Step 2: BLOCKING wait for `Gateway` to reach `PROGRAMMED=True`

A Gateway without `PROGRAMMED=True` means the controller hasn't finished provisioning the data plane (or isn't running at all). Don't proceed to HTTPRoute or smoke test until this passes:

```bash
kubectl wait --for=condition=Programmed --timeout=180s gateway/llm-d-inference-gateway -n <ns>
```

If timeout, STOP and consult the pitfalls KB entry "Gateway resource never PROGRAMMED=True". Common causes: missing controller (return to Phase 6.1's install branch), GAIE flag missing on istiod, GKE Gateway API not enabled, wrong `provider.name` in helm install.

Get the Gateway address (used for smoke test in Phase 6.6):

```bash
kubectl get gateway llm-d-inference-gateway -n <ns> -o jsonpath='{.status.addresses[0].value}'
```

#### Step 3: Verify the chart-managed HTTPRoute is bound

The HTTPRoute was created by the llm-d-router-gateway helm chart in Phase 6.4 (because `httpRoute.create=true` was set). Its name is `<release>` (same as the helm release name; not `<release>-httproute`). Do NOT create another HTTPRoute manually — that would duplicate the chart-managed one.

```bash
kubectl get httproute <release> -n <ns> -o jsonpath='{.status.parents[0].conditions}'
```

Look for `type: Accepted, status: True`. If not Accepted, check that `httpRoute.create=true` was actually passed to helm (it's required), and that the chart-rendered HTTPRoute's `parentRefs.name` matches the Gateway resource name (`llm-d-inference-gateway`) and `backendRefs.name` matches the InferencePool name (also `<release>` from the chart).

### Phase 6.6 — Smoke test

**Critical: the `model` field in the curl payload MUST exactly match the model the modelserver is actually serving.** This is the same model ID the user provided in Phase 2 — NOT the upstream Kustomize default, NOT a guess, NOT a different variant from the same family. Mismatching produces a 404 `"model X does not exist"` error from the OpenAI API surface. If the user requested `Qwen/Qwen2.5-72B-Instruct`, the curl payload must say exactly that.

If you patched the modelserver in Phase 6.3 to change the model, use the patched value. If you didn't patch and the upstream default is in effect, use that default — but in that case you should ALREADY have warned the user during Phase 6.3 that what's being deployed differs from what they asked for.

**Two-step pattern, branching on `DEPLOY_MODE`.** Don't use shell `$()` substitution — many agent runtimes block it. Run each command on its own and substitute values at command-build time.

**If `DEPLOY_MODE=gateway`** — get the gateway IP, then curl it:

```bash
# Step 1: read the gateway IP. Capture stdout.
kubectl get gateway -n <ns> -o jsonpath='{.items[0].status.addresses[0].value}'
```

```bash
# Step 2: agent substitutes <gateway-ip> and <model-id> from values it has.
curl -s "http://<gateway-ip>/v1/completions" \
    -H 'Content-Type: application/json' \
    -d '{"model":"<model-id>","prompt":"Hello, ","max_tokens":20}'
```

**If `DEPLOY_MODE=standalone`** — port-forward to the EPP service, then curl localhost:

```bash
# Step 1: find the EPP service name. Capture stdout.
kubectl get svc -n <ns> -l app.kubernetes.io/instance=<release-name> -o name
```

```bash
# Step 2: start a port-forward in the background. Capture the printed address.
# Run as its own command — do NOT use & in a $() subshell.
kubectl port-forward -n <ns> <svc-name-from-step-1> 8080:8000 &
```

```bash
# Step 3: agent waits ~1s for port-forward to bind, then curls localhost.
curl -s "http://localhost:8080/v1/completions" \
    -H 'Content-Type: application/json' \
    -d '{"model":"<model-id>","prompt":"Hello, ","max_tokens":20}'
```

```bash
# Step 4: clean up the port-forward when done.
# Find the PID via `pgrep -f "kubectl port-forward.*<svc-name>"` and kill it,
# OR if running as a foreground job, Ctrl+C.
```

Expected: a JSON response with completion text. Common failures:

- **`{"error":{"message":"The model X does not exist","code":404}}`**: model ID mismatch between deploy and curl. Re-read what the modelserver is serving (`kubectl get pod <vllm-pod> -o yaml | grep -A1 args` and look for `--model`) and fix the curl payload.
- **Connection refused**: gateway not Ready or HTTPRoute not bound. Check `kubectl get gateway,httproute -n <ns>`.
- **5xx errors**: model loaded but request shape wrong. Try the explicit `/v1/chat/completions` endpoint with a `messages` array if completions doesn't work.

### Phase 6.7 — Final report

Tell the user:
- What was installed (list the helm releases + kubectl-applied resources)
- What was skipped (any prereqs that were already present)
- The cluster context + namespace where it landed
- The smoke test result
- How to undo if needed:
  ```bash
  # The HTTPRoute is chart-managed; helm uninstall removes it automatically.
  helm uninstall <release> -n <ns>
  # Gateway resource (created in Phase 6.5 via kubectl apply -k recipe) is NOT
  # chart-managed; remove separately if you also want to drop it.
  kubectl delete gateway llm-d-inference-gateway -n <ns>
  # Note: prereq CRDs, gateway provider controllers (istiod / agentgateway),
  # and GKE proxy-only subnet intentionally NOT removed (cluster-wide).
  ```

After the final report, offer Phase 7 — the benchmark execution we generated a config for in Phase 5.

---


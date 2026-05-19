---
name: configure-wva-autoscaling-llm-d
description: Configure and deploy Workload Variant Autoscaler (WVA) for llm-d inference deployments. Guides users through namespace selection, configuration (with presets or custom values), deployment via Makefile + kubectl apply, and verification. Produces a reusable deployment script.
---

## Agent Behavior Rules

1. **Follow steps IN ORDER. Never skip or combine steps.**
2. **STOP after each step and ask for explicit permission to proceed to the next step.**
3. **Do NOT modify existing repository code.** Cloning a missing repo is allowed.
4. **Use existing skill scripts when possible** — see [`scripts/SCRIPTS.md`](./scripts/SCRIPTS.md).
5. **Before creating any Kubernetes resource**, state what will be created and why.
6. **After each kubectl/helm/make command**, run a verification check and report the result before continuing.

---

## Step 1 — Select Target Namespace and Deployments

**Ask the user:**

> "Which Kubernetes namespace should WVA monitor?"
> (Provide a single namespace, e.g., `my-llm-ns`)

Store the answer as `WVA_NS` — it will be used throughout deployment. WVA will be deployed **into** this namespace so it can watch the llm-d workloads there.

Then discover ALL llm-d deployments in that namespace:

```bash
kubectl get deployment -n <namespace> -l llm-d.ai/role=decode -o custom-columns=NAME:.metadata.name,MODEL:.metadata.labels.llm-d\.ai/model-id,REPLICAS:.spec.replicas
```

If no results, try the alternative label:
```bash
kubectl get deployment -n <namespace> -l app.kubernetes.io/part-of=llm-d -o custom-columns=NAME:.metadata.name,REPLICAS:.spec.replicas
```

Present ALL findings:
```
Namespace: my-llm-ns
Found 3 llm-d deployments:
  1. optimized-baseline-nvidia-gpu-vllm-decode  (model: Qwen/Qwen3-32B, replicas: 1)
  2. ms-gpt-oss-6b-llm-d-modelservice-decode    (model: EleutherAI/gpt-j-6b, replicas: 1)
  3. llama-70b-h100-decode                       (model: meta/llama-3.1-70b, replicas: 2)
```

**STOP. Ask:** "Which deployment(s) should WVA autoscale? (Enter numbers, names, or 'all')"

Wait for user response. The FIRST deployment selected will be deployed via Makefile (creates the controller + VA + HPA). Additional deployments get VA + HPA via `kubectl apply`.

---

## Step 2 — Configuration

**Ask the user ONE question:**

> "How would you like to configure WVA?"
> 1. **Help me choose** — I'll suggest configurations based on your goals
> 2. **I know my values** — I'll enter them directly
> 3. **Load from saved config** — Use a previously saved YAML file

---

### Option 1: Help me choose

Present these presets:

| Preset | Best for | KV Threshold | Queue Threshold | Stabilization | Min Replicas |
|--------|----------|-------------|-----------------|---------------|--------------|
| **Low Latency** | Real-time apps, chatbots | 0.70 | 3 | 60s up / 300s down | 2 |
| **Balanced** (default) | General workloads | 0.80 | 5 | 120s up / 300s down | 1 |
| **Cost Optimized** | Batch, async workloads | 0.85 | 8 | 180s up / 600s down | 1 |

**Ask:** "Which preset fits your use case? (1/2/3, or describe your goals)"

After user picks, also ask:
- "What is the maximum number of replicas allowed?" (default: 10)
- "Which scaler backend: HPA or KEDA?"
  - HPA: standard, works out-of-box. Min replicas = 1.
  - KEDA: required for scale-to-zero (min replicas = 0). Must be installed on cluster.

Then **auto-detect** the rest:
- Model ID: from deployment labels or pod args
- Accelerator: auto-detected from the cluster (see auto-detection logic below) — can be `nvidia`, `amd`, or `cpu`
- Platform: check if OpenShift (`kubectl api-resources | grep route.openshift.io`)

---

### Option 2: I know my values

**First ask:** "Which scaler backend: HPA or KEDA?"

#### Namespace-level settings (ask once — apply to all deployments in the namespace)

Collect these values ONCE before asking about per-deployment configuration:

| Parameter | Description | Default |
|-----------|-------------|---------|
| `kv_cache_threshold` | KV cache % that marks a replica as saturated | `0.80` |
| `queue_length_threshold` | Queue depth that marks a replica as saturated | `5` |
| `kv_spare_trigger` | Proactive scale-up when spare KV drops below this | `0.10` |
| `queue_spare_trigger` | Proactive scale-up when spare queue drops below this | `3` |
| `scale_up_window` | Seconds to wait before scaling up | `120` |
| `scale_down_window` | Seconds to wait before scaling down | `300` |

**Then ask:** "Do you want the same settings for all deployments, or configure each deployment separately?"

- If **same for all** → use the namespace-level values above for all deployments; only ask per-deployment for min/max/cost.
- If **per-deployment** → ask only the per-deployment parameters below for each deployment (namespace-level values are the baseline; the user can override specific thresholds per deployment).

#### Per-deployment settings (ask for each selected deployment)

For each deployment, first show the namespace-level defaults and ask:
> "For deployment `<name>` (`<model-id>`): use namespace defaults, or override specific values?"

Always collect these required per-deployment parameters:

| Parameter | Description | Default |
|-----------|-------------|---------|
| `min_replicas` | Minimum replicas (0 only with KEDA) | `1` |
| `max_replicas` | Maximum replicas | `10` |
| `variant_cost` | Cost weight — lower-cost variants scale first | `"10.0"` |

If the user wants to override thresholds for this specific deployment, also collect:

| Parameter | Description | Default (from namespace) |
|-----------|-------------|----------------------|
| `kv_cache_threshold` | Override KV saturation threshold for this deployment | *(namespace value)* |
| `queue_length_threshold` | Override queue saturation threshold for this deployment | *(namespace value)* |
| `kv_spare_trigger` | Override spare KV trigger for this deployment | *(namespace value)* |
| `queue_spare_trigger` | Override spare queue trigger for this deployment | *(namespace value)* |
| `scale_up_window` | Override scale-up stabilization for this deployment | *(namespace value)* |
| `scale_down_window` | Override scale-down stabilization for this deployment | *(namespace value)* |

After user provides all values, proceed to save.

---

### Option 3: Load from saved config

Check for existing configs:
```bash
ls skills/configure-wva-autoscaling-llm-d/scripts/configs/wva-*.yaml 2>/dev/null
```

Present available configs and let user pick one. Load values from the YAML.

---

### Save Configuration

After gathering values (from any option), save as YAML:

```yaml
# File: skills/configure-wva-autoscaling-llm-d/scripts/configs/wva-<namespace>.yaml
namespace: my-llm-ns
platform: kubernetes  # or openshift
scaler_backend: hpa  # or keda
wva_repo_path: /path/to/llm-d-workload-variant-autoscaler

# Shared defaults — applied to all deployments unless overridden per-deployment
defaults:
  kv_cache_threshold: "0.80"
  queue_length_threshold: "5"
  kv_spare_trigger: "0.10"
  queue_spare_trigger: "3"
  scale_up_window: 120
  scale_down_window: 300

# Per-deployment configuration
# Any field under 'defaults' can be overridden here for a specific deployment.
# Fields not listed fall back to the shared defaults above.
models:
  - deployment: optimized-baseline-nvidia-gpu-vllm-decode
    model_id: "Qwen/Qwen3-32B"
    accelerator: <auto-detected: nvidia|amd|cpu>
    min_replicas: 1
    max_replicas: 10
    variant_cost: "10.0"
    # No overrides — uses all shared defaults

  - deployment: ms-gpt-oss-6b-llm-d-modelservice-decode
    model_id: "EleutherAI/gpt-j-6b"
    accelerator: <auto-detected: nvidia|amd|cpu>
    min_replicas: 1
    max_replicas: 5
    variant_cost: "5.0"
    # Override thresholds for this lower-capacity model
    kv_cache_threshold: "0.70"
    queue_length_threshold: "3"
    scale_up_window: 60
    scale_down_window: 180
```

Tell the user: "Configuration saved to `<path>`. You can reload this in future runs."

**STOP. Ask:** "Configuration is ready. Shall I show you the deployment plan? (yes/no)"

---

## Step 3 — Deployment Plan

Write a concise plan and display it to the user.

### 3a. All Deployments and Models

| # | Deployment | Model ID | Accelerator | Min/Max | Cost | Deploy Method |
|---|-----------|----------|-------------|---------|------|---------------|
| 1 | optimized-baseline-nvidia-gpu-vllm-decode | Qwen/Qwen3-32B | *auto-detected* | 1/10 | "10.0" | Makefile (first) |
| 2 | ms-gpt-oss-6b-llm-d-modelservice-decode | EleutherAI/gpt-j-6b | *auto-detected* | 1/5 | "5.0" | kubectl apply |
| 3 | llama-70b-h100-decode | meta/llama-3.1-70b | *auto-detected* | 2/10 | "80.0" | kubectl apply |

### 3b. Shared Configuration

| Parameter | Value | What it means |
|-----------|-------|---------------|
| Namespace | `my-llm-ns` | WVA controller deployed here, watches this namespace only |
| Scaler Backend | HPA | HPA reads `wva_desired_replicas` metric via Prometheus Adapter |
| KV Cache Threshold | `0.80` | Replica saturated at 80% KV usage → WVA recommends scale-up |
| Queue Threshold | `5` | Replica saturated at queue depth 5 |
| KV Spare Trigger | `0.10` | Proactive scale-up when avg spare KV < 10% |
| Queue Spare Trigger | `3` | Proactive scale-up when avg spare queue < 3 |
| Scale-up Window | `120s` | Must see sustained saturation for 2 min before adding replicas |
| Scale-down Window | `300s` | Must see low utilization for 5 min before removing replicas |

### 3c. Execution Steps

```
Step 4a: Pre-flight checks (existing controller, KEDA availability)
Step 4b: Deploy WVA controller + monitoring + scaler backend via Makefile (Kustomize)
Step 4c: Verify controller running and watching namespace
Step 4d: Add accelerator labels to all target deployments
Step 4e: Apply VA + HPA (or annotated HPA) for ALL models via kubectl apply
Step 4f: Verify ALL VAs/HPAs are ready and have valid targets
Step 5:  Generate reusable deployment script
```

### 3d. References

| Resource | Link / Command |
|----------|---------------|
| WVA User Guide | `${WVA_REPO_PATH}/deploy/README.md` |
| Kustomize overlays | `${WVA_REPO_PATH}/config/default/` (k8s), `config/openshift/` (OCP) |
| HPA annotation samples | `${WVA_REPO_PATH}/config/samples/hpa/annotations/` |
| Troubleshooting | [Troubleshooting.md](./Troubleshooting.md) |
| WVA GitHub | https://github.com/llm-d/llm-d-workload-variant-autoscaler |

**STOP. Ask:** "Does this plan look correct? Ready to deploy? (yes/no/adjust)"

---

## Step 4 — Deploy

Execute each sub-step one at a time, verifying after each.

---

### 4a. Pre-flight Checks

```bash
cd skills/configure-wva-autoscaling-llm-d/scripts/
./preflight-check.sh <WVA_NS> --scaler-backend <prometheus-adapter|keda>
```

If a stale WVA controller is found, ask permission to remove:
```bash
cd <WVA_REPO_PATH>
WVA_NS=<WVA_NS> NAMESPACE=<WVA_NS> ./deploy/install.sh --undeploy
```

**STOP. Ask:** "Pre-flight checks complete. Ready to deploy WVA controller via Makefile? (yes/no)"

---

### 4b. Deploy WVA Controller (Makefile + Kustomize)

`deploy/install.sh` deploys the WVA controller via Kustomize, plus Prometheus monitoring and the scaler backend. **It does NOT create VariantAutoscaling or HPA resources** — those are applied in step 4e for all deployments.

All configuration must be `export`ed as environment variables **before** calling `make` (or pass them inline). The Makefile passes `NAMESPACE` to the scripts while `deploy/install.sh` reads `WVA_NS` — set both to the same value until this inconsistency is resolved in the Makefile.

**Kubernetes:**
```bash
cd <WVA_REPO_PATH>

# WVA_NS was captured in Step 1 — WVA runs in the same namespace as llm-d so it can watch workloads
export WVA_NS=<namespace-from-step-1>
export NAMESPACE=$WVA_NS          # workaround: Makefile passes NAMESPACE; install.sh reads WVA_NS
export LLMD_NS=$WVA_NS
export NAMESPACE_SCOPED=true
export SCALER_BACKEND=<prometheus-adapter|keda>
export DEPLOY_LWS=false          # set false if LWS already installed
export DEPLOY_PROMETHEUS=true    # set false if Prometheus already installed
export DEPLOY_WVA=true
export DEPLOY_PROMETHEUS_ADAPTER=true  # set false if using KEDA

make deploy-wva-on-k8s IMG=ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest
```

**OpenShift:**
```bash
cd <WVA_REPO_PATH>

export WVA_NS=<namespace-from-step-1>
export NAMESPACE=$WVA_NS          # workaround: Makefile passes NAMESPACE; install.sh reads WVA_NS
export LLMD_NS=$WVA_NS
export NAMESPACE_SCOPED=true
export SCALER_BACKEND=prometheus-adapter
export MONITORING_NAMESPACE=<openshift-user-workload-monitoring|openshift-monitoring>
export SKIP_TLS_VERIFY=true

INSTALL_GATEWAY_CTRLPLANE=false \
make deploy-wva-on-openshift IMG=ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest
```

**To customise thresholds**, edit the ConfigMap after deploy (takes effect without restart):
```bash
kubectl edit configmap workload-variant-autoscaler-saturation-scaling-config \
  -n <WVA_NS>
```

> **`LLM_D_MODELSERVICE_NAME`**: Used by `install-llmd-infra.sh` as the deployment name — the script uses it as-is. Default: `<GUIDE_NAME>-nvidia-gpu-vllm-decode`. Override when your deployment has a different name.

> **OpenShift `INSTALL_GATEWAY_CTRLPLANE=false`**: Skips gateway control plane installation (defaults to `true`). Set to `false` when the gateway control plane is already installed.

> **OpenShift exit code 2**: The Makefile chains scripts that may exit 2 even when WVA itself succeeded. Always verify with kubectl before assuming failure.

> **`MONITORING_NAMESPACE`**: Check with: `kubectl get apiservice v1beta1.external.metrics.k8s.io -o jsonpath='{.spec.service.namespace}'`

**What the Makefile creates:**
- WVA controller Deployment via Kustomize (`config/default` or `config/openshift`)
- Prometheus monitoring stack (if `DEPLOY_PROMETHEUS=true`)
- Scaler backend — Prometheus Adapter (HPA) or KEDA
- llm-d infrastructure via `install-llmd-infra.sh` (gateway, EPP, ModelService)
- **No VariantAutoscaling or HPA resources** — apply those in step 4e

---

### 4c. Verify Controller

```bash
kubectl get deployment workload-variant-autoscaler-controller-manager -n <WVA_NS>
kubectl logs -n <WVA_NS> -l control-plane=controller-manager --tail=20 | grep -i "watching"
```

Expected: controller Running, logs contain `"Watching single namespace"` with `"namespace":"<WVA_NS>"` (structured JSON log).

**STOP. Report and ask:** "Controller deployed. Proceed to add accelerator labels? (yes/no)"

---

### 4d. Add Accelerator Labels

Auto-detect the accelerator for each deployment:

```bash
ACCELERATOR=$(skills/configure-wva-autoscaling-llm-d/scripts/detect-accelerator.sh \
  <WVA_NS> <deployment-name>)
echo "Detected: $ACCELERATOR"
```

If the script exits with an error, ask the user: "Could not auto-detect accelerator for `<deployment>`. Is it `nvidia`, `amd`, or `cpu`?"

> Valid values: `nvidia` (covers H100, A100, L4, A10, etc.), `amd`, `cpu`. **Do not assume `nvidia`.**

Apply the label for EACH selected deployment:
```bash
kubectl label deployment <deployment-name> -n <WVA_NS> \
  inference.optimization/acceleratorName=$ACCELERATOR --overwrite
```

**Verify:**
```bash
kubectl get deployment -n <WVA_NS> \
  -o custom-columns=NAME:.metadata.name,ACCELERATOR:.metadata.labels."inference\.optimization/acceleratorName"
```

If using the VA-based path (not annotation-based), also verify and correct the VA's accelerator label:
```bash
# VA-based path only — skip if using annotation-based HPAs (no VA CRD exists)
kubectl get variantautoscaling -n <WVA_NS> \
  -o jsonpath='{.items[*].metadata.labels.inference\.optimization/acceleratorName}'
# If any VA shows a GPU model name (e.g., "H100") instead of vendor (e.g., "nvidia"), patch it:
kubectl patch variantautoscaling <va-name> -n <WVA_NS> --type=merge \
  -p '{"metadata":{"labels":{"inference.optimization/acceleratorName":"'$ACCELERATOR'"}}}'
```

---

### 4e. Apply VA + HPA for All Models

> **Note:** The VariantAutoscaling CRD is deprecated. The preferred approach is to annotate your HPA or ScaledObject directly (see annotation-based alternative below). The VA CRD path still works during the deprecation period.

Apply a VariantAutoscaling + HPA (or annotated HPA) for **every** selected deployment — the `install.sh` from step 4b does not create any VA or HPA resources.

> **Namespace scope**: With `NAMESPACE_SCOPED=true` the controller watches only the namespace where it runs (`WVA_NS`). Deploy VAs and HPAs to that **same** namespace, or set `NAMESPACE_SCOPED=false` to watch all namespaces.

Run `apply-hpa.sh` for **each** selected deployment. Choose the mode that matches your scaler backend and VA preference:

**Annotated HPA (preferred — no VA CRD required):**
```bash
skills/configure-wva-autoscaling-llm-d/scripts/apply-hpa.sh \
  --mode annotated \
  --namespace <WVA_NS> \
  --deployment <full-deployment-name> \
  --model-id "<model-id>" \
  --variant-cost "<variant_cost>" \
  --min-replicas <min> \
  --max-replicas <max> \
  --scale-up-window <scale_up_window> \
  --scale-down-window <scale_down_window>
```

**VA + HPA (Prometheus Adapter backend, VA CRD path):**
```bash
skills/configure-wva-autoscaling-llm-d/scripts/apply-hpa.sh \
  --mode va-hpa \
  --namespace <WVA_NS> \
  --deployment <full-deployment-name> \
  --model-id "<model-id>" \
  --variant-cost "<variant_cost>" \
  --accelerator $ACCELERATOR \
  --min-replicas <min> \
  --max-replicas <max> \
  --scale-up-window <scale_up_window> \
  --scale-down-window <scale_down_window>
```

**VA + ScaledObject (KEDA backend):**
```bash
skills/configure-wva-autoscaling-llm-d/scripts/apply-hpa.sh \
  --mode keda \
  --namespace <WVA_NS> \
  --deployment <full-deployment-name> \
  --model-id "<model-id>" \
  --variant-cost "<variant_cost>" \
  --accelerator $ACCELERATOR \
  --min-replicas <min> \
  --max-replicas <max> \
  --scale-up-window <scale_up_window> \
  --scale-down-window <scale_down_window> \
  --prometheus-url <prometheus-url>
```

> `apply-hpa.sh` derives the resource short name by stripping `-decode` from the deployment name. The HPA/VA will be named `<short-name>-hpa` / `<short-name>-va`.

> **Critical**: HPA metric name must be `wva_desired_replicas`. Do NOT use `wva_kv_cache_saturation` or `wva_queue_depth_saturation` — they are not exposed by Prometheus Adapter.

> **`variant_name`** must match the resource WVA tracks: VA name (VA-based path) or HPA name (annotated path). `apply-hpa.sh` sets this correctly.

> **`type: AverageValue, averageValue: "1"`**: HPA computes `desiredReplicas = currentReplicas × (metric / 1)` — directly matching WVA's recommendation.

---

### 4f. Verify ALL Resources

**Wait ~2 minutes for Prometheus to scrape metrics, then verify:**

```bash
skills/configure-wva-autoscaling-llm-d/scripts/verify-wva.sh <WVA_NS>
```

If verification is incomplete (VAs not METRICSREADY, HPAs showing `<unknown>`), run the troubleshoot script:
```bash
skills/configure-wva-autoscaling-llm-d/scripts/troubleshoot-scaling.sh <WVA_NS>
```

**Common causes:**
- `METRICSREADY: False` → accelerator label missing on deployment or VA (re-run Step 4d)
- HPA `<unknown>` → wrong metric name, `variant_name` mismatch, or Prometheus Adapter not running
- Deployment at 0 replicas → `kubectl scale deployment <name> -n <WVA_NS> --replicas=1`

**STOP. Report final status for each model:**
```
Model 1 (Qwen/Qwen3-32B):     VA=METRICSREADY:True, HPA=1/1 ✓
Model 2 (gpt-j-6b):            VA=METRICSREADY:True, HPA=1/1 ✓
Model 3 (llama-3.1-70b):       VA=METRICSREADY:True, HPA=2/1 ✓
```

**Ask:** "All models verified. Proceed to generate deployment script? (yes/no)"

---

## Step 5 — Generate Reusable Deployment Script

Generate a script that can reproduce this entire deployment:

```bash
cd skills/configure-wva-autoscaling-llm-d/scripts/

./generate-deploy-script.sh \
  --namespace <namespace> \
  --deployment <first-deployment-name> \
  --wva-repo <wva-repo-path> \
  --model-id "<model-id>" \
  --variant-cost "<variant_cost>" \
  --accelerator <detected-accelerator> \
  --min-replicas <min_replicas> \
  --max-replicas <max_replicas> \
  --kv-threshold <kv_cache_threshold> \
  --queue-threshold <queue_length_threshold> \
  --scale-up-window <scale_up_window> \
  --scale-down-window <scale_down_window> \
  --output deploy-wva-<namespace>.sh \
  --non-interactive
```

For additional models, append their `kubectl apply` commands to the generated script or create separate scripts.

Tell the user:
> "Deployment script saved to `<path>`. To reproduce the full WVA setup:
> ```bash
> ./<script-name>.sh
> ```"

**STOP. Ask:** "Deployment complete. Would you like to run a load test to verify scaling works? (yes/no)"

---

## Step 6 — Optional Load Test

**Only run if user says yes.**

### What the test does
Sends concurrent streaming requests to fill KV cache, triggering WVA to recommend scale-up.

### Run the test

```bash
cd skills/configure-wva-autoscaling-llm-d/scripts/
./test-wva-scaling.sh <namespace> <deployment-name> "<model-id>" 200
```

If the script fails (e.g., no gateway/InferencePool), use direct pod IP:
```bash
POD_IP=$(kubectl get pod -n <namespace> -l llm-d.ai/role=decode -o jsonpath='{.items[0].status.podIP}')

kubectl run wva-load-test -n <namespace> --rm -i --restart=Never \
  --image=curlimages/curl:latest \
  --command -- sh -c "
for i in \$(seq 1 200); do
  curl -s -N -X POST \"http://$POD_IP:8000/v1/chat/completions\" \
    -H \"Content-Type: application/json\" \
    -d '{\"model\":\"<model-id>\",\"messages\":[{\"role\":\"user\",\"content\":\"Write a long detailed essay. Part '\$i'.\"}],\"max_tokens\":4000,\"stream\":true}' \
    > /dev/null 2>&1 &
done
wait"
```

### Monitor while test runs

```bash
# Watch WVA decisions
kubectl logs -n <namespace> -l control-plane=controller-manager -f | grep -E "shouldScaleUp|desiredReplicas"

# Watch HPA
kubectl get hpa -n <namespace> -w

# Watch replicas
kubectl get deployment <deployment-name> -n <namespace> -w
```

### Expected result
1. WVA log: `"shouldScaleUp": true, "desiredReplicas": 2`
2. HPA target increases
3. After stabilization window: new pod starts

Report outcome to user.

---

## Summary Output

At the end of a successful run, present:

```
============================================
WVA Deployment Summary
============================================
Namespace:         <namespace>
Scaler Backend:    HPA
Configuration:     Balanced preset (kv=0.80, queue=5)

Models configured:
  1. <deployment-1>  Model: <model-id>  Min/Max: 1/10  Cost: "10.0"  Status: ACTIVE
  2. <deployment-2>  Model: <model-id>  Min/Max: 1/5   Cost: "5.0"   Status: ACTIVE

Saved artifacts:
  - Config YAML:  scripts/configs/wva-<namespace>.yaml
  - Deploy script: scripts/deploy-wva-<namespace>.sh

Commands:
  Redeploy:  ./scripts/deploy-wva-<namespace>.sh
  Verify:    ./scripts/verify-wva.sh <namespace>
  Test:      ./scripts/test-wva-scaling.sh <namespace> <deployment>
  Undeploy:  cd <WVA_REPO_PATH> && WVA_NS=<WVA_NS> NAMESPACE=<WVA_NS> ./deploy/install.sh --undeploy
============================================
```

---

## Reference

### Scaler Backend Decision

| User choice | Makefile `SCALER_BACKEND` value | When to use | Scale-to-zero? |
|-------------|--------------------------------|-------------|----------------|
| **HPA** | `prometheus-adapter` | Standard setup, works with kube-prometheus-stack or OpenShift monitoring | No (min 1 replica) |
| **KEDA** | `keda` | KEDA already installed, or scale-to-zero required | Yes (min 0 replicas) |

> When the user selects **HPA**, pass `SCALER_BACKEND=prometheus-adapter` to the Makefile. When the user selects **KEDA**, pass `SCALER_BACKEND=keda`.

### Key Constraints (VA + HPA compatibility)

These must ALL be true for WVA to work:

1. **VA must have accelerator label**: `inference.optimization/acceleratorName: <vendor>` — valid values: `nvidia`, `amd`, `cpu`. Auto-detect from cluster; do not assume `nvidia`.
2. **HPA metric must be `wva_desired_replicas`** — the only metric Prometheus Adapter exposes
3. **HPA selector labels must match**: `variant_name` = VA resource name, `exported_namespace` = namespace
4. **VA and HPA must target the same deployment**
5. **Target deployment must have >= 1 replica** (HPA cannot scale from 0 without KEDA)
6. **`variantCost` must be a string** (e.g., `"10.0"` not `10.0`)
7. **API version must be `llmd.ai/v1alpha1`** (not `inference.llmd.ai/v1alpha1`)

### Environment Variables and Helm Values Quick Reference

The `deploy-wva-on-k8s` / `deploy-wva-on-openshift` Makefile targets only propagate `NAMESPACE`, `IMG`, `ENVIRONMENT`, and `LLM_D_RELEASE` directly. Everything else must be **`export`ed** before calling `make`, or set via `helm upgrade --set` for chart-level values.

#### Env vars for `deploy/install.sh` (export before `make`)

| Variable | Description | Default |
|----------|-------------|---------|
| `IMG` | WVA container image (also a Make arg) | `ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest` |
| `WVA_NS` | WVA controller namespace | `workload-variant-autoscaler-system` — set to llm-d namespace (Step 1) |
| `LLMD_NS` | Namespace where llm-d runs | `llm-d-inference-scheduler` |
| `NAMESPACE_SCOPED` | Limit WVA to single namespace | `false` — set `true` for production (watches only `WVA_NS`) |
| `DEPLOY_WVA` | Deploy WVA controller | `true` |
| `DEPLOY_LWS` | Deploy LeaderWorkerSet | `true` — set `false` if already installed |
| `DEPLOY_PROMETHEUS` | Deploy kube-prometheus-stack | `true` — set `false` if already installed |
| `DEPLOY_PROMETHEUS_ADAPTER` | Deploy Prometheus Adapter | `true` — set `false` when using KEDA |
| `SCALER_BACKEND` | `prometheus-adapter` (HPA) or `keda` | `prometheus-adapter` |
| `MONITORING_NAMESPACE` | Prometheus namespace | `workload-variant-autoscaler-monitoring` (k8s) / `openshift-user-workload-monitoring` (OCP) |
| `SKIP_TLS_VERIFY` | Skip TLS for Prometheus | `false` |

#### Env vars for `deploy/install-llmd-infra.sh` (export before `make`)

| Variable | Description | Default |
|----------|-------------|---------|
| `LLM_D_MODELSERVICE_NAME` | llm-d ModelService base name **without** `-decode` suffix | `ms-<GUIDE_NAME>-llm-d-modelservice` |
| `MODEL_ID` | Model identifier | `unsloth/Meta-Llama-3.1-8B` |
| `ACCELERATOR_TYPE` | GPU vendor label (`nvidia`, `amd`, `cpu`) — auto-detected if unset | `H100` |
| `INSTALL_GATEWAY_CTRLPLANE` | Install gateway control plane | `true` — set `false` if already installed |

#### Threshold tuning (ConfigMap — live, no restart required)

Saturation thresholds live in the `wva-saturation-scaling-config` ConfigMap. Edit them directly:

```bash
kubectl edit configmap workload-variant-autoscaler-saturation-scaling-config \
  -n <WVA_NS>
```

| ConfigMap key | Description | Default |
|---------------|-------------|---------|
| `kvCacheThreshold` | KV cache saturation threshold | `0.80` |
| `queueLengthThreshold` | Queue depth saturation threshold | `5` |
| `kvSpareTrigger` | Proactive spare KV trigger | `0.10` |
| `queueSpareTrigger` | Proactive spare queue trigger | `3` |

HPA behavior (stabilization windows, min/max replicas) is set per-HPA resource — patch or re-apply the HPA manifest.

> **Helm chart is deprecated.** Do not use `helm upgrade --set` to tune thresholds. The chart will be removed in a future release.

### ConfigMap Live Tuning

Saturation thresholds live in ConfigMap `wva-saturation-scaling-config`. Changes take effect without controller restart:

```bash
kubectl edit configmap workload-variant-autoscaler-saturation-scaling-config -n <namespace>
```

### Asymmetric Stabilization Windows (post-deploy)

Patch the HPA you created in Step 4e directly:
```bash
kubectl patch hpa <hpa-name> -n <WVA_NS> --type=merge -p '{
  "spec": {
    "behavior": {
      "scaleUp":   {"stabilizationWindowSeconds": 60},
      "scaleDown": {"stabilizationWindowSeconds": 300}
    }
  }
}'
```

### EPP Threshold Alignment

WVA and EPP (Inference Scheduler) must use identical thresholds:

| WVA parameter | EPP parameter |
|---|---|
| `kvCacheThreshold` | `kvCacheUtilThreshold` |
| `queueLengthThreshold` | `queueDepthThreshold` |

After changing thresholds: `kubectl rollout restart deployment/<epp-deployment> -n <namespace>`

### Undeploy

```bash
cd <WVA_REPO_PATH>
WVA_NS=<WVA_NS> NAMESPACE=<WVA_NS> ./deploy/install.sh --undeploy
# or: make undeploy-wva-on-k8s
```

Also delete any VAs, HPAs, or annotated HPAs you created manually:
```bash
kubectl delete variantautoscaling,hpa -n <namespace> --all
```

### Known Issues

See [Troubleshooting.md](./Troubleshooting.md) for common problems including:
- METRICSREADY: False (accelerator label issues)
- HPA showing `<unknown>` (wrong metric or selector)
- Controller not detecting VA resources (namespace-scoping)
- CRD field manager conflicts
- OpenShift Helm chart label bug
- Scale-to-zero not working

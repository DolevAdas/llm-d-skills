---
name: configure-wva-autoscaling-llm-d
description: Configure and deploy Workload Variant Autoscaler (WVA) for llm-d inference deployments. Guides users through namespace selection, WVA repo location, configuration (with presets or custom values), deployment via Makefile + kubectl apply, and verification. Produces a reusable deployment script.
---

## Agent Behavior Rules

1. **Follow steps IN ORDER. Never skip or combine steps.**
2. **STOP after each step and ask for explicit permission to proceed to the next step.**
3. **Do NOT modify existing repository code.** Cloning a missing repo is allowed. Exception: the kustomize symlink fix in Step 4b is a known bug fix — apply it if needed.
4. **Use existing skill scripts when possible** — see [`scripts/SCRIPTS.md`](./scripts/SCRIPTS.md).
5. **Before creating any Kubernetes resource**, state what will be created and why.
6. **After each kubectl/make command**, run a verification check and report the result before continuing.

---

## Step 1 — Select Target Namespace and Deployments

**Ask the user:**

> "Which Kubernetes namespace should WVA monitor?"
> (Provide a single namespace, e.g., `my-llm-ns`)

Export the answer:
```bash
export WVA_NS=<namespace>
```

WVA will be deployed **into** this namespace so it can watch the llm-d workloads there.

Then discover ALL llm-d decode deployments in that namespace:

```bash
kubectl get deployment -n $WVA_NS -l llm-d.ai/role=decode -o custom-columns=NAME:.metadata.name,MODEL:.metadata.labels.llm-d\.ai/model-id,REPLICAS:.spec.replicas
```

If no results, try the alternative label:
```bash
kubectl get deployment -n $WVA_NS -l app.kubernetes.io/part-of=llm-d -o custom-columns=NAME:.metadata.name,REPLICAS:.spec.replicas
```

Also discover EPPs (InferencePool / EndpointPickerPool) and map which deployments each EPP routes to:

```bash
kubectl get inferencepool -n $WVA_NS -o custom-columns=NAME:.metadata.name,SELECTOR:.spec.targetPortNumber 2>/dev/null || \
kubectl get endpointpickerpool -n $WVA_NS -o yaml 2>/dev/null
```

Determine which EPP routes to which decode deployments by inspecting the EPP's selector labels and matching them against the deployments' labels. Deployments served by the **same EPP** share a single WVA saturation-scaling ConfigMap — their thresholds (kv_cache_threshold, queue_length_threshold, etc.) must be identical. Deployments under **different EPPs** can have independent thresholds.

Present ALL findings **grouped by EPP**:

```
Namespace: my-llm-ns
Found 3 llm-d decode deployments across 2 EPPs:

EPP: qwen-epp (routes to 2 deployments — shared WVA config)
  1. optimized-baseline-nvidia-gpu-vllm-decode  (model: Qwen/Qwen3-32B, replicas: 1)
  2. qwen3-8b-decode                            (model: Qwen/Qwen3-8B, replicas: 2)

EPP: gpt-epp (routes to 1 deployment)
  3. ms-gpt-oss-6b-llm-d-modelservice-decode    (model: EleutherAI/gpt-j-6b, replicas: 1)

Note: Deployments within the same EPP share saturation thresholds.
      Deployments in different EPPs can have independent thresholds.
```

If EPP discovery fails (no InferencePool/EndpointPickerPool CRDs), fall back to listing deployments without grouping and inform the user:
> "Could not detect EPP routing. Presenting all deployments ungrouped — if they share an EPP, their saturation thresholds must match."

**STOP. Ask:** "Which deployment(s) should WVA autoscale? (Enter numbers, names, or 'all')"

---

## Step 1b — WVA Repository Location

**Ask the user:**

> "Where is your `llm-d-workload-variant-autoscaler` repository cloned locally? "

If the user provides a path, verify it exists:
```bash
ls <provided-path>/deploy/install.sh 2>/dev/null && echo "Found" || echo "Not found"
```

If the path is valid, export it:
```bash
export WVA_REPO_PATH=<provided-path>
```

If the path is not found or the user does not have a clone, offer to clone it:
> "I can clone the repository for you. Where should I clone it? (default: `~/dev/llm-d-workload-variant-autoscaler`)"

```bash
git clone https://github.com/llm-d/llm-d-workload-variant-autoscaler <target-path>
export WVA_REPO_PATH=<target-path>
```

**`WVA_REPO_PATH` is required for all subsequent steps.**

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

#### Per-model customization

**Ask:** "Do you want the same configuration for all selected deployments, or customize per model?"

- If **same for all** → apply the chosen preset values uniformly. Proceed to auto-detection.
- If **customize per model** → ask the following two questions:

  **Question 1:** "Which configuration parameter(s) would you like to change?"

  Present the available parameters:

  | # | Parameter | Preset Value | Description |
  |---|-----------|--------------|-------------|
  | 1 | `kv_cache_threshold` | *(from preset)* | KV cache % that marks a replica as saturated |
  | 2 | `queue_length_threshold` | *(from preset)* | Queue depth that marks a replica as saturated |
  | 3 | `kv_spare_trigger` | *(from preset)* | Spare KV capacity trigger for scale-up |
  | 4 | `queue_spare_trigger` | *(from preset)* | Spare queue capacity trigger for scale-up |
  | 5 | `scale_up_window` | *(from preset)* | Seconds before scaling up |
  | 6 | `scale_down_window` | *(from preset)* | Seconds before scaling down |
  | 7 | `min_replicas` | *(from preset)* | Minimum replica count |
  | 8 | `max_replicas` | *(user-provided)* | Maximum replica count |
  | 9 | `variant_cost` | `"10.0"` | Cost weight (lower-cost variants scale first) |

  **Question 2:** "Which deployment(s) should get the custom value?"

  List the selected deployments (from Step 1) and let the user pick which ones get the override. Repeat for each parameter the user wants to change.

  > Reminder: deployments within the same EPP must share saturation thresholds (`kv_cache_threshold`, `queue_length_threshold`, `kv_spare_trigger`, `queue_spare_trigger`). Warn the user if they attempt to set different saturation values for deployments in the same EPP.

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
| `kv_spare_trigger` |  Scale up is requested when the average spare KV capacity across non-saturated replicas falls below this value.| `0.10` |
| `queue_spare_trigger` | Scale up is requested when the average spare queue capacity across non-saturated replicas falls below this value. | `3` |
| `scale_up_window` | Seconds to wait before scaling up | `120` |
| `scale_down_window` | Seconds to wait before scaling down | `300` |

The full explanation is in `$WVA_REPO_PATH/docs/developer-guide/saturation-scaling-config.md`.
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
wva_repo_path: $WVA_REPO_PATH  # set from Step 1b

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

Then present a **configuration summary** to the user — do NOT just show the raw YAML. Explain what the config means and show a clear table:

**Brief explanation:**
> "Here's your WVA configuration. The **shared defaults** apply to all deployments unless a specific deployment has an override. WVA will use these thresholds to decide when your models are saturated and need more replicas."

**Shared Defaults Table:**

| Parameter | Value | Meaning |
|-----------|-------|---------|
| KV Cache Threshold | `0.80` | A replica is considered saturated when KV cache exceeds 80% |
| Queue Length Threshold | `5` | A replica is considered saturated when queue depth exceeds 5 |
| KV Spare Trigger | `0.10` | Scale up when average spare KV across healthy replicas < 10% |
| Queue Spare Trigger | `3` | Scale up when average spare queue capacity < 3 |
| Scale-up Window | `120s` | Wait 2 min of sustained saturation before adding replicas |
| Scale-down Window | `300s` | Wait 5 min of low utilization before removing replicas |

**Per-Deployment Table:**

| Deployment | Model | Min | Max | Cost | Overrides |
|-----------|-------|-----|-----|------|-----------|
| optimized-baseline-nvidia-gpu-vllm-decode | Qwen/Qwen3-32B | 1 | 10 | "10.0" | *(none — uses defaults)* |
| ms-gpt-oss-6b-llm-d-modelservice-decode | EleutherAI/gpt-j-6b | 1 | 5 | "5.0" | kv=0.70, queue=3, up=60s, down=180s |

If any deployments share an EPP, add a note:
> "Deployments 1 and 2 share EPP `qwen-epp` — their saturation thresholds are identical as required."

**STOP. Ask:** "Configuration is ready. Shall I show you the deployment plan? (yes/no)"

---

## Step 3 — Deployment Plan

Write a concise plan and display it to the user.

### 3a. Selected Deployments and Models

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
Step 4a:   Pre-flight checks (existing controller, Prometheus Adapter availability)
Step 4a.5: Detect monitoring namespace (OpenShift only)
Step 4b:   Deploy WVA controller + Prometheus Adapter via Makefile (Kustomize)
Step 4c:   Verify controller running and watching namespace
Step 4d:   Add accelerator labels to selected decode deployments
Step 4e:   Apply VA + HPA for selected decode deployments via kubectl apply
Step 4f:   Verify VAs/HPAs are ready and have valid targets
Step 5:    Generate reusable deployment script
```

### 3d. References

| Resource | Link / Command |
|----------|---------------|
| WVA User Guide | `${WVA_REPO_PATH}/deploy/README.md` |
| Kustomize overlays | `${WVA_REPO_PATH}/config/default/` (k8s), `config/openshift/` (OCP) |
| Troubleshooting | [Troubleshooting.md](./docs/Troubleshooting.md) |
| WVA GitHub | https://github.com/llm-d/llm-d-workload-variant-autoscaler |

**STOP. Ask:** "Does this plan look correct? Ready to deploy? (yes/no/adjust)"

---

## Step 4 — Deploy

Execute each sub-step one at a time, verifying after each.

---

### 4a. Pre-flight Checks

Before running the checks, explain to the user what will be verified:

> "Running pre-flight checks. This will verify:
> - **No existing WVA controller** is already running in `$WVA_NS` (to avoid conflicts with a stale deploy)
> - **Prometheus Adapter** (external metrics API) is available — WVA needs it to expose `wva_desired_replicas` to the HPA
> - **Cluster connectivity** — confirming `kubectl` can reach the cluster"

```bash
cd skills/configure-wva-autoscaling-llm-d/scripts/
./preflight-check.sh $WVA_NS --scaler-backend <prometheus-adapter|keda>
```

If a stale WVA controller is found, ask permission to remove:
```bash
cd $WVA_REPO_PATH
WVA_NS=$WVA_NS ./deploy/install.sh --undeploy
```

**STOP. Ask:** "Pre-flight checks complete. Ready to proceed? (yes/no)"

---

### 4a.5. Detect Monitoring Namespace (OpenShift only)

If the platform is **OpenShift**, detect the namespace where Prometheus Adapter is registered. This is needed so the Makefile can configure the adapter to scrape the right Prometheus instance:

```bash
MONITORING_NAMESPACE=$(kubectl get apiservice v1beta1.external.metrics.k8s.io \
  -o jsonpath='{.spec.service.namespace}' 2>/dev/null)
export MONITORING_NAMESPACE
echo "Monitoring namespace: $MONITORING_NAMESPACE"
```

Expected values: `openshift-user-workload-monitoring` or `openshift-monitoring`.

If the command returns empty, default to `openshift-user-workload-monitoring` and inform the user.

> Skip this step on plain Kubernetes — the Makefile default (`workload-variant-autoscaler-monitoring`) applies there.

---

### 4b. Deploy WVA Controller (Makefile + Kustomize)

`deploy/install.sh` deploys the WVA controller via Kustomize, plus the scaler backend. **It does NOT create VariantAutoscaling or HPA resources** — those are applied in step 4e for each selected deployment.

> **Full deploy commands, pre-checks (Go PATH, kustomize symlink fix), and platform-specific instructions:** see [`docs/DEPLOY-CONTROLLER.md`](./docs/DEPLOY-CONTROLLER.md).

Follow the steps in that file:
1. Verify Go is in PATH (required for Makefile tooling)
2. Apply kustomize symlink fix if not already patched
3. Export all required env vars and run the appropriate `make` target (Kubernetes or OpenShift)

---

### 4c. Verify Controller

```bash
kubectl get deployment workload-variant-autoscaler-controller-manager -n $WVA_NS
kubectl logs -n $WVA_NS -l control-plane=controller-manager --tail=20 | grep -i "watching"
```

Expected: controller `1/1 Ready`, logs contain `"Watching single namespace"` with `"namespace":"<WVA_NS>"`.

**STOP. Report and ask:** "Controller deployed. Proceed to add accelerator labels? (yes/no)"

---

### 4d. Add Accelerator Labels

**Why this step is needed:** WVA uses the `inference.optimization/acceleratorName` label on each decode deployment to identify the GPU vendor backing it. This label is required for the VariantAutoscaling CRD to become `METRICSREADY: True` — without it, WVA cannot match the deployment to its GPU inventory and will not emit scaling metrics.

Auto-detect the accelerator for each selected deployment:

```bash
ACCELERATOR=$(skills/configure-wva-autoscaling-llm-d/scripts/detect-accelerator.sh \
  $WVA_NS <deployment-name>)
echo "Detected: $ACCELERATOR"
```

If the script exits with an error, ask the user: "Could not auto-detect accelerator for `<deployment>`. Is it `nvidia`, `amd`, or `cpu`?"

> Valid values: `nvidia` (covers H100, A100, L4, A10, etc.), `amd`, `cpu`. **Do not assume `nvidia`.**

Apply the label for EACH selected deployment:
```bash
kubectl label deployment <deployment-name> -n $WVA_NS \
  inference.optimization/acceleratorName=$ACCELERATOR --overwrite
```

**Verify:**
```bash
kubectl get deployment -n $WVA_NS \
  -o custom-columns=NAME:.metadata.name,ACCELERATOR:.metadata.labels."inference\.optimization/acceleratorName"
```

---

### 4e. Apply VA + HPA for Selected Decode Deployments

Apply a VariantAutoscaling + HPA for **each selected decode deployment** — the `install.sh` from step 4b does not create any VA or HPA resources.

> **Namespace scope**: With `NAMESPACE_SCOPED=true` the controller watches only `WVA_NS`. Deploy VAs and HPAs to that **same** namespace.

> **⚠️ TEMPORARY NOTE — VA + HPA path is required until image is updated:**
> The annotation-based HPA mode (`--mode annotated`) was introduced in PR #1123 of the WVA repo. As of this writing, the published `ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest` image predates that PR and does not support it. **Use `--mode va-hpa` (or `--mode keda`) until the image is updated past PR #1123.**
> Once the image includes PR #1123, `--mode annotated` can be used and this note can be removed.

Run `apply-hpa.sh` for **each** selected deployment:

**VA + HPA (Prometheus Adapter / HPA backend):**
```bash
skills/configure-wva-autoscaling-llm-d/scripts/apply-hpa.sh \
  --mode va-hpa \
  --namespace $WVA_NS \
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
  --namespace $WVA_NS \
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

> **`type: AverageValue, averageValue: "1"`**: HPA computes `desiredReplicas = currentReplicas × (metric / 1)` — directly matching WVA's recommendation.

---

### 4f. Verify All Resources

**Wait ~2 minutes for Prometheus to scrape metrics, then verify:**

```bash
skills/configure-wva-autoscaling-llm-d/scripts/verify-wva.sh $WVA_NS
```

If verification is incomplete, run the troubleshoot script:
```bash
skills/configure-wva-autoscaling-llm-d/scripts/troubleshoot-scaling.sh $WVA_NS
```

**Common causes and resolutions:**

| Symptom | Cause | Resolution |
|---------|-------|------------|
| `METRICSREADY: False` | Accelerator label missing on deployment or VA | Re-run Step 4d |
| HPA `<unknown>` | Wrong metric name, `variant_name` mismatch, or Prometheus Adapter not running | Check HPA spec and Prometheus Adapter pod |
| `ScalingDisabled` | Deployment at 0 replicas — HPA cannot scale from 0 | Scale to ≥1 first: `kubectl scale deployment <name> -n $WVA_NS --replicas=1`, or use KEDA for scale-to-zero |
| `METRICSREADY: False` with pods Pending | GPUs not yet available on the cluster | **This is not a WVA error.** WVA is correctly configured. Inform the user: "WVA is ready and will activate automatically once GPU pods are scheduled. This may take time depending on cluster GPU availability — no further action needed." Continue to Step 5. |
| EPP pod returning 500 in WVA logs | EPP has no running decode pods to proxy | Expected when the backing deployment is at 0 replicas. Will resolve once the deployment scales up. |

**STOP. Report final status for each model:**
```
Model 1 (openai/gpt-oss-20b):  VA=METRICSREADY:True,  HPA=2/2 ✓
Model 2 (Qwen/Qwen3-32B):      VA=created, HPA=ScalingDisabled (0 replicas — awaiting GPU) ⚠
```

**Ask:** "All resources applied. Proceed to generate deployment script? (yes/no)"

---

## Step 5 — Generate Reusable Deployment Script

Generate a script that can reproduce this entire deployment:

```bash
cd skills/configure-wva-autoscaling-llm-d/scripts/

./generate-deploy-script.sh \
  --namespace $WVA_NS \
  --deployment <first-deployment-name> \
  --wva-repo $WVA_REPO_PATH \
  --model-id "<model-id>" \
  --variant-cost "<variant_cost>" \
  --accelerator <detected-accelerator> \
  --min-replicas <min_replicas> \
  --max-replicas <max_replicas> \
  --kv-threshold <kv_cache_threshold> \
  --queue-threshold <queue_length_threshold> \
  --scale-up-window <scale_up_window> \
  --scale-down-window <scale_down_window> \
  --output deploy-wva-$WVA_NS.sh \
  --non-interactive
```

For additional models, append their `apply-hpa.sh` commands to the generated script.

Tell the user:
> "Deployment script saved to `<path>`. To reproduce the full WVA setup:
> ```bash
> ./<script-name>.sh
> ```"

**STOP. Ask:** "Deployment complete. Would you like to run a load test to verify scaling works? (yes/no)"

---

## Step 6 — Optional Load Test

**Only run if user says yes.**

> **Full load test instructions** (run script, fallback to direct pod IP, monitoring commands, expected results): see [`docs/LOAD-TEST.md`](./docs/LOAD-TEST.md).

Quick path:
```bash
cd skills/configure-wva-autoscaling-llm-d/scripts/
./test-wva-scaling.sh $WVA_NS <deployment-name> "<model-id>" 200
```

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
  - Config YAML:    scripts/configs/wva-<namespace>.yaml
  - Deploy script:  scripts/deploy-wva-<namespace>.sh

Commands:
  Redeploy:  ./scripts/deploy-wva-<namespace>.sh
  Verify:    ./scripts/verify-wva.sh <namespace>
  Test:      ./scripts/test-wva-scaling.sh <namespace> <deployment>
  Undeploy:  cd $WVA_REPO_PATH && WVA_NS=$WVA_NS ./deploy/install.sh --undeploy
============================================
```

---

## Reference

> **Full reference** (env vars, key constraints, threshold tuning, EPP alignment, stabilization windows, undeploy, known issues): see [`docs/REFERENCE.md`](./docs/REFERENCE.md).

Key points to remember during deployment:
- HPA metric must be `wva_desired_replicas` — the only metric Prometheus Adapter exposes
- `variantCost` must be a string (e.g., `"10.0"` not `10.0`)
- API version: `llmd.ai/v1alpha1` (not `inference.llmd.ai/v1alpha1`)
- VA must have accelerator label for `METRICSREADY: True`
- Target deployment must have >= 1 replica (HPA cannot scale from 0 without KEDA)
- WVA and EPP saturation thresholds must match

For undeploy: `cd $WVA_REPO_PATH && WVA_NS=$WVA_NS ./deploy/install.sh --undeploy`

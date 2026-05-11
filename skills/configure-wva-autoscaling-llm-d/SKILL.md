---
name: configure-wva-autoscaling-llm-d
description: Configure and optimize Workload Variant Autoscaler (WVA) for llm-d inference deployments. Use when users want to set up autoscaling based on KV cache saturation, configure multi-variant cost optimization, tune saturation thresholds, enable scale-to-zero, or troubleshoot WVA behavior. Helps translate user requirements like "I want aggressive scaling" or "optimize for cost across H100 and A100 variants" into proper WVA configuration.
---
## Command Execution Notice

**Before executing any command, I will:**
1. **Explain what the command does** - Clear description of purpose and expected outcome
2. **Show the actual command** - The exact command to be executed
3. **Explain why it's needed** - How it fits into the workflow

## Critical Rules

1. **Do NOT modify existing repository code** — Cloning a missing repository is allowed and required, but never edit code you did not create. Copy to a new location if adjustment is needed.

2. **ALWAYS use existing skill scripts first** — Use scripts in [`scripts/`](./scripts/SCRIPTS.md). Only perform manual edits if scripts fail due to non-standard deployment structure.

3. **Verify cluster resources** — Check available GPU/RDMA resources before applying changes.

4. **Notify before creating resources** — Before creating ANY Kubernetes resource, state what will be created and why. Never create silently.

---

## Core Workflow

Follow these steps **in order**. Do not skip ahead.

---

### Step 1 — Ask About Target Namespace(s) (FIRST QUESTION)

**This is always the first question.** Never proceed to configuration before the user has confirmed which namespace(s) WVA should monitor.

Ask the user:

> "Which Kubernetes namespace(s) do you want WVA to monitor? (e.g., `my-llm-ns` or `team-a-prod, team-b-prod`)"

Then, for each namespace the user names, **discover its llm-d deployments before continuing**:

```bash
# List all llm-d deployments in the namespace
kubectl get deployment -n <namespace> -l llm-d.ai/role=decode
kubectl get deployment -n <namespace> -l app.kubernetes.io/part-of=llm-d
```

Present the findings:

```
Namespace: my-llm-ns
  Found 2 llm-d deployments:
    • optimized-baseline-nvidia-gpu-vllm-decode  (model: Qwen/Qwen3-32B, replicas: 1)
    • ms-gpt-oss-6b-llm-d-modelservice-decode    (model: EleutherAI/gpt-j-6b, replicas: 1)
```

**Architecture implication — one controller per namespace, one VA + scaler per model:**

A single WVA controller is namespace-scoped and watches **all** VariantAutoscaling resources in the namespace. You do not need one controller per model — only one controller per namespace. The Helm chart creates the controller plus VA + scaler (HPA or ScaledObject, depending on `SCALER_BACKEND`) for **one model**. Additional models in the same namespace get their own VA + scaler applied via `kubectl apply`, picked up automatically by the running controller.




### Step 2 — Gather Configuration

After the namespace(s) and deployments are confirmed, collect the following. For each namespace with multiple models, ask for per-model values where they differ.

**First, ask the user:**

> "Do you already know the values you want to set, or would you like help deciding based on your requirements (latency, cost, stability)?"

- If they **know the values** → collect directly and proceed.
- If they **need guidance** → use the table below to help them decide.

**Scaling backend (ask once per namespace):**

| Backend | `SCALER_BACKEND` value | When to use |
|---------|----------------------|-------------|
| HPA (Prometheus Adapter) | `prometheus-adapter` | Standard; works out-of-box with kube-prometheus-stack or OpenShift monitoring |
| KEDA | `keda` | KEDA already installed, or scale-to-zero is needed |

**Per-namespace settings** (shared across all models in the namespace):

| Parameter | What to ask | Default | Guidance |
|-----------|-------------|---------|----------|
| `KV_CACHE_THRESHOLD` | KV cache saturation threshold | `0.80` | `0.70` for faster response, `0.90` for cost savings |
| `QUEUE_LENGTH_THRESHOLD` | Queue depth threshold | `5` | `3` for low latency, `8` for cost savings |
| `KV_SPARE_TRIGGER` | Proactive scale-up when spare KV < trigger | `0.10` | Lower = more eager scale-up |
| `QUEUE_SPARE_TRIGGER` | Proactive scale-up when spare queue < trigger | `3` | Lower = more eager scale-up |
| `HPA_STABILIZATION_SECONDS` | Scale-up AND scale-down window (symmetric) | `240` | `60–120` responsive, `300` conservative |
| `SCALER_BACKEND` | Scaler backend type | `prometheus-adapter` | See table above |

**Per-model settings** (ask for each model separately):

| Parameter | What to ask | Notes |
|-----------|-------------|-------|
| `MODEL_ID` | Model identifier | e.g., `"Qwen/Qwen3-32B"` — auto-detect from deployment labels if possible |
| `ACCELERATOR_TYPE` | GPU vendor | Valid values: `nvidia`, `amd`, `cpu` — this is the vendor label, **not** the GPU model. `H100`, `A100`, `L4` are GPU models; they all use `nvidia` as the label value. |
| `HPA_MIN_REPLICAS` | Min replicas for this model | Default `1`; use `0` only with KEDA (HPA cannot scale from 0). Can differ per model. |
| Max replicas | Max replicas | Set via `--set hpa.maxReplicas=N` post-deploy; Makefile default is `2` |
| Variant cost | Cost weight for multi-model priority | Lower cost = scales first; e.g., cheaper model gets `"50"`, expensive gets `"100"` |

> **Asymmetric stabilization**: `HPA_STABILIZATION_SECONDS` sets both windows to the same value. For different scale-up vs scale-down windows, use `helm upgrade` after the initial deploy — see the Asymmetric Windows note in the Reference section below.

---

### Step 3 — Choose a Save Location, Write the Plan, and Confirm

Before running any commands:

**1. Scan the repo to find a suitable default save path.**
Look for directories that already contain deployment plans or deployment artifacts

**2. Ask the user:**

> "Where should I save the deployment plan? 
> Suggested default: `<discovered-directory>/wva-<namespace>-<YYYYMMDD>/plan.md`
> Press enter to use the default, or provide a different path."

Use whatever path the user confirms (or the default if they press enter).

**3. Write the plan file** at the confirmed path. The plan must include:
- Architecture diagram (one controller + N VA+scaler pairs)
- Configuration table (all parameters, per-model where they differ)
- Exact commands to run (including the prerequisite Helm check, both Makefile and kubectl apply steps)
- Expected resources to be created

**4. Ask the user:**

> "I've written the deployment plan to `<confirmed-path>`. Please review it and let me know when you're ready to deploy."

**Do not run any deployment command until the user confirms.**

---

### Step 4 — Deploy

#### Prerequisite — Check for Existing WVA Release

```bash
helm list -n <namespace> | grep workload-variant-autoscaler
```

If a stale release exists (e.g., controller was deleted but Helm release remains), remove it first:

```bash
helm uninstall workload-variant-autoscaler -n <namespace>
```

> This removes only Helm-managed resources (RBAC, ConfigMaps, Helm metadata). Model deployments are **not** touched.

#### 4a — First Model: Deploy Controller + VA + Scaler via Makefile

Use the appropriate target for your platform:

**Kubernetes:**
```bash
cd ${WVA_REPO_PATH}

make deploy-wva-on-k8s \
  IMG=ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest \
  WVA_NS=<namespace> \
  LLMD_NS=<namespace> \
  NAMESPACE_SCOPED=true \
  DEPLOY_LLM_D=false \
  DEPLOY_LWS=false \
  DEPLOY_VA=true \
  DEPLOY_HPA=true \
  LLM_D_MODELSERVICE_NAME=<deployment-name-without-decode-suffix> \
  MODEL_ID="<model-id>" \
  ACCELERATOR_TYPE=<nvidia|amd|cpu> \
  KV_CACHE_THRESHOLD=<kv_threshold> \
  QUEUE_LENGTH_THRESHOLD=<queue_threshold> \
  KV_SPARE_TRIGGER=<kv_spare_trigger> \
  QUEUE_SPARE_TRIGGER=<queue_spare_trigger> \
  HPA_MIN_REPLICAS=<min_replicas> \
  HPA_STABILIZATION_SECONDS=<stabilization_seconds> \
  SCALER_BACKEND=<prometheus-adapter|keda>
```

**OpenShift (adds TLS and monitoring namespace):**
```bash
cd ${WVA_REPO_PATH}

E2E_TESTS_ENABLED=true INSTALL_GATEWAY_CTRLPLANE=false \
make deploy-wva-on-openshift \
  IMG=ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest \
  WVA_NS=<namespace> \
  LLMD_NS=<namespace> \
  NAMESPACE_SCOPED=true \
  DEPLOY_LLM_D=false \
  DEPLOY_LWS=false \
  DEPLOY_VA=true \
  DEPLOY_HPA=true \
  LLM_D_MODELSERVICE_NAME=<deployment-name-without-decode-suffix> \
  MODEL_ID="<model-id>" \
  ACCELERATOR_TYPE=<nvidia|amd|cpu> \
  KV_CACHE_THRESHOLD=<kv_threshold> \
  QUEUE_LENGTH_THRESHOLD=<queue_threshold> \
  KV_SPARE_TRIGGER=<kv_spare_trigger> \
  QUEUE_SPARE_TRIGGER=<queue_spare_trigger> \
  HPA_MIN_REPLICAS=<min_replicas> \
  HPA_STABILIZATION_SECONDS=<stabilization_seconds> \
  SCALER_BACKEND=prometheus-adapter \
  MONITORING_NAMESPACE=openshift-monitoring \
  SKIP_TLS_VERIFY=true
```

> **`LLM_D_MODELSERVICE_NAME` on OpenShift**: The Makefile template appends `-decode` to this value to form the full deployment name. Set it **without** the `-decode` suffix. Example: deployment `my-model-decode` → `LLM_D_MODELSERVICE_NAME=my-model`.

> **OpenShift gateway prompt bypass**: `E2E_TESTS_ENABLED=true INSTALL_GATEWAY_CTRLPLANE=false` prevents the deploy script from stopping at an interactive gateway installation prompt. Without these, the make command exits with code 2.

**What the Makefile creates:**

Per-namespace (created once, shared by all models):
- `workload-variant-autoscaler-controller-manager` — WVA controller, namespace-scoped

Per-model (created for the first model only — additional models need Step 4b):
- `workload-variant-autoscaler-va` — VariantAutoscaling resource
- `workload-variant-autoscaler-hpa` — HPA (`SCALER_BACKEND=prometheus-adapter`) **or** `workload-variant-autoscaler-scaledobject` — ScaledObject (`SCALER_BACKEND=keda`)

#### 4b — Additional Models in the Same Namespace: VA + HPA via kubectl

The controller from Step 4a is already running. Apply a **VariantAutoscaling + HPA** (or ScaledObject) for each additional model — the controller picks them up automatically.

```bash
kubectl apply -n <namespace> -f - <<'EOF'
apiVersion: llmd.ai/v1alpha1
kind: VariantAutoscaling
metadata:
  name: <model-short-name>-va
  namespace: <namespace>
  labels:
    inference.optimization/acceleratorName: <nvidia|amd|cpu>
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: <full-deployment-name>
  modelID: "<model-id>"
  variantCost: "<cost>"
  minReplicas: <min>
  maxReplicas: <max>
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: <model-short-name>-hpa
  namespace: <namespace>
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: <full-deployment-name>
  minReplicas: <min>
  maxReplicas: <max>
  metrics:
  - type: External
    external:
      metric:
        name: wva_desired_replicas
        selector:
          matchLabels:
            variant_name: <model-short-name>-va
            exported_namespace: <namespace>
      target:
        type: AverageValue
        averageValue: "1"
  behavior:
    scaleUp:
      stabilizationWindowSeconds: <stabilization_seconds>
    scaleDown:
      stabilizationWindowSeconds: <stabilization_seconds>
EOF
```

> **HPA metric**: Prometheus Adapter only exposes `wva_desired_replicas`. Do NOT use `wva_kv_cache_saturation` or `wva_queue_depth_saturation` in the HPA — these metrics are not served by the adapter and will cause `<unknown>` in `kubectl get hpa`.

> **`type: AverageValue, averageValue: "1"`**: HPA scales `desiredReplicas = currentReplicas × (metricValue / averageValue)`. With `averageValue: "1"`, when WVA emits `desiredReplicas: 2` the HPA computes `2/1 = 2` replicas — exactly matching WVA's recommendation.

#### 4c — Non-default maxReplicas or variantCost

The Makefile has no dedicated variable for `maxReplicas` or `variantCost`. Override via `helm upgrade` immediately after deploy:

```bash
helm upgrade workload-variant-autoscaler ${WVA_REPO_PATH}/charts/workload-variant-autoscaler \
  -n <namespace> \
  --reuse-values \
  --set hpa.maxReplicas=10 \
  --set va.variantCost="70"
```

> `variantCost` must be a **string** (e.g., `"70"` not `70`).

---

### Step 5 — Verify

#### Controller

```bash
# Controller running?
kubectl get deployment workload-variant-autoscaler-controller-manager -n <namespace>

# Namespace-scoping correct?
kubectl logs -n <namespace> -l control-plane=controller-manager | grep "Watching"
# Expected: "Watching single namespace: <namespace>"

# VA and scaler resources created?
kubectl get variantautoscaling,hpa -n <namespace>          # prometheus-adapter backend
kubectl get variantautoscaling,scaledobject -n <namespace> # keda backend
```

#### Metrics Ready (wait ~2 minutes for first Prometheus scrape)

```bash
kubectl get variantautoscaling -n <namespace>
# METRICSREADY should be True for all VAs
```

If `METRICSREADY` stays `False`, check the accelerator label on the VariantAutoscaling:
```bash
kubectl get variantautoscaling <name> -n <namespace> -o jsonpath='{.metadata.labels}'
# Must include: inference.optimization/acceleratorName: <accelerator>
```

#### Scaler Metrics

**Prometheus Adapter (HPA):**
```bash
kubectl get hpa -n <namespace>
# TARGETS should show a number (e.g., 1/1) not <unknown>
```

If HPA shows `<unknown>`, the metric selector labels don't match the VA name or namespace. Verify:
```bash
kubectl describe hpa <name> -n <namespace> | grep -A5 "Conditions:"
```

**KEDA (ScaledObject):**
```bash
kubectl get scaledobject -n <namespace>
# READY should be True, ACTIVE shows whether scaling is currently triggered
kubectl describe scaledobject <name> -n <namespace>
```

#### Success Criteria

- ✅ `workload-variant-autoscaler-controller-manager` Running
- ✅ WVA logs: `"Watching single namespace: <namespace>"`
- ✅ All VAs show `METRICSREADY: True`
- ✅ All HPAs show valid metric targets (not `<unknown>`) — or ScaledObjects show `READY: True` if using KEDA
- ✅ No errors in WVA controller logs

Use the verification scripts for a comprehensive check:
```bash
./scripts/verify-wva.sh <namespace>
./scripts/troubleshoot-metrics.sh <namespace> <pod-name>
./scripts/troubleshoot-scaling.sh <namespace>
```

---

### Step 6 — Optional Load Test (Ask User First)

After verification succeeds, **always ask the user before running any load test**:

> "WVA is deployed and verified. Would you like to run a load test to see it scale in action? This sends concurrent inference requests to trigger scale-up — it's optional and safe to skip."

**Only proceed if the user says yes.**

#### What the Load Test Does

1. Sends `N` concurrent streaming requests with high `max_tokens` to a target deployment
2. Monitors vLLM metrics (`kv_cache_usage_perc`, `num_requests_running`, `num_requests_waiting`)
3. Watches WVA logs for `shouldScaleUp: true` and `desiredReplicas` change
4. Waits for the scaler stabilization window to elapse (HPA or ScaledObject)
5. Confirms the deployment scaled up

#### Running the Load Test

**Via script** (recommended):
```bash
cd skills/configure-wva-autoscaling-llm-d/scripts
./test-wva-scaling.sh <namespace> <deployment-name> "<model-id>" 200
```

**Manual approach** (when gateway/InferencePool is unavailable — direct pod IP):
```bash
# Find the pod IP
kubectl get pod -n <namespace> -l llm-d.ai/role=decode -o wide

# Launch from inside the cluster (streaming, high max_tokens fills KV cache)
kubectl run wva-load-test -n <namespace> --rm -i --restart=Never \
  --image=curlimages/curl:latest \
  --command -- sh -c '
POD_IP="<pod-ip>"
MODEL="<model-id>"
for i in $(seq 1 200); do
  curl -s -N -X POST "http://$POD_IP:8000/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Write a long detailed essay. Part $i.\"}],\"max_tokens\":4000,\"stream\":true}" \
    > /dev/null 2>&1 &
done
wait'
```

#### Monitoring Scale-Up Progress

While load test runs, in a separate terminal:
```bash
# Watch WVA saturation analysis
kubectl logs -n <namespace> -l control-plane=controller-manager -f | grep -E "shouldScaleUp|desiredReplicas|avgSpareKv|avgSpareQueue"

# Watch scaler decision (use whichever backend applies)
kubectl get hpa <hpa-name> -n <namespace> -w          # prometheus-adapter
kubectl get scaledobject <name> -n <namespace> -w     # keda

# Watch deployment replica count
kubectl get deployment <deployment-name> -n <namespace> -w
```

#### Expected Sequence

1. vLLM metrics: `num_requests_waiting` > 0, `kv_cache_usage_perc` rises
2. WVA log: `"shouldScaleUp": true, "desiredReplicas": 2`
3. Scaler reacts: HPA TARGETS changes to `2/1 (avg)` and condition `ScaleUpStabilized` appears — or ScaledObject ACTIVE becomes `True`
4. After stabilization window elapses: `spec.replicas` → 2, new pod starts

> **Scale-up stabilization**: `HPA_STABILIZATION_SECONDS` applies to **both** scale-up and scale-down by default. The scaler will not act until it has seen `desiredReplicas: 2` consistently for the full stabilization window. This prevents flapping from transient spikes.

#### If Scale-Up Doesn't Trigger

- **KV cache insufficient**: Qwen3-32B on 2×H100-80GB has ~80 GB KV cache — 50 short requests may not fill it. Send more requests with higher `max_tokens` (4000+) and use streaming mode so tokens stay in cache during generation.
- **Already at maxReplicas**: `kubectl get hpa -n <namespace>` (or `kubectl get scaledobject`) — check MAXPODS / `maxReplicaCount`.
- **Threshold too high**: If `avgSpareKv` stays near the default (e.g., 0.7) and `shouldScaleUp` is false, your workload doesn't hit the threshold. Lower `KV_CACHE_THRESHOLD` or `QUEUE_LENGTH_THRESHOLD` and redeploy.
- **Stabilization window**: If WVA emitted `desiredReplicas: 2` but the scaler hasn't acted yet, wait for the stabilization window to elapse. HPA: check the `ScaleUpStabilized` condition in `kubectl describe hpa`. KEDA: check `kubectl describe scaledobject`.

---

## Known Issues and Gotchas

These are real issues encountered during deployment. Read before debugging.

### 1. OpenShift: Interactive Gateway Prompt Causes Exit Code 2

**Symptom**: `make deploy-wva-on-openshift` exits with code 2 after printing a gateway installation prompt.

**Cause**: The deploy script calls `prompt_gateway_installation()` when `E2E_TESTS_ENABLED` is not `true`. This is an interactive prompt that blocks non-TTY execution.

**Fix**: Prefix the make command with:
```bash
E2E_TESTS_ENABLED=true INSTALL_GATEWAY_CTRLPLANE=false make deploy-wva-on-openshift ...
```

### 2. CRD Field Manager Conflict

**Symptom**: Helm fails with:
```
failed to install CRD crds/llmd.ai_variantautoscalings.yaml: conflict occurred with
"kubectl-client-side-apply"
```

**Cause**: The CRD was previously applied directly via `kubectl apply`, creating a client-side field manager. Helm uses server-side apply, which conflicts.

**Fix**: Force server-side apply on the CRD before re-running the Makefile:
```bash
kubectl apply --server-side --force-conflicts \
  -f ${WVA_REPO_PATH}/charts/workload-variant-autoscaler/crds/llmd.ai_variantautoscalings.yaml
```

### 3. HPA Shows `<unknown>` for Metrics

**Symptom**: `kubectl get hpa` shows `<unknown>/1` for all metrics.

**Causes and fixes:**

| Cause | Fix |
|-------|-----|
| Wrong metric name in HPA (e.g., `wva_kv_cache_saturation`) | Use only `wva_desired_replicas` — it's the only metric Prometheus Adapter exposes for WVA |
| HPA selector missing `exported_namespace` label | Add `exported_namespace: <namespace>` to `matchLabels` |
| HPA selector `variant_name` doesn't match VA resource name | Verify with `kubectl get variantautoscaling -n <namespace>` |
| Prometheus Adapter not installed | Check `kubectl get apiservice v1beta1.external.metrics.k8s.io` |

### 4. `METRICSREADY: False` on VariantAutoscaling

**Symptom**: `kubectl get variantautoscaling` shows `METRICSREADY: False`. WVA logs: `"Skipping status update for VA without accelerator info"`.

**Fix**: Add the accelerator label to the VariantAutoscaling:
```bash
kubectl patch variantautoscaling <name> -n <namespace> --type=merge \
  -p '{"metadata":{"labels":{"inference.optimization/acceleratorName":"nvidia"}}}'
```

### 5. "No dispatch rate" Warning in WVA Logs

**Symptom**: `"Pod has vLLM metrics but no dispatch rate — possible pod/pod_name label mismatch"`.

**Impact**: Informational only. Saturation analysis still works using KV cache and queue depth. Scaling proceeds normally.

**Cause**: vLLM pod labels don't include the `pod_name` label that WVA uses to correlate dispatch metrics. Does not block scaling.

### 6. Load Test Not Triggering Scale-Up on Large Models

**Symptom**: WVA logs show `avgSpareKv: 0.7, shouldScaleUp: false` even after sending requests.

**Cause**: Large models (e.g., Qwen3-32B on 2×H100-80GB) have enormous KV caches. Short requests with small `max_tokens` complete and free KV slots before the next Prometheus scrape (every 30s).

**Fix**: Use streaming requests with `max_tokens=4000` and send 150–200 concurrent requests. Streaming keeps KV slots occupied during the entire generation, allowing Prometheus to observe the saturation.

---

## Reference

### Makefile Variables

Run `make help` in `${WVA_REPO_PATH}` for all 40+ targets.

*Infrastructure:*

| Variable | Description | Default |
|----------|-------------|---------|
| `IMG` | WVA container image | `ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest` |
| `WVA_NS` | Target namespace for WVA deployment | `workload-variant-autoscaler-system` |
| `LLMD_NS` | Namespace where llm-d workloads run | same as `WVA_NS` |
| `NAMESPACE_SCOPED` | Limit WVA to single namespace | `false` — **always set `true`** |
| `DEPLOY_LLM_D` | Deploy llm-d stack | `true` — set `false` when llm-d already exists |
| `DEPLOY_LWS` | Deploy LeaderWorkerSet CRDs | `true` — set `false` if not needed |
| `DEPLOY_VA` | Deploy VariantAutoscaling via chart | `false` — **set `true`** |
| `DEPLOY_HPA` | Deploy scaler via chart (HPA or ScaledObject, based on `SCALER_BACKEND`) | `false` — **set `true`** |

*User configuration:*

| Variable | Description | Default |
|----------|-------------|---------|
| `LLM_D_MODELSERVICE_NAME` | Deployment name **without** `-decode` suffix | — |
| `MODEL_ID` | Model identifier | `unsloth/Meta-Llama-3.1-8B` |
| `ACCELERATOR_TYPE` | GPU type | `H100` |
| `KV_CACHE_THRESHOLD` | KV cache saturation threshold | `0.80` |
| `QUEUE_LENGTH_THRESHOLD` | Queue depth saturation threshold | `5` |
| `KV_SPARE_TRIGGER` | Proactive scale-up spare KV trigger | `0.10` |
| `QUEUE_SPARE_TRIGGER` | Proactive scale-up spare queue trigger | `3` |
| `HPA_MIN_REPLICAS` | Minimum replicas | `1` |
| `HPA_STABILIZATION_SECONDS` | Scale-up and scale-down window (symmetric) | `240` |
| `SCALER_BACKEND` | `prometheus-adapter` or `keda` | `prometheus-adapter` |
| `MONITORING_NAMESPACE` | Prometheus namespace (OpenShift) | `monitoring` |
| `SKIP_TLS_VERIFY` | Skip TLS for Prometheus (OpenShift) | `false` |

### Parameter → Resource Mapping

The saturation thresholds land in a ConfigMap (not the HPA or VA directly). This is why you can tune them post-deploy without redeploying the controller:

**ConfigMap `wva-saturation-scaling-config`:**

| Makefile var | ConfigMap field | Meaning |
|---|---|---|
| `KV_CACHE_THRESHOLD` | `kvCacheThreshold` | Replica saturated when KV cache ≥ this |
| `QUEUE_LENGTH_THRESHOLD` | `queueLengthThreshold` | Replica saturated when queue depth ≥ this |
| `KV_SPARE_TRIGGER` | `kvSpareTrigger` | Proactively scale up when spare KV < this |
| `QUEUE_SPARE_TRIGGER` | `queueSpareTrigger` | Proactively scale up when spare queue < this |

The ConfigMap is watched live — editing it takes effect without restarting the controller.

**Asymmetric stabilization windows** (post-deploy tuning):
```bash
helm upgrade workload-variant-autoscaler ${WVA_REPO_PATH}/charts/workload-variant-autoscaler \
  -n <namespace> --reuse-values \
  --set hpa.behavior.scaleUp.stabilizationWindowSeconds=60 \
  --set hpa.behavior.scaleDown.stabilizationWindowSeconds=300
```

### EPP Threshold Alignment

WVA and EPP (Inference Scheduler) must use identical saturation thresholds, or EPP may route requests to replicas WVA considers saturated.

| WVA parameter | EPP parameter |
|---|---|
| `kvCacheThreshold` | `kvCacheUtilThreshold` |
| `queueLengthThreshold` | `queueDepthThreshold` |

After changing thresholds, restart EPP:
```bash
kubectl rollout restart deployment/<epp-deployment-name> -n <namespace>
```

### Undeploy

```bash
cd ${WVA_REPO_PATH}

make undeploy-wva-on-k8s WVA_NS=<namespace>
# or
make undeploy-wva-on-openshift WVA_NS=<namespace>
```

### Upgrade

```bash
cd ${WVA_REPO_PATH}
git pull origin main

# Apply CRDs first (force-conflicts in case of prior kubectl apply)
kubectl apply --server-side --force-conflicts -f charts/workload-variant-autoscaler/crds/

make deploy-wva-on-k8s \
  IMG=ghcr.io/llm-d/llm-d-workload-variant-autoscaler:v0.6.0 \
  WVA_NS=<namespace> \
  NAMESPACE_SCOPED=true \
  DEPLOY_LLM_D=false
```

> **Breaking change v0.5.1**: `scaleTargetRef` now required in VariantAutoscaling. Must include `apiVersion: apps/v1`.

### Skill Structure

```
skills/configure-wva-autoscaling-llm-d/
├── SKILL.md              # This file
├── Troubleshooting.md    # Quick troubleshooting reference
├── scripts/
│   ├── SCRIPTS.md
│   ├── deploy-wva.sh.template
│   ├── verify-wva.sh
│   ├── test-wva-scaling.sh
│   ├── troubleshoot-metrics.sh
│   └── troubleshoot-scaling.sh
└── evals/
```

### Repository Resources

**WVA** (`${WVA_REPO_PATH}`): `deploy/install.sh`, `charts/`, `docs/user-guide/`, `config/samples/`

**llm-d** (`${LLMD_REPO_PATH}`): `guides/workload-autoscaling/README.wva.md`

- WVA repo: https://github.com/llm-d/llm-d-workload-variant-autoscaler
- llm-d repo: https://github.com/llm-d/llm-d

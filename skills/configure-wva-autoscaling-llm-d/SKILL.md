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
Step 4a: Pre-flight checks (existing releases, KEDA availability)
Step 4b: Deploy WVA controller + first model VA + HPA via Makefile
Step 4c: Verify controller running and watching namespace
Step 4d: Add accelerator labels to all target deployments
Step 4e: Apply VA + HPA for additional models via kubectl apply
Step 4f: Verify ALL VAs show METRICSREADY: True and ALL HPAs have valid targets
Step 5:  Generate reusable deployment script
```

### 3d. References

| Resource | Link / Command |
|----------|---------------|
| WVA User Guide | `${WVA_REPO_PATH}/docs/user-guide/` |
| Helm Values | `${WVA_REPO_PATH}/charts/workload-variant-autoscaler/values.yaml` |
| Troubleshooting | [Troubleshooting.md](./Troubleshooting.md) |
| WVA GitHub | https://github.com/llm-d/llm-d-workload-variant-autoscaler |

**STOP. Ask:** "Does this plan look correct? Ready to deploy? (yes/no/adjust)"

---

## Step 4 — Deploy

Execute each sub-step one at a time, verifying after each.

---

### 4a. Pre-flight Checks

```bash
# Check for existing WVA release
helm list -n <namespace> | grep workload-variant-autoscaler

# Check scaler backend availability
kubectl get apiservice v1beta1.external.metrics.k8s.io 2>/dev/null | grep -o 'True\|False'
# If KEDA backend: verify KEDA is installed
kubectl get deployment keda-operator -A 2>/dev/null
```

If a stale release exists, inform the user and ask permission to remove:
```bash
helm uninstall workload-variant-autoscaler -n <namespace>
```

**STOP. Ask:** "Pre-flight checks complete. Ready to deploy WVA controller via Makefile? (yes/no)"

---

### 4b. Deploy Controller + First Model (Makefile)

Use the appropriate Makefile target. The first selected deployment gets the controller + VA + HPA.

**Kubernetes:**
```bash
cd <WVA_REPO_PATH>

make deploy-wva-on-k8s \
  IMG=ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest \
  NAMESPACE=<namespace> \
  LLMD_NS=<namespace> \
  NAMESPACE_SCOPED=true \
  DEPLOY_LLM_D=false \
  DEPLOY_LWS=false \
  DEPLOY_VA=true \
  DEPLOY_HPA=true \
  LLM_D_MODELSERVICE_NAME=<first-deployment-without-decode-suffix> \
  MODEL_ID="<model-id>" \
  ACCELERATOR_TYPE=<detected-accelerator> \
  KV_CACHE_THRESHOLD=<kv_cache_threshold> \
  QUEUE_LENGTH_THRESHOLD=<queue_length_threshold> \
  KV_SPARE_TRIGGER=<kv_spare_trigger> \
  QUEUE_SPARE_TRIGGER=<queue_spare_trigger> \
  HPA_MIN_REPLICAS=<min_replicas> \
  HPA_STABILIZATION_SECONDS=<scale_up_window> \
  SCALER_BACKEND=<prometheus-adapter|keda>
```

**OpenShift:**
```bash
cd <WVA_REPO_PATH>

E2E_TESTS_ENABLED=true INSTALL_GATEWAY_CTRLPLANE=false \
make deploy-wva-on-openshift \
  IMG=ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest \
  NAMESPACE=<namespace> \
  LLMD_NS=<namespace> \
  NAMESPACE_SCOPED=true \
  DEPLOY_LLM_D=false \
  DEPLOY_LWS=false \
  DEPLOY_VA=true \
  DEPLOY_HPA=true \
  LLM_D_MODELSERVICE_NAME=<first-deployment-without-decode-suffix> \
  MODEL_ID="<model-id>" \
  ACCELERATOR_TYPE=<detected-accelerator> \
  KV_CACHE_THRESHOLD=<kv_cache_threshold> \
  QUEUE_LENGTH_THRESHOLD=<queue_length_threshold> \
  KV_SPARE_TRIGGER=<kv_spare_trigger> \
  QUEUE_SPARE_TRIGGER=<queue_spare_trigger> \
  HPA_MIN_REPLICAS=<min_replicas> \
  HPA_STABILIZATION_SECONDS=<scale_up_window> \
  SCALER_BACKEND=prometheus-adapter \
  MONITORING_NAMESPACE=<openshift-monitoring|openshift-user-workload-monitoring> \
  SKIP_TLS_VERIFY=true
```

> **`LLM_D_MODELSERVICE_NAME`**: The Makefile appends `-decode` to this value. Set it **without** the `-decode` suffix. Example: deployment `my-model-decode` → `LLM_D_MODELSERVICE_NAME=my-model`.

> **OpenShift `E2E_TESTS_ENABLED=true INSTALL_GATEWAY_CTRLPLANE=false`**: Prevents the script from stopping at an interactive gateway prompt.

> **OpenShift exit code 2**: The Makefile chains scripts that may exit 2 even when WVA itself succeeded. Always verify with kubectl before assuming failure.

> **`MONITORING_NAMESPACE`**: Check with: `kubectl get apiservice v1beta1.external.metrics.k8s.io -o jsonpath='{.spec.service.namespace}'`

**What the Makefile creates:**
- Per-namespace: `workload-variant-autoscaler-controller-manager` (WVA controller)
- Per-model (first only): `workload-variant-autoscaler-va` (VariantAutoscaling) + `workload-variant-autoscaler-hpa` (HPA) or ScaledObject

---

### 4c. Verify Controller

```bash
kubectl get deployment workload-variant-autoscaler-controller-manager -n <namespace>
kubectl logs -n <namespace> -l control-plane=controller-manager --tail=20 | grep -i "watching"
```

Expected: controller Running, logs show `"Watching single namespace: <namespace>"`

**STOP. Report and ask:** "Controller deployed. Proceed to add accelerator labels? (yes/no)"

---

### 4d. Add Accelerator Labels

**First, auto-detect the accelerator for each deployment.** Run these checks in order and use the first value found:

```bash
# 1. Check if the deployment already has the label
kubectl get deployment <deployment-name> -n <namespace> \
  -o jsonpath='{.metadata.labels.inference\.optimization/acceleratorName}'

# 2. Check the pod template labels
kubectl get deployment <deployment-name> -n <namespace> \
  -o jsonpath='{.spec.template.metadata.labels.inference\.optimization/acceleratorName}'

# 3. Check node selector for GPU vendor hints
kubectl get deployment <deployment-name> -n <namespace> \
  -o jsonpath='{.spec.template.spec.nodeSelector}'

# 4. Check the nodes where the pods are running
kubectl get pod -n <namespace> -l llm-d.ai/role=decode \
  -o jsonpath='{.items[0].spec.nodeName}' | \
  xargs kubectl get node -o jsonpath='{.metadata.labels}' | grep -oE '"(nvidia|amd)\.com[^"]*"' | head -1
```

Valid values: `nvidia` (covers H100, A100, L4, A10, etc.), `amd`, `cpu`.

> **This is not always `nvidia`.** Always detect from the cluster — do not assume.

Report what was detected: "Detected accelerator: `<value>` for deployment `<name>`."
If detection is ambiguous, ask the user: "Could not auto-detect accelerator for `<deployment>`. Is it `nvidia`, `amd`, or `cpu`?"

Then apply the label for EACH selected deployment:
```bash
kubectl label deployment <deployment-name> -n <namespace> \
  inference.optimization/acceleratorName=<detected-accelerator> --overwrite
```

**Verify:**
```bash
kubectl get deployment -n <namespace> \
  -o custom-columns=NAME:.metadata.name,ACCELERATOR:.metadata.labels."inference\.optimization/acceleratorName"
```

Also patch the first model's VA if the Helm chart set the wrong label (known OpenShift issue — chart hardcodes `H100` regardless of `ACCELERATOR_TYPE`):
```bash
kubectl get variantautoscaling -n <namespace> -o jsonpath='{.items[0].metadata.labels.inference\.optimization/acceleratorName}'
# If the value is a GPU model name (e.g., "H100") instead of the vendor (e.g., "nvidia"), patch it:
kubectl patch variantautoscaling workload-variant-autoscaler-va -n <namespace> --type=merge \
  -p '{"metadata":{"labels":{"inference.optimization/acceleratorName":"<detected-accelerator>"}}}'
```

---

### 4e. Apply VA + HPA for Additional Models

For each additional deployment (beyond the first), apply a VariantAutoscaling + HPA pair. The controller from 4b picks them up automatically.

**For HPA backend (uses `SCALER_BACKEND=prometheus-adapter`):**
```bash
kubectl apply -n <namespace> -f - <<'EOF'
apiVersion: llmd.ai/v1alpha1
kind: VariantAutoscaling
metadata:
  name: <model-short-name>-va
  namespace: <namespace>
  labels:
    inference.optimization/acceleratorName: <detected-accelerator>
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: <full-deployment-name>
  modelID: "<model-id>"
  variantCost: "<variant_cost>"
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
      stabilizationWindowSeconds: <scale_up_window>
      policies:
      - type: Pods
        value: 10
        periodSeconds: 15
    scaleDown:
      stabilizationWindowSeconds: <scale_down_window>
      policies:
      - type: Pods
        value: 10
        periodSeconds: 15
EOF
```

**For KEDA backend (ScaledObject):**
```bash
kubectl apply -n <namespace> -f - <<'EOF'
apiVersion: llmd.ai/v1alpha1
kind: VariantAutoscaling
metadata:
  name: <model-short-name>-va
  namespace: <namespace>
  labels:
    inference.optimization/acceleratorName: <detected-accelerator>
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: <full-deployment-name>
  modelID: "<model-id>"
  variantCost: "<variant_cost>"
  minReplicas: <min>
  maxReplicas: <max>
---
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: <model-short-name>-scaler
  namespace: <namespace>
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: <full-deployment-name>
  pollingInterval: 5
  cooldownPeriod: 30
  maxReplicaCount: <max>
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleUp:
          stabilizationWindowSeconds: <scale_up_window>
          policies:
          - type: Pods
            value: 10
            periodSeconds: 15
        scaleDown:
          stabilizationWindowSeconds: <scale_down_window>
          policies:
          - type: Pods
            value: 10
            periodSeconds: 15
  triggers:
  - type: prometheus
    name: wva-desired-replicas
    metadata:
      serverAddress: <prometheus-url>
      query: |
        wva_desired_replicas{
          variant_name="<model-short-name>-va",
          exported_namespace="<namespace>"
        }
      threshold: '1'
      activationThreshold: '0'
      metricType: "AverageValue"
      unsafeSsl: "true"
EOF
```

> **Critical**: HPA metric name must be `wva_desired_replicas`. Do NOT use `wva_kv_cache_saturation` or `wva_queue_depth_saturation` — they are not exposed by Prometheus Adapter.

> **`variant_name` must match the VA resource name exactly.**

> **`type: AverageValue, averageValue: "1"`**: HPA computes `desiredReplicas = currentReplicas × (metric / 1)` — directly matching WVA's recommendation.

---

### 4f. Verify ALL Resources

**Wait ~2 minutes for Prometheus to scrape metrics, then verify:**

```bash
# All VAs
kubectl get variantautoscaling -n <namespace>

# All HPAs (or ScaledObjects)
kubectl get hpa -n <namespace>
# or: kubectl get scaledobject -n <namespace>
```

**Compatibility checks for EACH VA+HPA pair:**

```bash
# VA target matches HPA target?
kubectl get variantautoscaling -n <namespace> -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.scaleTargetRef.name}{"\n"}{end}'
kubectl get hpa -n <namespace> -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.scaleTargetRef.name}{"\n"}{end}'

# HPA metric selector variant_name matches VA name?
kubectl get hpa -n <namespace> -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.metrics[0].external.metric.selector.matchLabels.variant_name}{"\n"}{end}'
```

**Success criteria:**
- All VAs show `METRICSREADY: True`
- All HPAs show numeric targets (not `<unknown>`) — or ScaledObjects show `READY: True`
- Each HPA's `variant_name` selector matches its corresponding VA name
- Each VA and HPA target the same deployment
- No errors in WVA controller logs

**If METRICSREADY stays False:**
1. Check logs: `kubectl logs -n <namespace> -l control-plane=controller-manager --tail=50`
2. `"Skipping status update for VA without accelerator info"` → label missing
3. Deployment at 0 replicas → scale to 1: `kubectl scale deployment <name> -n <namespace> --replicas=1`

**If HPA shows `<unknown>`:**
1. Wrong metric name (must be `wva_desired_replicas`)
2. `variant_name` doesn't match VA name
3. Missing `exported_namespace` label
4. Prometheus Adapter not running

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
  Undeploy:  helm uninstall workload-variant-autoscaler -n <namespace>
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

### Makefile Variables Quick Reference

| Variable | Description | Default |
|----------|-------------|---------|
| `IMG` | WVA container image | `ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest` |
| `NAMESPACE` | Target namespace | `workload-variant-autoscaler-system` |
| `LLMD_NS` | Namespace where llm-d runs | same as NAMESPACE |
| `NAMESPACE_SCOPED` | Limit WVA to single namespace | `false` — **always set `true`** |
| `DEPLOY_LLM_D` | Deploy llm-d stack | `true` — set `false` when llm-d exists |
| `DEPLOY_LWS` | Deploy LeaderWorkerSet | `true` — set `false` if not needed |
| `DEPLOY_VA` | Deploy VariantAutoscaling | `false` — **set `true`** |
| `DEPLOY_HPA` | Deploy scaler (HPA or ScaledObject) | `false` — **set `true`** |
| `LLM_D_MODELSERVICE_NAME` | Deployment name **without** `-decode` suffix | — |
| `MODEL_ID` | Model identifier | `unsloth/Meta-Llama-3.1-8B` |
| `ACCELERATOR_TYPE` | GPU vendor label (`nvidia`, `amd`, `cpu`) | `H100` |
| `KV_CACHE_THRESHOLD` | KV cache saturation threshold | `0.80` |
| `QUEUE_LENGTH_THRESHOLD` | Queue depth saturation threshold | `5` |
| `KV_SPARE_TRIGGER` | Proactive spare KV trigger | `0.10` |
| `QUEUE_SPARE_TRIGGER` | Proactive spare queue trigger | `3` |
| `HPA_MIN_REPLICAS` | Minimum replicas | `1` |
| `HPA_STABILIZATION_SECONDS` | Scale-up AND scale-down window (symmetric) | `240` |
| `SCALER_BACKEND` | HPA → `prometheus-adapter`, KEDA → `keda` | `prometheus-adapter` (HPA) |
| `MONITORING_NAMESPACE` | Prometheus namespace (OpenShift) | `monitoring` |
| `SKIP_TLS_VERIFY` | Skip TLS for Prometheus | `false` |

### ConfigMap Live Tuning

Saturation thresholds live in ConfigMap `wva-saturation-scaling-config`. Changes take effect without controller restart:

```bash
kubectl edit configmap wva-saturation-scaling-config -n <namespace>
```

### Asymmetric Stabilization Windows (post-deploy)

The Makefile sets symmetric windows. For different up/down values:
```bash
helm upgrade workload-variant-autoscaler <WVA_REPO_PATH>/charts/workload-variant-autoscaler \
  -n <namespace> --reuse-values \
  --set hpa.behavior.scaleUp.stabilizationWindowSeconds=60 \
  --set hpa.behavior.scaleDown.stabilizationWindowSeconds=300
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
helm uninstall workload-variant-autoscaler -n <namespace>
```

### Known Issues

See [Troubleshooting.md](./Troubleshooting.md) for common problems including:
- METRICSREADY: False (accelerator label issues)
- HPA showing `<unknown>` (wrong metric or selector)
- Controller not detecting VA resources (namespace-scoping)
- CRD field manager conflicts
- OpenShift Helm chart label bug
- Scale-to-zero not working

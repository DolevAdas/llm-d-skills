# WVA Configuration Troubleshooting

Quick reference for common WVA issues. For detailed troubleshooting, see `${WVA_REPO_PATH}/docs/developer-guide/troubleshooting.md`.

---

## Known Issues and Gotchas

These are real issues encountered during deployment. Read before debugging.

### 1. HPA Shows `<unknown>` for Metrics

**Symptom**: `kubectl get hpa` shows `<unknown>/1` for all metrics.

**Causes and fixes:**

| Cause | Fix |
| ----- | --- |
| Wrong metric name in HPA (e.g., `wva_kv_cache_saturation`) | Use only `wva_desired_replicas` — it's the only metric Prometheus Adapter exposes for WVA |
| HPA selector missing `exported_namespace` label | Add `exported_namespace: <namespace>` to `matchLabels` |
| HPA selector `variant_name` doesn't match VA/HPA resource name | For annotated mode, `variant_name` must match the HPA name; for VA mode, it must match the VariantAutoscaling name |
| Prometheus Adapter not installed | Check `kubectl get apiservice v1beta1.external.metrics.k8s.io` |

### 2. `METRICSREADY: False` on VariantAutoscaling

**Symptom**: `kubectl get variantautoscaling` shows `METRICSREADY: False`. WVA logs: `"Skipping status update for VA without accelerator info"`.

**Fix**: Add the accelerator label to the VariantAutoscaling:

```bash
kubectl patch variantautoscaling <name> -n <namespace> --type=merge \
  -p '{"metadata":{"labels":{"inference.optimization/acceleratorName":"nvidia"}}}'
```

### 3. "No dispatch rate" Warning in WVA Logs

**Symptom**: `"Pod has vLLM metrics but no dispatch rate — possible pod/pod_name label mismatch"`.

**Impact**: Informational only. Saturation analysis still works using KV cache and queue depth. Scaling proceeds normally.

**Cause**: vLLM pod labels don't include the `pod_name` label that WVA uses to correlate dispatch metrics. Does not block scaling.

### 4. OpenShift: Wrong `acceleratorName` Label After Deploy

**Symptom**: `METRICSREADY` stays blank or `False` after deploy on OpenShift. WVA logs: `"Skipping status update for VA without accelerator info"`.

**Cause**: The `acceleratorName` label on the VariantAutoscaling resource was set to a GPU model name (e.g., `H100`) instead of the vendor (`nvidia`). WVA requires the vendor label.

**Fix**: Patch the VA to use the correct vendor label:

```bash
kubectl patch variantautoscaling <va-name> -n <namespace> --type=merge \
  -p '{"metadata":{"labels":{"inference.optimization/acceleratorName":"nvidia"}}}'
```

To avoid this when creating resources, always use `apply-hpa.sh --accelerator nvidia` which sets the vendor label correctly.

### 5. HPA Stuck at 0 When Target Deployment Has 0 Replicas at Deploy Time

**Symptom**: Both HPAs show `REPLICAS: 0` even though `minReplicas: 1` is set. WVA logs: `"No active VariantAutoscalings found, skipping optimization"`. `METRICSREADY` stays blank.

**Cause**: Chicken-and-egg — WVA only analyzes VAs whose target deployments have running pods. With 0 pods, WVA emits no `wva_desired_replicas` metric. With no metric, the HPA target shows `<unknown>`. With `<unknown>` metrics and a deployment already at 0 replicas, the HPA does not scale up to `minReplicas`.

**Fix**: Manually scale each target deployment to 1 replica to bootstrap the loop:

```bash
kubectl scale deployment <deployment-name> -n <namespace> --replicas=1
```

Once WVA sees running pods it will start emitting metrics, the HPA target will resolve from `<unknown>`, and from that point the HPA enforces `minReplicas` normally.

### 6. Load Test Not Triggering Scale-Up on Large Models

**Symptom**: WVA logs show `avgSpareKv: 0.7, shouldScaleUp: false` even after sending requests.

**Cause**: Large models (e.g., Qwen3-32B on 2×H100-80GB) have enormous KV caches. Short requests with small `max_tokens` complete and free KV slots before the next Prometheus scrape (every 30s).

**Fix**: Use streaming requests with `max_tokens=4000` and send 150–200 concurrent requests. Streaming keeps KV slots occupied during the entire generation, allowing Prometheus to observe the saturation.

### 7. Slow Scale-Up Response (Scale-From-Zero)

**Symptom**: After scaling from zero, the new pod takes too long before WVA considers it active. Requests queue up waiting for the fresh replica.

**Cause**: When scaling from zero, vLLM needs time to load the model. WVA waits for the pod to report metrics before routing to it. If `SCALE_FROM_ZERO_ENGINE_MAX_CONCURRENCY` is too low, WVA limits concurrent requests to the warming pod, increasing time to first useful response.

**Fix**: Increase the env var on the WVA controller deployment:

```bash
kubectl set env deployment/workload-variant-autoscaler-controller-manager \
  -n <WVA_NS> \
  SCALE_FROM_ZERO_ENGINE_MAX_CONCURRENCY=5
```

Default is `1`. Raise to match your model's warm-up parallelism.

### 8. InferencePool Datastore Empty

**Symptom**: WVA logs show `"InferencePool datastore is empty"`. No scaling decisions made.

**Cause**: WVA uses the llm-d InferencePool to discover decode pods. If the pool is empty (no pods registered), WVA has no workload to evaluate.

**Diagnosis**:

```bash
# Check InferencePool resources
kubectl get inferencepool -n <namespace>

# Check EPP logs for registration errors
kubectl logs -n <namespace> -l app.kubernetes.io/name=llm-d-epp -f | grep -i pool

# Check pod labels — decode pods must have llm-d.ai/role=decode
kubectl get pod -n <namespace> --show-labels | grep decode
```

**Fix**: Ensure decode pods have the `llm-d.ai/role=decode` label. If using manual deployment, add the label:

```bash
kubectl label deployment <decode-deployment> -n <namespace> llm-d.ai/role=decode --overwrite
```

---

## Quick Diagnostics

```bash
# Check VariantAutoscaling status (VA-path only)
kubectl get variantautoscaling -n <namespace>

# Check annotated HPAs
kubectl get hpa -n <namespace> -o yaml | grep -A 3 "llm-d.ai/managed"

# Check WVA controller logs
kubectl logs -n <WVA_NS> \
  -l app.kubernetes.io/name=workload-variant-autoscaler -f

# Check HPA status
kubectl get hpa -n <namespace>

# Verify metrics are available
kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/<namespace>/wva_desired_replicas" | jq
```

## Common Troubleshooting Patterns

**Use these patterns first** - they cover 90% of WVA issues and provide quick resolution steps.

### Pattern 1: "No active VariantAutoscalings found"

**Symptoms**: WVA controller logs show this message even though VariantAutoscaling resource exists.

**Quick Diagnosis Checklist**:

1. ✅ Check target deployments have at least 1 running replica — if at 0, see **Known Issue #5**
2. ✅ Check accelerator label on deployment (MOST COMMON)
3. ✅ Verify API version is `llmd.ai/v1alpha1`
4. ✅ Check variantCost is string (e.g., `"100"`)
5. ✅ Verify WVA is deployed into the same namespace as your workloads

**Resolution Steps**:

```bash
# Step 1: Add accelerator label (MOST COMMON FIX)
kubectl label deployment <name> -n <namespace> \
  inference.optimization/acceleratorName=nvidia --overwrite

# Step 2: Verify API version
kubectl get variantautoscaling -n <namespace> -o yaml | grep apiVersion
# Must be: llmd.ai/v1alpha1 (NOT inference.llmd.ai/v1alpha1)

# Step 3: Check variantCost type
kubectl get variantautoscaling -n <namespace> -o yaml | grep variantCost
# Must be string: "100" not 100

# Step 4: Verify WVA is watching the correct namespace
kubectl logs -n <WVA_NS> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "Watching"

# Step 5: Restart controller after fixes
kubectl rollout restart deployment -n <WVA_NS> \
  workload-variant-autoscaler-controller-manager

# Step 6: Verify WVA now detects the resource
kubectl logs -n <WVA_NS> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "VariantAutoscaling"
```

### Pattern 2: METRICSREADY: False

**Symptoms**: VariantAutoscaling shows `METRICSREADY: False` status.

**Quick Diagnosis Checklist**:

1. ✅ Check WVA logs for `"Skipping status update for VA without accelerator info"` → accelerator label missing on VA (see **Known Issues #2 and #4**)
2. ✅ Wait 2 minutes for Prometheus scrape interval
3. ✅ Check PodMonitor/ServiceMonitor exists and matches pod labels
4. ✅ Verify metrics endpoint is accessible, send test traffic if needed

```bash
# Check for accelerator label issue
kubectl logs -n <WVA_NS> -l control-plane=controller-manager | grep "accelerator"

# Wait for Prometheus scrape, then recheck
kubectl get variantautoscaling -n <namespace>

# Check metrics infrastructure
kubectl get podmonitor,servicemonitor -n <namespace>

# Test metrics endpoint directly
kubectl exec -n <namespace> <pod-name> -- curl -s localhost:8000/metrics | grep vllm
```

### Pattern 3: Not Scaling

**Symptoms**: Replicas don't change despite load, HPA shows metrics but no scaling action.

**Quick Diagnosis Checklist**:

1. ✅ Check current saturation levels
2. ✅ Verify HPA selector matches variant_name and exported_namespace
3. ✅ Check stabilization windows aren't too long
4. ✅ Review WVA controller logs for scaling decisions

**Resolution Steps**:

```bash
# Step 1: Check current saturation levels
kubectl get variantautoscaling -n <namespace> -o yaml | grep -A 5 status
# Look for kvCacheSaturation and queueDepthSaturation values

# Step 2: Verify HPA selector matches
kubectl get hpa -n <namespace> -o yaml | grep -A 5 metricSelector
# Must match: variant_name=<va-name> AND exported_namespace=<namespace>

# Step 3: Check WVA scaling decisions
kubectl logs -n <WVA_NS> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "desired replicas"

# Step 4: Check HPA status
kubectl describe hpa -n <namespace>
# Look for "unable to get metric" or other errors

# Step 5: If saturation never reached, lower thresholds
kubectl edit configmap -n <WVA_NS> \
  workload-variant-autoscaler-saturation-scaling-config
# Try: kvCacheThreshold: "0.70" (from 0.80)

# Step 6: If stabilization too long, adjust HPA
kubectl patch hpa <hpa-name> -n <namespace> --type=merge -p '{
  "spec": {
    "behavior": {
      "scaleUp": {"stabilizationWindowSeconds": 60},
      "scaleDown": {"stabilizationWindowSeconds": 180}
    }
  }
}'
```

## Detailed Common Issues

### 0. WVA Controller Not Detecting VariantAutoscaling Resource

**Symptoms**:

- WVA controller logs show "No active VariantAutoscalings found"
- VariantAutoscaling resource exists but WVA doesn't see it
- Controller watching wrong namespace

**Common Causes**:

- **Missing accelerator label on deployment** (MOST COMMON)
- WVA deployed to a different namespace than workloads
- Wrong API version in VariantAutoscaling YAML
- Namespace-scoping not set correctly during deploy

**Solutions**:

```bash
# 1. CRITICAL: Add required accelerator label to deployment
kubectl label deployment <deployment-name> -n <namespace> \
  inference.optimization/acceleratorName=nvidia --overwrite

# 2. Verify the label was added
kubectl get deployment <deployment-name> -n <namespace> --show-labels | grep acceleratorName

# 3. Check VariantAutoscaling API version (must be llmd.ai/v1alpha1)
kubectl get variantautoscaling -n <namespace> -o yaml | grep apiVersion
# Should show: apiVersion: llmd.ai/v1alpha1
# NOT: apiVersion: inference.llmd.ai/v1alpha1

# 4. Verify variantCost is a string, not integer
kubectl get variantautoscaling -n <namespace> -o yaml | grep variantCost
# Should show: variantCost: "100"
# NOT: variantCost: 100

# 5. Check which namespace WVA is watching
kubectl logs -n <WVA_NS> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "Watching"

# 6. Restart WVA controller after fixes
kubectl rollout restart deployment -n <WVA_NS> \
  workload-variant-autoscaler-controller-manager

# 7. Verify WVA now detects the resource
kubectl logs -n <WVA_NS> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "VariantAutoscaling"
```

**Configuration Rules**:

- **ALWAYS add `inference.optimization/acceleratorName` label before creating VariantAutoscaling**
- Use correct API version: `llmd.ai/v1alpha1`
- Use string for `variantCost`: `"100"` not `100`
- Deploy WVA into the same namespace as your llm-d workloads

### 1. METRICSREADY: False

> **If WVA logs say `"Skipping status update for VA without accelerator info"`**: see **Known Issues #2** (missing accelerator label on VA) and **Known Issue #4** (wrong label value on OpenShift).

For all other causes (Prometheus not scraping, no traffic, PodMonitor mismatch), see **Pattern 2** above and **Issue #2** below.

### 2. Missing Metrics Service and ServiceMonitor

**Symptoms**:

- Prometheus not scraping vLLM metrics
- No metrics available for WVA
- METRICSREADY stays False even after waiting

**Common Causes**:

- No Service exposing vLLM metrics port (8000)
- No ServiceMonitor configured for Prometheus
- ServiceMonitor selector not matching pod labels

**Solutions**:

```bash
# 1. Check if Service exists for metrics
kubectl get service -n <namespace> | grep metrics

# 2. Create Service for vLLM metrics endpoint
kubectl apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: <deployment-name>-metrics
  namespace: <namespace>
  labels:
    app: <deployment-name>
spec:
  selector:
    app: <deployment-name>
  ports:
  - name: metrics
    port: 8000
    targetPort: 8000
    protocol: TCP
EOF

# 3. Create ServiceMonitor for Prometheus
kubectl apply -f - <<EOF
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: <deployment-name>-metrics
  namespace: <namespace>
spec:
  selector:
    matchLabels:
      app: <deployment-name>
  endpoints:
  - port: metrics
    path: /metrics
    interval: 30s
EOF

# 4. Verify ServiceMonitor is created
kubectl get servicemonitor -n <namespace>

# 5. Check if Prometheus is scraping
kubectl exec -n <namespace> <pod-name> -- curl -s localhost:8000/metrics | grep vllm
```

### 3. WVA Not Scaling

**Symptoms**: Replicas don't change despite load

**Common Causes**:

- Saturation thresholds never reached (too high)
- HPA stabilization window too long
- Mismatched variant_name label in HPA
- WVA controller not running

**Solutions**:

```bash
# Check current saturation levels
kubectl get variantautoscaling -n <namespace> -o yaml | grep -A 5 status

# Check WVA scaling decisions
kubectl logs -n <WVA_NS> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "desired replicas"

# Verify HPA is reading metrics
kubectl describe hpa -n <namespace>

# Check if variant_name matches
kubectl get hpa -n <namespace> -o yaml | grep variant_name
kubectl get variantautoscaling -n <namespace> -o yaml | grep "name:"
```

**Configuration Fixes**:

- Lower saturation thresholds (e.g., kvCacheThreshold: 0.70)
- Reduce HPA stabilization windows
- Ensure HPA variant_name label matches the VariantAutoscaling name (VA mode) or HPA name (annotated mode)

### 4. Frequent Scaling (Flapping)

**Symptoms**: Replicas constantly scaling up and down

**Common Causes**:

- Thresholds too sensitive
- HPA stabilization window too short
- Misaligned WVA and EPP thresholds
- Insufficient spare capacity triggers

**Solutions**:

```bash
# Check scaling events
kubectl get events -n <namespace> --sort-by='.lastTimestamp' | grep -i scale

# Monitor saturation over time
watch -n 5 'kubectl get variantautoscaling -n <namespace>'
```

**Configuration Fixes**:

- Increase HPA stabilization windows (300s+ for scale-down)
- Increase saturation thresholds (e.g., kvCacheThreshold: 0.85)
- Align WVA and EPP thresholds
- Adjust spare capacity triggers (lower kvSpareTrigger)

### 5. Wrong Deployment Target

**Symptoms**: VariantAutoscaling exists but doesn't affect deployment

**Common Causes**:

- scaleTargetRef points to non-existent deployment
- Deployment name changed but VariantAutoscaling not updated
- Wrong namespace

**Solutions**:

```bash
# Verify deployment exists
kubectl get deployment -n <namespace> <deployment-name>

# Check VariantAutoscaling target
kubectl get variantautoscaling -n <namespace> -o yaml | grep -A 3 scaleTargetRef

# Update if needed
kubectl edit variantautoscaling -n <namespace> <name>
```

### 6. Prometheus Connection Issues

**Symptoms**: WVA controller logs show Prometheus errors

**Common Causes**:

- HTTPS required but Prometheus only has HTTP
- CA certificate issues
- Prometheus not accessible from WVA namespace
- Wrong Prometheus URL

**Solutions**:

```bash
# Check WVA Prometheus configuration
kubectl get configmap -n <WVA_NS> \
  workload-variant-autoscaler-saturation-scaling-config -o yaml | grep PROMETHEUS

# Test Prometheus connectivity from WVA pod
kubectl exec -n <WVA_NS> \
  <wva-controller-pod> -- curl -k <prometheus-url>/api/v1/query?query=up

# For OpenShift, use Thanos Querier
# Update WVA config to use: https://thanos-querier.openshift-monitoring.svc.cluster.local:9091
```

### 7. Scale-to-Zero Not Working

**Symptoms**: Replicas don't scale to zero despite idle period

**Common Causes**:

- `prometheus-adapter` backend in use — HPA fundamentally cannot scale to 0; KEDA is required for scale-to-zero
- HPAScaleToZero feature gate not enabled (vanilla Kubernetes with KEDA)
- HPA minReplicas not set to 0
- Scale-to-zero not enabled in WVA config
- Retention period not elapsed

**Solutions**:

```bash
# Check HPA minReplicas
kubectl get hpa -n <namespace> -o yaml | grep minReplicas

# Check scale-to-zero config
kubectl get configmap -n <WVA_NS> \
  wva-model-scale-to-zero-config -o yaml

# Check WVA controller logs for scale-to-zero decisions
kubectl logs -n <WVA_NS> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "scale.*zero"
```

**Configuration Fixes**:

- Enable HPAScaleToZero feature gate in cluster
- Set HPA minReplicas: 0
- Enable scale-to-zero in WVA ConfigMap
- Adjust retention period if needed

### 8. Multi-Variant Cost Optimization Not Working

**Symptoms**: WVA scales expensive variant instead of cheap one

**Common Causes**:

- variantCost not set or set incorrectly
- All variants have same cost
- Cheap variant at maxReplicas
- Model IDs don't match

**Solutions**:

```bash
# Check variant costs
kubectl get variantautoscaling -n <namespace> -o yaml | grep -A 2 variantCost

# Verify model IDs match
kubectl get variantautoscaling -n <namespace> -o yaml | grep modelID

# Check current replica counts
kubectl get variantautoscaling -n <namespace>
```

**Configuration Fixes**:

- Set different variantCost values (e.g., H100: "80.0", A100: "40.0")
- Ensure model IDs are identical across variants
- Increase maxReplicas on cheaper variant

### 9. Wrong API Version or variantCost Type

**Symptoms**:

- VariantAutoscaling resource fails to create
- Validation errors about API version
- WVA controller doesn't recognize the resource

**Common Causes**:

- Using wrong API group: `inference.llmd.ai/v1alpha1` instead of `llmd.ai/v1alpha1`
- Using integer for `variantCost` instead of string

**Solutions**:

```bash
# Check current API version
kubectl get variantautoscaling -n <namespace> -o yaml | grep apiVersion

# If wrong, delete and recreate with correct version
kubectl delete variantautoscaling <name> -n <namespace>

# Create with correct API version and variantCost type
kubectl apply -f - <<EOF
apiVersion: llmd.ai/v1alpha1  # CORRECT
kind: VariantAutoscaling
metadata:
  name: <name>
  namespace: <namespace>
spec:
  scaleTargetRef:
    kind: Deployment
    name: <deployment-name>
  modelID: <model-id>
  variantCost: "100"  # MUST be string, not integer
  minReplicas: 2
  maxReplicas: 10
EOF
```

**Configuration Rules**:

- API version: `llmd.ai/v1alpha1` (NOT `inference.llmd.ai/v1alpha1`)
- `variantCost`: Must be string (e.g., `"100"` not `100`)
- **NEVER include `spec.metrics` in VariantAutoscaling** - metrics belong in HPA only

### 10. WVA Deployed to Wrong Namespace

**Symptoms**:

- WVA controller logs show "No active VariantAutoscalings found"
- VariantAutoscaling exists in target namespace but WVA doesn't detect it
- Controller watching a different namespace than your workloads

**Root Cause**:
With `NAMESPACE_SCOPED=true`, WVA uses `--watch-namespace=$(POD_NAMESPACE)` where `POD_NAMESPACE` is the pod's own namespace (resolved via Kubernetes Downward API). This means WVA only watches the namespace it is deployed into. If WVA is in a different namespace than your llm-d workloads, it will not see them.

**CORRECT Solution**: Deploy WVA into the same namespace as your llm-d workloads by setting `WVA_NS` to your llm-d namespace:

```bash
# Set WVA_NS to your llm-d workload namespace
export WVA_NS=<your-llm-d-namespace>
export NAMESPACE=$WVA_NS   # required — Makefile passes NAMESPACE to scripts

# Deploy WVA into the target namespace
make -C ${WVA_REPO_PATH} deploy-wva-on-k8s \
  WVA_NS=$WVA_NS \
  NAMESPACE=$WVA_NS \
  NAMESPACE_SCOPED=true \
  PROMETHEUS_URL=<prometheus-url> \
  PROMETHEUS_INSECURE_SKIP_VERIFY=true
```

**Verify namespace scoping**:

```bash
kubectl logs -n <WVA_NS> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep -i "watching"
# Structured output: {"msg":"Watching single namespace","namespace":"<WVA_NS>"}
```

**To migrate from a wrong namespace**:

```bash
# Undeploy from wrong namespace
WVA_NS=<wrong-namespace> NAMESPACE=<wrong-namespace> \
  ${WVA_REPO_PATH}/deploy/install.sh --undeploy

# Redeploy into correct namespace
make -C ${WVA_REPO_PATH} deploy-wva-on-k8s \
  WVA_NS=<correct-namespace> \
  NAMESPACE=<correct-namespace> \
  NAMESPACE_SCOPED=true \
  PROMETHEUS_URL=<prometheus-url>
```

### 11. Invalid spec.metrics in VariantAutoscaling

**Symptoms**:

- Error: "unknown field spec.metrics" when applying VariantAutoscaling
- VariantAutoscaling resource fails validation

**Root Cause**:
Metrics configuration belongs in HPA, not VariantAutoscaling.

**Solution**:

```yaml
# WRONG - metrics in VariantAutoscaling:
apiVersion: llmd.ai/v1alpha1
kind: VariantAutoscaling
spec:
  ...
  metrics:  # ❌ WRONG - this field doesn't exist
    - type: External

# CORRECT - VariantAutoscaling has no metrics field:
apiVersion: llmd.ai/v1alpha1
kind: VariantAutoscaling
spec:
  scaleTargetRef:
    kind: Deployment
    name: <deployment-name>
  modelID: "Qwen/Qwen3-32B"
  variantCost: "100"
  minReplicas: 2
  maxReplicas: 10
  # No metrics field here — goes in the HPA

---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
spec:
  metrics:  # ✅ CORRECT - metrics go in HPA
    - type: External
      external:
        metric:
          name: wva_desired_replicas
```

### 12. ConfigMap Threshold Changes Not Visible

**Symptom**: Edited `workload-variant-autoscaler-saturation-scaling-config` ConfigMap but saturation behavior hasn't changed.

**Note**: The ConfigMap is mounted and watched live — no controller restart is needed. Changes take effect within the next reconciliation cycle (typically within 30 seconds).

If changes appear not to apply:

```bash
# Confirm the change was saved
kubectl get configmap workload-variant-autoscaler-saturation-scaling-config \
  -n <WVA_NS> -o yaml | grep -A 20 "data:"

# Watch WVA logs for the updated threshold values being read
kubectl logs -n <WVA_NS> \
  -l app.kubernetes.io/name=workload-variant-autoscaler -f | grep -i threshold

# If still no effect after 60s, restart the controller once
kubectl rollout restart deployment workload-variant-autoscaler-controller-manager \
  -n <WVA_NS>
```

### 13. Annotation-Based HPA Not Discovered by WVA

**Symptom**: HPA has `llm-d.ai/managed: "true"` annotation but WVA still shows no scaling activity. HPA metric shows `<unknown>`.

**Cause**: WVA synthesizes in-memory VariantAutoscaling objects from annotated HPAs. The `variant_name` label in the HPA metric selector must match the HPA's own name (this is how WVA correlates the two).

**Check**:

```bash
# Check HPA annotations and metric selector
kubectl get hpa <hpa-name> -n <namespace> -o yaml | grep -E "llm-d.ai|variant_name"
```

The `variant_name` in the metric selector must equal the HPA name (not the deployment name):

```yaml
metadata:
  name: my-deployment-hpa          # HPA name
  annotations:
    llm-d.ai/managed: "true"
    llm-d.ai/model-id: "Qwen/Qwen3-32B"
    llm-d.ai/variant-cost: "100"
spec:
  metrics:
  - type: External
    external:
      metric:
        name: wva_desired_replicas
        selector:
          matchLabels:
            variant_name: my-deployment-hpa  # must match metadata.name above
            exported_namespace: <namespace>
```

Use `apply-hpa.sh --mode annotated` to generate correctly correlated resources.

---

## Threshold Tuning Guide

### Understanding Saturation Metrics

**KV Cache Utilization**: Percentage of KV cache memory used (0.0-1.0)

- 0.80 = 80% of KV cache filled
- Higher values = more memory pressure

**Queue Length**: Number of requests waiting in queue

- Higher values = more backlog

### Tuning Strategy

1. **Start with defaults** and monitor for 24 hours
2. **Observe saturation patterns** in Prometheus/Grafana
3. **Adjust based on behavior**:
   - Frequent saturation → Lower thresholds
   - Never saturated → Raise thresholds
   - Flapping → Increase stabilization windows

### Threshold Recommendations by Use Case

| Use Case | kvCacheThreshold | queueLengthThreshold | kvSpareTrigger | Stabilization |
| -------- | ---------------- | -------------------- | -------------- | ------------- |
| Low Latency | 0.70 | 3 | 0.15 | 60s up, 300s down |
| Balanced | 0.80 | 5 | 0.10 | 120s up, 300s down |
| Cost Optimized | 0.85 | 8 | 0.05 | 180s up, 600s down |
| Development | 0.75 | 5 | 0.10 | 60s up, 120s down |

### Applying Threshold Changes

Edit the live ConfigMap — no restart needed:

```bash
kubectl edit configmap workload-variant-autoscaler-saturation-scaling-config \
  -n <WVA_NS>
```

---

## Alignment with Inference Scheduler (EPP)

**Critical**: WVA and EPP must use the same thresholds.

### Check EPP Configuration

```bash
# Get GAIE deployment values
kubectl get deployment -n <namespace> <gaie-deployment> -o yaml | grep -A 10 env

# Look for EPP threshold environment variables
# - KV_CACHE_THRESHOLD
# - QUEUE_LENGTH_THRESHOLD
```

### Update Both Together

When changing thresholds:

1. Update WVA saturation ConfigMap
2. Update EPP environment variables in GAIE deployment
3. Restart EPP (WVA picks up ConfigMap changes automatically)

```bash
# Update WVA ConfigMap (takes effect without restart)
kubectl edit configmap workload-variant-autoscaler-saturation-scaling-config \
  -n <WVA_NS>

# Update GAIE deployment
kubectl set env deployment/<gaie-deployment> -n <namespace> \
  KV_CACHE_THRESHOLD=0.80 \
  QUEUE_LENGTH_THRESHOLD=5
```

---

## Getting Help

For issues not covered here:

1. **Check official docs**:
   - WVA Troubleshooting: `${WVA_REPO_PATH}/docs/developer-guide/troubleshooting.md`
   - WVA Debugging: `${WVA_REPO_PATH}/docs/developer-guide/debugging.md`
   - llm-d WVA Guide: `${LLMD_REPO_PATH}/guides/workload-autoscaling/README.wva.md`
2. **Review WVA logs**: Look for ERROR or WARN messages
3. **Check Prometheus metrics**: Verify vLLM metrics are being scraped
4. **Test with llm-d-benchmark**: Use benchmark templates to validate behavior
   - Templates: `deployments/*/benchmark-templates/` (guide.yaml, guidellm.yaml, sanity.yaml, shared_prefix.yaml)
5. **Community support**: Join llm-d Slack or GitHub discussions

## Useful Commands Reference

```bash
# Watch VariantAutoscaling status
watch -n 5 'kubectl get variantautoscaling -n <namespace>'

# Stream WVA controller logs
kubectl logs -n <WVA_NS> \
  -l app.kubernetes.io/name=workload-variant-autoscaler -f

# Check all WVA resources
kubectl get variantautoscaling,hpa,podmonitor -n <namespace>

# View recent scaling events
kubectl get events -n <namespace> --sort-by='.lastTimestamp' | grep -i scale | tail -20

# Check Prometheus metrics directly
kubectl port-forward -n <namespace> <pod-name> 8000:8000 &
curl -s localhost:8000/metrics | grep vllm_kv_cache_usage_perc

# Verify external metrics API
kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1" | jq

# Check Prometheus Adapter
kubectl logs -n <monitoring-namespace> -l app.kubernetes.io/name=prometheus-adapter
```

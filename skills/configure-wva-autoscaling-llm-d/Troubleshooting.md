# WVA Configuration Troubleshooting

Quick reference for common WVA issues. For detailed troubleshooting, see `${WVA_REPO_PATH}/docs/user-guide/troubleshooting.md`.

---

## Known Issues and Gotchas

These are real issues encountered during deployment. Read before debugging.

### 1. CRD Field Manager Conflict

**Symptom**: Helm fails with:

```text
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
| ----- | --- |
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

### 6. OpenShift: Helm Chart Sets `acceleratorName: H100` Despite `ACCELERATOR_TYPE=nvidia`

**Symptom**: `METRICSREADY` stays blank or `False` after deploy on OpenShift. WVA logs: `"Skipping status update for VA without accelerator info"`.

**Cause**: The Helm chart used by `deploy-wva-on-openshift` hardcodes the GPU model (`H100`) as the accelerator label rather than the vendor (`nvidia`), overriding the `ACCELERATOR_TYPE` variable. The VariantAutoscaling created for the first model will have `acceleratorName: H100` instead of `nvidia`.

**Fix**: After Step 4a, always patch the first model's VA:

```bash
kubectl patch variantautoscaling workload-variant-autoscaler-va -n <namespace> --type=merge \
  -p '{"metadata":{"labels":{"inference.optimization/acceleratorName":"nvidia"}}}'
```

Additional models created via `kubectl apply` in Step 4b are not affected — set the label correctly in the manifest.

### 7. HPA Stuck at 0 When Target Deployment Has 0 Replicas at Deploy Time

**Symptom**: Both HPAs show `REPLICAS: 0` even though `minReplicas: 1` is set. WVA logs: `"No active VariantAutoscalings found, skipping optimization"`. `METRICSREADY` stays blank.

**Cause**: Chicken-and-egg — WVA only analyzes VAs whose target deployments have running pods. With 0 pods, WVA emits no `wva_desired_replicas` metric. With no metric, the HPA target shows `<unknown>`. With `<unknown>` metrics and a deployment already at 0 replicas, the HPA does not scale up to `minReplicas`.

**Fix**: Manually scale each target deployment to 1 replica to bootstrap the loop:

```bash
kubectl scale deployment <deployment-name> -n <namespace> --replicas=1
```

Once WVA sees running pods it will start emitting metrics, the HPA target will resolve from `<unknown>`, and from that point the HPA enforces `minReplicas` normally.

### 8. Load Test Not Triggering Scale-Up on Large Models

**Symptom**: WVA logs show `avgSpareKv: 0.7, shouldScaleUp: false` even after sending requests.

**Cause**: Large models (e.g., Qwen3-32B on 2×H100-80GB) have enormous KV caches. Short requests with small `max_tokens` complete and free KV slots before the next Prometheus scrape (every 30s).

**Fix**: Use streaming requests with `max_tokens=4000` and send 150–200 concurrent requests. Streaming keeps KV slots occupied during the entire generation, allowing Prometheus to observe the saturation.

---

## Quick Diagnostics

```bash
# Check VariantAutoscaling status
kubectl get variantautoscaling -n <namespace>

# Check WVA controller logs (WVA is deployed into the target namespace)
kubectl logs -n <namespace> \
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

1. ✅ Check target deployments have at least 1 running replica — if at 0, see **Known Issue #7**
2. ✅ Check accelerator label on deployment (MOST COMMON)
3. ✅ Verify API version is `llmd.ai/v1alpha1`
4. ✅ Check variantCost is string (e.g., `"100"`)
5. ✅ Verify namespace-scoping configuration

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

# Step 4: Verify namespace-scoping
kubectl logs -n workload-variant-autoscaler-system \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "Watching"

# Step 5: Restart controller after fixes
kubectl rollout restart deployment -n workload-variant-autoscaler-system \
  workload-variant-autoscaler-controller-manager

# Step 6: Verify WVA now detects the resource
kubectl logs -n workload-variant-autoscaler-system \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "VariantAutoscaling"
```

### Pattern 2: METRICSREADY: False

**Symptoms**: VariantAutoscaling shows `METRICSREADY: False` status.

**Quick Diagnosis Checklist**:

1. ✅ Check WVA logs for `"Skipping status update for VA without accelerator info"` → accelerator label missing on VA (see **Known Issues #4 and #6**)
2. ✅ Wait 2 minutes for Prometheus scrape interval
3. ✅ Check PodMonitor/ServiceMonitor exists and matches pod labels
4. ✅ Verify metrics endpoint is accessible, send test traffic if needed

```bash
# Check for accelerator label issue
kubectl logs -n <namespace> -l control-plane=controller-manager | grep "accelerator"

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
kubectl logs -n <namespace> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "desired replicas"

# Step 4: Check HPA status
kubectl describe hpa -n <namespace>
# Look for "unable to get metric" or other errors

# Step 5: If saturation never reached, lower thresholds
kubectl edit configmap -n <namespace> \
  wva-saturation-scaling-config
# Try: kvCacheThreshold: "0.70" (from 0.80)

# Step 6: If stabilization too long, adjust HPA
kubectl edit hpa -n <namespace>
# Reduce scaleUp/scaleDown stabilizationWindowSeconds
```

## Detailed Common Issues

### 0. WVA Controller Not Detecting VariantAutoscaling Resource

**Symptoms**:
- WVA controller logs show "No active VariantAutoscalings found"
- VariantAutoscaling resource exists but WVA doesn't see it
- Controller watching wrong namespace

**Common Causes**:
- **WVA deployed to wrong namespace** (MOST COMMON - see Issue #11)
- **Missing accelerator label on deployment**
- Wrong API version in VariantAutoscaling YAML
- Namespace-scoping configuration not working
- Config file overriding environment variables

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

# 5. Check which namespace WVA is watching (WVA is deployed in the target namespace)
kubectl logs -n <namespace> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "Watching"

# 6. Restart WVA controller after fixes
kubectl rollout restart deployment -n <namespace> \
  workload-variant-autoscaler-controller-manager

# 7. Verify WVA now detects the resource
kubectl logs -n <namespace> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "VariantAutoscaling"
```

**Configuration Fixes**:
- **ALWAYS add `inference.optimization/acceleratorName` label before creating VariantAutoscaling**
- Use correct API version: `llmd.ai/v1alpha1`
- Use string for `variantCost`: `"100"` not `100`
- Set namespace-scoping via Helm values or ConfigMap, NOT environment variables
- The config file overrides `WATCH_NAMESPACE` environment variable

### 1. METRICSREADY: False

> **If WVA logs say `"Skipping status update for VA without accelerator info"`**: see **Known Issues #4** (missing accelerator label on VA) and **Known Issue #6** (OpenShift Helm chart sets wrong label).

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
kubectl logs -n workload-variant-autoscaler-system \
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
- Ensure HPA variant_name label matches VariantAutoscaling name

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
kubectl get configmap -n workload-variant-autoscaler-system \
  wva-variantautoscaling-config -o yaml | grep PROMETHEUS

# Test Prometheus connectivity from WVA pod
kubectl exec -n workload-variant-autoscaler-system \
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
kubectl get configmap -n workload-variant-autoscaler-system \
  wva-model-scale-to-zero-config -o yaml

# Check WVA controller logs for scale-to-zero decisions
kubectl logs -n workload-variant-autoscaler-system \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "scale.*zero"
```

**Configuration Fixes**:
- Enable HPAScaleToZero feature gate in cluster
- Set HPA minReplicas: 0
- Enable scale-to-zero in WVA Helm values or ConfigMap
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

### 9. Namespace-Scoping Not Working (DEPRECATED - See Issue #11)

**⚠️ IMPORTANT**: This approach is deprecated. See Issue #11 for the correct namespace-scoped deployment method.

**Symptoms**:
- WVA controller watching wrong namespace
- Controller logs show "Watching single namespace: workload-variant-autoscaler-system"
- Setting WATCH_NAMESPACE environment variable has no effect

**Root Cause**:
- The WVA config file overrides the `WATCH_NAMESPACE` environment variable
- Setting env var directly doesn't work
- **This entire approach is problematic - use namespace-scoped deployment instead**

**Old Solutions (NOT RECOMMENDED)**:

```bash
# WRONG WAY (doesn't work):
kubectl set env deployment/workload-variant-autoscaler-controller-manager \
  -n workload-variant-autoscaler-system \
  WATCH_NAMESPACE=<your-namespace>

# OLD WAY 1: Use Helm values (problematic)
helm upgrade workload-variant-autoscaler ./charts/workload-variant-autoscaler \
  --namespace workload-variant-autoscaler-system \
  --set controller.watchNamespace=<your-namespace> \
  --reuse-values

# OLD WAY 2: Edit ConfigMap directly (problematic)
kubectl edit configmap -n workload-variant-autoscaler-system \
  wva-variantautoscaling-config
# Add or modify: WATCH_NAMESPACE: "<your-namespace>"

# After changing ConfigMap, restart controller
kubectl rollout restart deployment -n workload-variant-autoscaler-system \
  workload-variant-autoscaler-controller-manager
```

**⚠️ CORRECT APPROACH**: Deploy WVA directly into your target namespace with `namespaceScoped: true`. See Issue #11 below.

### 10. Wrong API Version or variantCost Type

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

### 11. WVA Deployed to Wrong Namespace (CRITICAL)

**Symptoms**:
- WVA controller logs show "No active VariantAutoscalings found"
- VariantAutoscaling exists in target namespace but WVA doesn't detect it
- Controller watching `workload-variant-autoscaler-system` instead of target namespace
- Attempts to configure `watchNamespace` via Helm values or ConfigMap don't work reliably

**Root Cause**:
- **WVA must be deployed INTO the target namespace to watch it**
- When `namespaceScoped: true`, WVA watches its own deployment namespace
- Deploying to `workload-variant-autoscaler-system` and trying to watch another namespace is unreliable
- ConfigMap changes require controller restart and may not take effect properly

**CORRECT Solution**:

```bash
# 1. Deploy WVA directly into your target namespace
helm install workload-variant-autoscaler ./charts/workload-variant-autoscaler \
  --namespace <your-target-namespace> \
  --create-namespace \
  --set controller.namespaceScoped=true \
  --set prometheus.url=<prometheus-url> \
  --set prometheus.insecureSkipVerify=true

# 2. Verify WVA is watching the correct namespace
kubectl logs -n <your-target-namespace> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "Watching"
# Should show: "Watching single namespace: <your-target-namespace>"

# 3. Verify WVA detects your VariantAutoscaling resources
kubectl logs -n <your-target-namespace> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "VariantAutoscaling"
```

**Why This Works**:
- `namespaceScoped: true` makes WVA watch its own deployment namespace
- No need for ConfigMap changes or `watchNamespace` parameter
- Controller starts with correct configuration immediately
- More reliable and predictable behavior

**Migration from Old Setup**:

```bash
# 1. Uninstall WVA from workload-variant-autoscaler-system
helm uninstall workload-variant-autoscaler \
  --namespace workload-variant-autoscaler-system

# 2. Install WVA into target namespace
helm install workload-variant-autoscaler ./charts/workload-variant-autoscaler \
  --namespace <your-target-namespace> \
  --set controller.namespaceScoped=true \
  --set prometheus.url=<prometheus-url> \
  --set prometheus.insecureSkipVerify=true

# 3. Verify it's working
kubectl get variantautoscaling -n <your-target-namespace>
kubectl logs -n <your-target-namespace> \
  -l app.kubernetes.io/name=workload-variant-autoscaler
```

**Key Takeaways**:
- ✅ Deploy WVA INTO the namespace you want to monitor
- ✅ Use `namespaceScoped: true` for single-namespace deployments
- ❌ Don't deploy to `workload-variant-autoscaler-system` and try to watch other namespaces
- ❌ Don't rely on `watchNamespace` parameter or ConfigMap changes

### 12. Invalid spec.metrics in VariantAutoscaling

**Symptoms**:
- Error: "unknown field spec.metrics" when applying VariantAutoscaling
- VariantAutoscaling resource fails validation
- WVA controller doesn't recognize the resource

**Root Cause**:
- Metrics configuration belongs in HPA, not VariantAutoscaling
- Common mistake when copying examples or migrating configurations

**Solution**:

```bash
# WRONG - metrics in VariantAutoscaling:
apiVersion: llmd.ai/v1alpha1
kind: VariantAutoscaling
metadata:
  name: my-variant
spec:
  scaleTargetRef:
    kind: Deployment
    name: my-deployment
  modelID: "Qwen/Qwen3-32B"
  variantCost: "100"
  minReplicas: 2
  maxReplicas: 10
  metrics:  # ❌ WRONG - this field doesn't exist
    - type: External
      external:
        metric:
          name: wva_kv_cache_saturation

# CORRECT - metrics in HPA only:
apiVersion: llmd.ai/v1alpha1
kind: VariantAutoscaling
metadata:
  name: my-variant
spec:
  scaleTargetRef:
    kind: Deployment
    name: my-deployment
  modelID: "Qwen/Qwen3-32B"
  variantCost: "100"
  minReplicas: 2
  maxReplicas: 10
  # No metrics field here

---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: my-variant-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: my-deployment
  minReplicas: 2
  maxReplicas: 10
  metrics:  # ✅ CORRECT - metrics go in HPA
    - type: External
      external:
        metric:
          name: wva_kv_cache_saturation
```

**Key Takeaway**: VariantAutoscaling defines the autoscaling policy, HPA defines the metrics and scaling behavior.

### 13. ConfigMap Changes Not Taking Effect

**Symptoms**:
- Modified WVA ConfigMap but behavior doesn't change
- Controller still using old configuration
- Saturation thresholds not updating

**Root Cause**:
- WVA controller reads ConfigMap at startup only
- Changes require controller restart to take effect
- **Better approach**: Set configuration during initial Helm install

**Solutions**:

```bash
# Option 1: Restart controller after ConfigMap change
kubectl rollout restart deployment -n <namespace> \
  workload-variant-autoscaler-controller-manager

# Option 2 (BETTER): Set configuration during Helm install
helm install workload-variant-autoscaler ./charts/workload-variant-autoscaler \
  --namespace <namespace> \
  --set controller.namespaceScoped=true \
  --set saturationScaling.kvCacheThreshold=0.80 \
  --set saturationScaling.queueLengthThreshold=5 \
  --set prometheus.url=<prometheus-url>

# Option 3: Update via Helm upgrade
helm upgrade workload-variant-autoscaler ./charts/workload-variant-autoscaler \
  --namespace <namespace> \
  --set saturationScaling.kvCacheThreshold=0.75 \
  --reuse-values
```

**Best Practice**:
- Set all configuration during initial Helm install
- Avoid manual ConfigMap edits
- Use Helm upgrade for configuration changes
- ConfigMap changes always require controller restart

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
|----------|------------------|----------------------|----------------|---------------|
| Low Latency | 0.70 | 3 | 0.15 | 60s up, 300s down |
| Balanced | 0.80 | 5 | 0.10 | 120s up, 300s down |
| Cost Optimized | 0.85 | 8 | 0.05 | 180s up, 600s down |
| Development | 0.75 | 5 | 0.10 | 60s up, 120s down |

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
3. Restart both controllers

```bash
# Update WVA ConfigMap
kubectl edit configmap -n workload-variant-autoscaler-system \
  wva-saturation-scaling-config

# Update GAIE deployment
kubectl set env deployment/<gaie-deployment> -n <namespace> \
  KV_CACHE_THRESHOLD=0.80 \
  QUEUE_LENGTH_THRESHOLD=5

# Restart WVA controller
kubectl rollout restart deployment -n workload-variant-autoscaler-system \
  workload-variant-autoscaler-controller-manager
```

## Getting Help

For issues not covered here:

1. **Check official docs**:
   - WVA Troubleshooting: `${WVA_REPO_PATH}/docs/user-guide/troubleshooting.md`
   - WVA Configuration: `${WVA_REPO_PATH}/docs/user-guide/configuration.md`
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
kubectl logs -n workload-variant-autoscaler-system \
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
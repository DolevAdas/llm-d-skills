# WVA Configuration Troubleshooting

Quick reference for common WVA issues. For detailed troubleshooting, see `${WVA_REPO_PATH}/docs/user-guide/troubleshooting.md`.


## Quick Diagnostics

```bash
# Check VariantAutoscaling status
kubectl get variantautoscaling -n <namespace>

# Check WVA controller logs
kubectl logs -n workload-variant-autoscaler-system \
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
1. ✅ Check accelerator label on deployment (MOST COMMON)
2. ✅ Verify API version is `llmd.ai/v1alpha1`
3. ✅ Check variantCost is string (e.g., `"100"`)
4. ✅ Verify namespace-scoping configuration

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
1. ✅ Wait 2 minutes for Prometheus scrape interval
2. ✅ Check Service and ServiceMonitor exist
3. ✅ Verify metrics endpoint is accessible
4. ✅ Send test traffic to generate metrics

**Resolution Steps**:
```bash
# Step 1: Wait for Prometheus scrape (most common - just need patience)
sleep 120 && kubectl get variantautoscaling -n <namespace>

# Step 2: Check metrics infrastructure exists
kubectl get service,servicemonitor -n <namespace> | grep metrics

# Step 3: Test metrics endpoint directly
kubectl exec -n <namespace> <pod-name> -- curl -s localhost:8000/metrics | grep vllm

# Step 4: If no metrics, create Service and ServiceMonitor
# See Issue #2 below for full YAML examples

# Step 5: Send test traffic to generate metrics
curl -X POST http://<gateway-url>/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "<model-id>", "messages": [{"role": "user", "content": "test"}]}'

# Step 6: Check again after 2 minutes
sleep 120 && kubectl get variantautoscaling -n <namespace>
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
kubectl logs -n workload-variant-autoscaler-system \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "desired replicas"

# Step 4: Check HPA status
kubectl describe hpa -n <namespace>
# Look for "unable to get metric" or other errors

# Step 5: If saturation never reached, lower thresholds
kubectl edit configmap -n workload-variant-autoscaler-system \
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
- **Missing accelerator label on deployment** (MOST COMMON)
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

# 5. Check which namespace WVA is watching
kubectl logs -n workload-variant-autoscaler-system \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "Watching"

# 6. If namespace-scoping not working, check ConfigMap (NOT environment variable)
kubectl get configmap -n workload-variant-autoscaler-system \
  wva-variantautoscaling-config -o yaml

# 7. Fix namespace-scoping via Helm values (CORRECT WAY)
helm upgrade workload-variant-autoscaler ./charts/workload-variant-autoscaler \
  --namespace workload-variant-autoscaler-system \
  --set controller.watchNamespace=<your-namespace> \
  --reuse-values

# 8. Restart WVA controller after fixes
kubectl rollout restart deployment -n workload-variant-autoscaler-system \
  workload-variant-autoscaler-controller-manager

# 9. Verify WVA now detects the resource
kubectl logs -n workload-variant-autoscaler-system \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "VariantAutoscaling"
```

**Configuration Fixes**:
- **ALWAYS add `inference.optimization/acceleratorName` label before creating VariantAutoscaling**
- Use correct API version: `llmd.ai/v1alpha1`
- Use string for `variantCost`: `"100"` not `100`
- Set namespace-scoping via Helm values or ConfigMap, NOT environment variables
- The config file overrides `WATCH_NAMESPACE` environment variable

### 1. METRICSREADY: False

**Symptoms**: VariantAutoscaling shows `METRICSREADY: False`

**Common Causes**:
- Prometheus hasn't scraped metrics yet (wait 1-2 minutes after deployment)
- No traffic to model (metrics are zero)
- PodMonitor not configured or not matching pod labels
- Prometheus connection issues

**Solutions**:

```bash
# Wait for Prometheus scrape interval
sleep 120 && kubectl get variantautoscaling -n <namespace>

# Send test traffic to generate metrics
kubectl port-forward -n <namespace> svc/<gateway-service> 8080:80 &
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "<model-id>", "messages": [{"role": "user", "content": "test"}]}'

# Check if pods expose metrics
kubectl exec -n <namespace> <pod-name> -- curl -s localhost:8000/metrics | grep vllm

# Verify PodMonitor exists and matches labels
kubectl get podmonitor -n <namespace>
kubectl get pods -n <namespace> --show-labels
```

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
- HPAScaleToZero feature gate not enabled
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

### 9. Namespace-Scoping Not Working

**Symptoms**:
- WVA controller watching wrong namespace
- Controller logs show "Watching single namespace: workload-variant-autoscaler-system"
- Setting WATCH_NAMESPACE environment variable has no effect

**Root Cause**:
- The WVA config file overrides the `WATCH_NAMESPACE` environment variable
- Setting env var directly doesn't work

**Solutions**:

```bash
# WRONG WAY (doesn't work):
kubectl set env deployment/workload-variant-autoscaler-controller-manager \
  -n workload-variant-autoscaler-system \
  WATCH_NAMESPACE=<your-namespace>

# CORRECT WAY 1: Use Helm values
helm upgrade workload-variant-autoscaler ./charts/workload-variant-autoscaler \
  --namespace workload-variant-autoscaler-system \
  --set controller.watchNamespace=<your-namespace> \
  --reuse-values

# CORRECT WAY 2: Edit ConfigMap directly
kubectl edit configmap -n workload-variant-autoscaler-system \
  wva-variantautoscaling-config
# Add or modify: WATCH_NAMESPACE: "<your-namespace>"

# After changing ConfigMap, restart controller
kubectl rollout restart deployment -n workload-variant-autoscaler-system \
  workload-variant-autoscaler-controller-manager

# Verify namespace-scoping is working
kubectl logs -n workload-variant-autoscaler-system \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "Watching"
# Should show: "Watching single namespace: <your-namespace>"
```

**Key Takeaway**: Always use Helm values or ConfigMap to set namespace-scoping, never environment variables.

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
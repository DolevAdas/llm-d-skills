# WVA Configuration Scripts Guide

This directory contains scripts and configuration examples for setting up Workload Variant Autoscaler (WVA) with llm-d deployments.

## Overview

The scripts in this directory provide:
- **Automated deployment** - Complete WVA setup automation
- **Load testing** - Validate autoscaling behavior
- **Runtime verification** - Check WVA status and metrics
- **Troubleshooting** - Diagnose common issues
- **Configuration templates** - Example YAML configurations

## Script Index

### Deployment Scripts

#### [`generate-deploy-script.sh`](./generate-deploy-script.sh) ⭐ **Start Here**
**Purpose**: Generates a customized deployment script from template based on user requirements

**Usage**:
```bash
# Interactive mode (prompts for all values)
./generate-deploy-script.sh

# Command-line mode (all values provided)
./generate-deploy-script.sh \
  --namespace <namespace> \
  --deployment <deployment-name> \
  --wva-repo <path> \
  --model-id <model-id> \
  --variant-cost <cost> \
  --min-replicas <n> \
  --max-replicas <n> \
  --kv-threshold <threshold> \
  --queue-threshold <threshold> \
  --scale-up-window <seconds> \
  --scale-down-window <seconds> \
  --output <output-file>
```

**Example**:
```bash
./generate-deploy-script.sh \
  --namespace example-namespace \
  --deployment my-llm-deployment \
  --wva-repo /path/to/wva-repo \
  --model-id "Qwen/Qwen3-32B" \
  --variant-cost "100" \
  --min-replicas 2 \
  --max-replicas 10 \
  --output deploy-wva-example.sh
```

**What it does**:
1. Collects user requirements (interactively or via command-line)
2. Reads [`deploy-wva.sh.template`](./deploy-wva.sh.template)
3. Replaces template variables with user values
4. Generates executable deployment script
5. Makes script executable and ready to run

**Template Variables**:
- `{{NAMESPACE}}`, `{{DEPLOYMENT_NAME}}`, `{{WVA_REPO_PATH}}`
- `{{MODEL_ID}}`, `{{VARIANT_COST}}`
- `{{MIN_REPLICAS}}`, `{{MAX_REPLICAS}}`
- `{{KV_CACHE_THRESHOLD}}`, `{{QUEUE_LENGTH_THRESHOLD}}`
- `{{SCALE_UP_STABILIZATION}}`, `{{SCALE_DOWN_STABILIZATION}}`
- `{{PROMETHEUS_URL}}`, `{{PROMETHEUS_INSECURE_SKIP_VERIFY}}`

#### [`deploy-wva.sh.template`](./deploy-wva.sh.template)
**Purpose**: Template for generating customized deployment scripts

**Note**: This is a template file with `{{VARIABLE}}` placeholders. Use `generate-deploy-script.sh` to create an executable script from this template.

**What the generated script does**:
1. Deploys WVA controller into target namespace with `namespaceScoped: true`
2. Adds required labels (`inference.optimization/acceleratorName`)
3. Creates metrics Service and ServiceMonitor
4. Creates VariantAutoscaling and HPA resources with embedded configuration
5. Verifies deployment and waits for metrics to be ready

**Benefits**:
- ✅ No separate YAML files needed - all configuration embedded
- ✅ Single executable script per deployment
- ✅ Easy to version control with deployment
- ✅ Can be regenerated with different parameters

### Testing Scripts

#### [`test-wva-scaling.sh`](./test-wva-scaling.sh)
**Purpose**: Tests WVA autoscaling by sending load and monitoring response

**Usage**:
```bash
./test-wva-scaling.sh <namespace> <deployment-name> [model-id] [num-requests]
```

**Example**:
```bash
./test-wva-scaling.sh example-namespace my-llm-deployment "Qwen/Qwen3-32B" 100
```

**What it does**:
1. Records baseline state (current replicas)
2. Sends concurrent requests with long outputs to increase KV cache usage
3. Monitors WVA logs for scaling decisions
4. Checks vLLM metrics (KV cache, queue depth)
5. Waits for scaling to occur (respects stabilization window)
6. Reports results and provides troubleshooting suggestions

**Parameters**:
- `namespace`: Target namespace
- `deployment-name`: Name of deployment to test
- `model-id`: (Optional) Auto-detects if not provided
- `num-requests`: (Optional) Default: 100

### Verification Scripts

#### [`verify-wva.sh`](./verify-wva.sh)
**Purpose**: Comprehensive WVA status check

**Usage**:
```bash
./verify-wva.sh <namespace>
```

**Checks**:
- VariantAutoscaling resources and status
- HPA configuration and metrics
- WVA controller logs
- Prometheus metrics availability

### Troubleshooting Scripts

#### [`troubleshoot-metrics.sh`](./troubleshoot-metrics.sh)
**Purpose**: Diagnose metrics collection issues

**Usage**:
```bash
./troubleshoot-metrics.sh <namespace> <pod-name>
```

**Checks**:
- vLLM metrics endpoint accessibility
- Prometheus scraping configuration
- ServiceMonitor setup
- Metric values and formats

#### [`troubleshoot-scaling.sh`](./troubleshoot-scaling.sh)
**Purpose**: Diagnose scaling behavior issues

**Usage**:
```bash
./troubleshoot-scaling.sh <namespace>
```

**Checks**:
- Current saturation levels
- WVA scaling decisions
- HPA status and events
- Stabilization windows
- Threshold configuration

## Quick Start

### 1. Deploy WVA

Use the automated deployment script:

```bash
# Prepare configuration files first
cd deployments/your-deployment/

# Run deployment script
../../skills/configure-wva-autoscaling-llm-d/scripts/deploy-wva.sh \
  <namespace> <deployment-name> /path/to/wva-repo
```

### 2. Verify Setup

Check that everything is working:

```bash
cd skills/configure-wva-autoscaling-llm-d/scripts
./verify-wva.sh <namespace>
```

### 3. Test Autoscaling (Optional)

Validate WVA responds to load:

```bash
./test-wva-scaling.sh <namespace> <deployment-name>
```

### 3. Troubleshoot Issues

If autoscaling isn't working:

```bash
# Check metrics availability
./troubleshoot-metrics.sh <namespace> <pod-name>

# Check scaling behavior
./troubleshoot-scaling.sh <namespace>
```

## Available Scripts

**`verify-wva.sh`** - Comprehensive verification
```bash
./verify-wva.sh <namespace>                    # Use default WVA namespace
./verify-wva.sh <namespace> <wva-namespace>    # Specify WVA controller namespace
```

**`troubleshoot-metrics.sh`** - Metrics diagnostics
```bash
./troubleshoot-metrics.sh <namespace> <pod-name>
```

**`troubleshoot-scaling.sh`** - Scaling diagnostics
```bash
./troubleshoot-scaling.sh <namespace>                    # Use default WVA namespace
./troubleshoot-scaling.sh <namespace> <wva-namespace>    # Specify WVA controller namespace
```

All scripts include input validation and improved error handling.

## Configuration Examples

The `configs/` directory contains example configurations:

### Basic Templates
- **`variantautoscaling-basic.yaml`** - Minimal VariantAutoscaling configuration
- **`hpa-basic.yaml`** - Basic HPA configuration

### Complete Examples
- **`example.yaml`** - Low-latency aggressive scaling (available)
- Other examples referenced in SKILL.md (create as needed)

## Critical Configuration Requirements

### 1. VariantAutoscaling Must Have Accelerator Label

```yaml
apiVersion: llmd.ai/v1alpha1
kind: VariantAutoscaling
metadata:
  name: my-autoscaler
  namespace: my-namespace
  labels:
    # REQUIRED: Without this label, WVA will not process the resource
    inference.optimization/acceleratorName: nvidia
spec:
  scaleTargetRef:
    kind: Deployment
    name: my-deployment
  modelID: "vendor/model-name"
  variantCost: "10.0"
```

**Why it's required:**
- WVA controller filters resources by accelerator type
- Without this label, the VariantAutoscaling will show `METRICSREADY: False`
- Controller logs will show: "Skipping status update for VA without accelerator info"

### 2. HPA Must Match Both Labels

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: my-hpa
  namespace: my-namespace
spec:
  metrics:
  - type: External
    external:
      metric:
        name: wva_desired_replicas
        selector:
          matchLabels:
            # REQUIRED: Must match VariantAutoscaling name
            variant_name: my-autoscaler
            # REQUIRED: Must match namespace
            exported_namespace: my-namespace
      target:
        type: AverageValue
        averageValue: "1"
```

**Why both labels are required:**
- WVA exports metrics with both `variant_name` and `exported_namespace` labels
- HPA needs both labels to uniquely identify the correct metric
- Without both labels, HPA will show `<unknown>` for metrics

## Common Issues and Solutions

### Issue 1: VariantAutoscaling shows METRICSREADY: False

**Symptom:**
```bash
$ oc get variantautoscaling
NAME              METRICSREADY   REPLICAS   DESIRED
my-autoscaler     False          1          0
```

**Solution:**
Add the accelerator label to VariantAutoscaling metadata:
```yaml
metadata:
  labels:
    inference.optimization/acceleratorName: nvidia
```

### Issue 2: HPA shows `<unknown>` for metrics

**Symptom:**
```bash
$ oc get hpa
NAME        REFERENCE              TARGETS         MINPODS   MAXPODS
my-hpa      Deployment/my-deploy   <unknown>/1     1         10
```

**Solution:**
Update HPA metric selector to include both labels:
```yaml
selector:
  matchLabels:
    variant_name: my-autoscaler
    exported_namespace: my-namespace
```

### Issue 3: Multiple WVA controllers reporting same metric

**Symptom:**
Multiple metric values for the same variant in Prometheus adapter.

**Solution:**
This is normal behavior. HPA automatically averages the values from multiple controllers. Ensure your HPA selector includes both `variant_name` and `exported_namespace` labels to filter correctly.

### Issue 4: Deployment not scaling

**Symptom:**
HPA shows correct metrics but deployment doesn't scale.

**Troubleshooting steps:**
1. Check HPA: `kubectl describe hpa <hpa-name> -n <namespace>`
2. Verify deployment has ≥1 replica
3. Check WVA controller logs
4. Verify saturation thresholds fit workload

## WVA Repository Scripts

Use `${WVA_REPO_PATH}/deploy/install.sh` for deployment. See [SKILL.md](../SKILL.md) for Makefile targets.


## Saturation Configuration

Adjust saturation thresholds in the WVA controller namespace:

```bash
# View current configuration
oc get configmap wva-saturation-config -n <wva-controller-namespace> -o yaml

# Update for aggressive scaling (low latency)
oc apply -f configs/configmap-aggressive-saturation.yaml
```

**Default thresholds:**
- `kvCacheThreshold: 0.80` (80% KV cache full)
- `queueLengthThreshold: 5` (5 requests queued)
- `kvSpareTrigger: 0.10` (10% spare capacity)
- `queueSpareTrigger: 3` (3 requests spare capacity)

**Aggressive thresholds:**
- `kvCacheThreshold: 0.70` (70% KV cache full)
- `queueLengthThreshold: 3` (3 requests queued)
- `kvSpareTrigger: 0.15` (15% spare capacity)
- `queueSpareTrigger: 5` (5 requests spare capacity)

## Monitoring

### Watch HPA scaling decisions
```bash
oc get hpa <hpa-name> -n <namespace> -w
```

### View WVA controller logs
```bash
# Find WVA controller namespace
WVA_NS=$(oc get deployment --all-namespaces -l app.kubernetes.io/name=workload-variant-autoscaler -o jsonpath='{.items[0].metadata.namespace}')

# Tail logs
oc logs -f -n $WVA_NS -l app.kubernetes.io/name=workload-variant-autoscaler
```

### Check VariantAutoscaling status
```bash
oc get variantautoscaling <name> -n <namespace> -o yaml
```

### Verify metrics are available
```bash
oc get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/<namespace>/wva_desired_replicas" | jq
```

## Best Practices

1. **Start with default thresholds** - Only adjust after observing behavior
2. **Use appropriate stabilization windows** - Prevent flapping
3. **Set realistic maxReplicas** - Consider cluster capacity
4. **Monitor for 24-48 hours** - Ensure stable behavior under various loads
5. **Use cost-based optimization** - For multi-variant deployments
6. **Test scale-down behavior** - Ensure graceful handling of reduced load

## Additional Resources

- [Main Skill Documentation](../SKILL.md)
- [Troubleshooting Guide](../Troubleshooting.md)
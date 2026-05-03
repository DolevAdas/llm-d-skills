---
name: configure-wva-autoscaling-llm-d
description: Configure and optimize Workload Variant Autoscaler (WVA) for llm-d inference deployments. Use when users want to set up autoscaling based on KV cache saturation, configure multi-variant cost optimization, tune saturation thresholds, enable scale-to-zero, or troubleshoot WVA behavior. Helps translate user requirements like "I want aggressive scaling" or "optimize for cost across H100 and A100 variants" into proper WVA configuration.
---

## 📋 Command Execution Notice

**Before executing any command, I will:**
1. **Explain what the command does** - Clear description of purpose and expected outcome
2. **Show the actual command** - The exact command to be executed
3. **Explain why it's needed** - How it fits into the workflow

> ## 🔔 ALWAYS NOTIFY BEFORE CREATING RESOURCES
>
> **RULE**: Before creating ANY resource (namespaces, files, Kubernetes objects), notify the user first.
>
> **Format**: "I am about to create `<resource-type>` named `<name>` because `<reason>`. Proceeding now."
>
> **Never silently create resources.** Check existence first, then notify before acting.

## Critical Rules

1. **Do NOT modify existing repository code** - Cloning a missing repository is allowed and required for this skill, but never edit code you did not create. If existing code must be adjusted, copy it to a new location, modify the copied file there, and reference the new file instead of changing the original.

2. **ALWAYS use existing skill scripts first** - Use scripts in [`scripts/`](./scripts/SCRIPTS.md). Only perform manual edits if scripts fail due to non-standard deployment structure.

3. **Verify cluster resources** - Check available GPU/RDMA resources before applying changes.

## Prerequisites: Repository Setup

**Required Repositories**: llm-d, llm-d-workload-variant-autoscaler

**Setup Process**:
1. Check for repositories in common locations
2. Ask user for paths if not found
3. Clone missing repositories with user approval
4. Set environment variables: `LLMD_REPO_PATH`, `WVA_REPO_PATH`

**Note**: llm-d-benchmark is optional for testing/validation. Benchmark templates are in deployment directories.

## Overview
This skill helps you configure Workload Variant Autoscaler (WVA) for llm-d inference deployments based on your specific requirements. WVA provides intelligent autoscaling using KV cache saturation and queue depth metrics (number of requests waiting to be processed), with support for multi-variant cost optimization.

## When to Use This Skill

Use this skill when you need to:
- Configure WVA autoscaling for existing llm-d deployments
- Tune saturation thresholds based on workload characteristics
- Set up multi-variant deployments with cost optimization
- Enable or configure scale-to-zero behavior
- Troubleshoot WVA scaling decisions
- Align WVA thresholds with Inference Scheduler (EPP) settings

## What is WVA?

**Workload Variant Autoscaler (WVA)** is a Kubernetes controller that provides intelligent autoscaling for LLM inference workloads. Unlike traditional CPU/GPU-based autoscaling, WVA uses:

- **KV Cache Saturation**: Proactive scaling based on memory pressure in the inference server
- **Queue Depth**: Request backlog monitoring to prevent latency spikes
- **Cost Optimization**: Preferentially scales cheaper variants when multiple hardware options are available
- **Spare Capacity Model**: Scales before saturation occurs, not after

**Key Concept - Variants**: A variant is a way of serving a model with a specific combination of hardware, runtime, and serving approach. For example:
- Same model on H100 vs A100 vs L4 GPUs (different cost/performance)
- Same model with different parallelism strategies
- Same model with different LoRA adapters


### Skill Structure

```
skills/configure-wva-autoscaling-llm-d/
├── SKILL.md              # This file - main skill with configuration guidance
├── Troubleshooting.md    # Quick troubleshooting reference
├── scripts/              # Configuration templates and utility scripts
│   ├── SCRIPTS.md       # Detailed scripts usage guide
│   ├── configs/         # YAML configuration templates (examples)
│   ├── verify-wva.sh    # Runtime verification script
│   ├── troubleshoot-metrics.sh  # Metrics troubleshooting
│   └── troubleshoot-scaling.sh  # Scaling troubleshooting
└── evals/               # Skill evaluation tests
```

**Note**: This skill primarily references scripts from `${WVA_REPO_PATH}` for deployment and installation. The skill scripts focus on runtime verification and troubleshooting.
## Core Workflow

When a user asks for WVA configuration help, follow this workflow:

### 1. Choose Namespace Isolation Strategy

**FIRST**, determine the WVA deployment scope based on the user's environment and requirements:

#### Option 1: Namespace-Scoped Controller (Recommended for Multi-Tenant/Testing)
**Use when**: Testing, development, or multi-tenant clusters where teams need isolation.

**CRITICAL**: To watch a specific namespace, deploy WVA **INTO that namespace** with `namespaceScoped: true`. When `namespaceScoped: true`, WVA watches its own deployment namespace.

**Configuration**:
```bash
# Deploy WVA directly into your target namespace
# It will automatically watch only that namespace
helm upgrade -i workload-variant-autoscaler ./charts/workload-variant-autoscaler \
  --namespace <your-target-namespace> \
  --create-namespace \
  --set controller.namespaceScoped=true
```

**Behavior**:
- ✅ WVA deployed in your namespace watches only that namespace
- ✅ Complete isolation from other namespaces
- ✅ Perfect for multi-tenant clusters where each team has their own controller
- ✅ No interference with other teams' deployments
- ✅ Simple and predictable behavior

**Example**: To watch `example-namespace` namespace, deploy WVA into `example-namespace` with `namespaceScoped: true`

#### Option 2: Cluster-Wide with Namespace Exclusions
**Use when**: You want cluster-wide monitoring but need to exclude specific namespaces.

**Configuration**:
```bash
# Exclude specific namespaces from WVA monitoring
kubectl annotate namespace other-team-namespace wva.llmd.ai/exclude=true
kubectl annotate namespace kube-system wva.llmd.ai/exclude=true
```

**Behavior**:
- ✅ WVA watches all namespaces by default
- ✅ Explicitly excluded namespaces are ignored
- ✅ Good for shared clusters with some protected namespaces

#### Option 3: Multi-Controller Isolation (Advanced)
**Use when**: Complete isolation between teams/projects is required.

**Configuration**:
```bash
# Your team's controller (only manages your namespace)
helm upgrade -i wva-my-team ./charts/workload-variant-autoscaler \
  --namespace wva-system \
  --set wva.controllerInstance=my-team \
  --set controller.watchNamespace=my-namespace

# Other team's controller (manages their namespace)
helm upgrade -i wva-other-team ./charts/workload-variant-autoscaler \
  --namespace wva-system \
  --set wva.controllerInstance=other-team \
  --set controller.watchNamespace=other-namespace
```

**Behavior**:
- ✅ Complete isolation between teams
- ✅ Each controller has its own metrics with `controller_instance` label
- ✅ No interference between different teams' autoscaling
- ✅ Separate monitoring and troubleshooting per team

**Choose one of the above options based on your requirements.**

### 2. Configuration Strategy

**Configuration Detection and User Input:**

1. **Auto-detect from deployment:**
   - Deployment/StatefulSet/LWS name, kind, namespace
   - Model ID (from labels/env vars if available)
   - Current replicas, existing HPA
   - Accelerator type

2. **ALWAYS ask user for:**
   1. **Scaling backend**: HPA or KEDA
     - **CRITICAL**: Always ask which backend the user prefers
     - If HPA is preferable for their llm-d deployment, explain why:
   2. **Scaling behavior**: Fast/balanced/cost-optimized
   3. **Stabilization windows**: Scale-up (default 120s, range 0-300s), Scale-down (default 300s, range 120-600s)
   4. **Replica limits**: minReplicas, maxReplicas
   5. **Saturation Thresholds** (ConfigMap: `wva-saturation-scaling-config`):
      - `kvCacheThreshold` (0.80): Saturation trigger when KV cache exceeds 80%
      - `queueLengthThreshold` (5): Saturation trigger when queue exceeds 5 requests
      - `kvSpareTrigger` (0.10): Proactive scaling when spare capacity drops below 10%
      - `queueSpareTrigger` (3): Proactive scaling when spare queue capacity drops below 3
      - **Namespace overrides**: Create ConfigMap in target namespace for local thresholds

   6. **Multi-variant**: Variant cost, other variants for same model

3. **Must ask if not detected:**
   - Model ID, variant cost, scale-to-zero requirement

**Key Components:**

**VariantAutoscaling** ([template](scripts/configs/variantautoscaling-basic.yaml)):
- `scaleTargetRef`: Target deployment/statefulset/LWS
- `modelID`: Model identifier (e.g., `meta-llama/Llama-3.1-8B`)
- `variantCost`: Relative cost (H100=100, A100=70, L4=30) for multi-variant optimization


**Controller Config** (ConfigMap: `wva-variantautoscaling-config`):
- `PROMETHEUS_BASE_URL`: Prometheus endpoint (must be accessible from WVA)
- `GLOBAL_OPT_INTERVAL` (60s): WVA reconciliation frequency (not Prometheus scrape interval)


### 3. Common Configuration Patterns

**Choose configuration based on user needs. If unclear, ask:**
- Scaling priority: fast response, cost optimization, or balanced?
- Hardware: single GPU type or multi-variant (H100/A100/L4)?
- Scale-to-zero needed? (dev/test only)

| Pattern | Use When | Template | Key Settings |
|---------|----------|----------|--------------|
| **Single Variant** | One GPU type, balanced scaling | [example1](scripts/configs/example1-single-variant.yaml) | `kvCacheThreshold: 0.80`, `queueLengthThreshold: 5` |
| **Multi-Variant** | Multiple GPUs, minimize cost | [example2](scripts/configs/example2-multi-variant.yaml) | Set `variantCost` per GPU (H100=100, A100=70, L4=30) |
| **Aggressive** | Fast response to load spikes | [example3](scripts/configs/example3-aggressive-scaling.yaml) | Lower thresholds (0.70), faster scale-up (60s) |
| **Conservative** | Stable production, avoid over-scaling | Custom | Higher thresholds (0.85), longer stabilization (300s+) |
| **Scale-to-Zero** | Dev/test cost savings | [example4](scripts/configs/example4-scale-to-zero.yaml) | `minReplicas: 0`, requires alpha feature gate |

**Configuration Rules:**
- **CRITICAL**: Include `inference.optimization/acceleratorName` label on deployments (e.g., `nvidia`, `amd`, `cpu`)
  - Without this label, WVA controller will NOT detect the VariantAutoscaling resource
  - Add to deployment: `kubectl label deployment <name> -n <namespace> inference.optimization/acceleratorName=nvidia`
- **CRITICAL**: HPA selector must match BOTH labels: `variant_name` + `exported_namespace`
  - `variant_name` must match the VariantAutoscaling resource name
  - `exported_namespace` must match the deployment namespace
- **CRITICAL**: `variantCost` must be a STRING, not an integer (e.g., `"100"` not `100`)
- **CRITICAL**: API version must be `llmd.ai/v1alpha1` (NOT `inference.llmd.ai/v1alpha1`)
- **CRITICAL**: VariantAutoscaling spec should NOT include `metrics` field - metrics are defined in HPA only
  - The `spec.metrics` field is invalid and will cause errors
  - Only include: `scaleTargetRef`, `modelID`, `variantCost`, `minReplicas`, `maxReplicas`
- Multi-variant: WVA scales cheaper variants first based on `variantCost`
- Align thresholds with Inference Scheduler (EPP) - see section 4

See [`scripts/SCRIPTS.md`](./scripts/SCRIPTS.md) for detailed examples.

### 4. Threshold Alignment with Inference Scheduler (EPP)

**Critical**: WVA and EPP must use identical thresholds. One EPP instance per model (shared across variants of same model).

**Threshold mapping:**
- WVA `kvCacheThreshold` = EPP `kvCacheUtilThreshold`
- WVA `queueLengthThreshold` = EPP `queueDepthThreshold`

**To align:**
1. Update WVA ConfigMap `wva-saturation-scaling-config` (auto-reloads)
2. Update EPP config
3. Restart EPP: `kubectl rollout restart deployment/gaie-<model-name>-epp -n <namespace>`

## Installation and Deployment

**Single Source of Truth**: All deployment scripts in WVA repository (`${WVA_REPO_PATH}`).

**IMPORTANT - Deployment Methods:**

### Method 1: Makefile Deployment (Requires Go)

**Prerequisites**: Go must be installed on the system.

```bash
cd ${WVA_REPO_PATH}

# Kubernetes
make deploy-wva-on-k8s IMG=ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest

# OpenShift
make deploy-wva-on-openshift IMG=ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest

# Kind (local testing)
make deploy-wva-emulated-on-kind

# Full stack (WVA + llm-d + monitoring)
make deploy-e2e-infra ENVIRONMENT=kubernetes
```

### Method 2: Helm Deployment 

**Use Helm when Go is not available or for simpler deployments:**

```bash
cd ${WVA_REPO_PATH}

# Namespace-scoped deployment (RECOMMENDED)
# Deploy WVA INTO the target namespace to watch only that namespace
helm upgrade --install workload-variant-autoscaler ./charts/workload-variant-autoscaler \
  --namespace <your-target-namespace> \
  --create-namespace \
  --set controller.namespaceScoped=true

# Example: Deploy WVA to watch only example-namespace namespace
helm upgrade --install workload-variant-autoscaler ./charts/workload-variant-autoscaler \
  --namespace example-namespace \
  --create-namespace \
  --set controller.namespaceScoped=true
```

**CRITICAL Configuration Notes:**
- **Deploy WVA INTO the namespace you want to watch** - don't use `watchNamespace` parameter
- **Set `controller.namespaceScoped=true`** - this makes WVA watch its own deployment namespace
- **Verify after deployment**: Check logs to confirm it's watching the correct namespace
- This approach is simpler and more predictable than using `watchNamespace` parameter

**IMPORTANT - Three-Phase Process:**

### Phase 1: Deploy WVA Controller (Infrastructure)

This phase deploys the WVA controller infrastructure. **CRITICAL**: Deploy WVA INTO the target namespace (where your llm-d deployment lives) with `namespaceScoped: true`.

**Deployment command:**
```bash
cd ${WVA_REPO_PATH}

# Deploy WVA into the target namespace
helm upgrade --install workload-variant-autoscaler ./charts/workload-variant-autoscaler \
  --namespace <target-namespace> \
  --create-namespace \
  --set controller.namespaceScoped=true
```

**What gets deployed in Phase 1:**
- WVA controller in target namespace (watches only that namespace)
- Default ConfigMaps with standard thresholds
- ServiceMonitor for WVA metrics

**Verification:**
```bash
# Verify WVA is running and watching correct namespace
kubectl get deployment -n <target-namespace> -l app.kubernetes.io/name=workload-variant-autoscaler
kubectl logs -n <target-namespace> -l app.kubernetes.io/name=workload-variant-autoscaler --tail=20 | grep "Watching"
# Should show: "Watching single namespace: <target-namespace>"
```

### Phase 2: Prepare Deployment for WVA

**CRITICAL**: Before applying VariantAutoscaling, ensure the deployment has required labels and metrics:

```bash
# 1. Add required accelerator label to deployment
kubectl label deployment <deployment-name> -n <namespace> \
  inference.optimization/acceleratorName=nvidia

# 2. Ensure vLLM metrics are exposed (if not already)
# Create Service for metrics endpoint
kubectl apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: <deployment-name>-metrics
  namespace: <namespace>
spec:
  selector:
    app: <deployment-name>
  ports:
  - name: metrics
    port: 8000
    targetPort: 8000
EOF

# 3. Create ServiceMonitor for Prometheus scraping
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
```

### Phase 3: Apply User-Specific Configuration

After Phase 1 and 2 complete, apply the configuration built in Section 2 based on user requirements:

```bash
# 1. Apply saturation thresholds ConfigMap (if custom values needed)
kubectl apply -f <generated-saturation-config>.yaml

# 2. Apply VariantAutoscaling resource (built from user input in Section 2)
kubectl apply -f <generated-variantautoscaling>.yaml

# 3. Apply HPA with user-specified stabilization windows
kubectl apply -f <generated-hpa>.yaml

# 4. Verify WVA detects the resource
kubectl logs -n workload-variant-autoscaler-system \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "VariantAutoscaling"

# 5. Verify configuration
./scripts/verify-wva.sh <namespace>
```

**Configuration Flow:**
1. Section 2: Gather user requirements and auto-detect deployment details
2. Phase 1: Deploy WVA infrastructure (generic)
3. Phase 2: Prepare deployment with labels and metrics
4. Phase 3: Apply user-specific VariantAutoscaling, HPA, and threshold configs
5. Verification: Ensure WVA detects resources and scaling works
### Immediate Verification After Each Phase

**CRITICAL**: Verify each phase before proceeding to the next.

**Phase 1 Verification (WVA Controller)**:
```bash
# Check controller is running in target namespace
kubectl get deployment -n <target-namespace> -l app.kubernetes.io/name=workload-variant-autoscaler

# Verify namespace-scoping is correct
kubectl logs -n <target-namespace> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "Watching"
# Should show: "Watching single namespace: <target-namespace>"
```

**Phase 2 Verification (Deployment Preparation)**:
```bash
# Verify accelerator label was added
kubectl get deployment <name> -n <namespace> --show-labels | grep acceleratorName

# Verify metrics infrastructure exists
kubectl get service,servicemonitor -n <namespace> | grep metrics

# Test metrics endpoint
kubectl exec -n <namespace> <pod-name> -- curl -s localhost:8000/metrics | grep vllm
```

**Phase 3 Verification (Configuration Applied)**:
```bash
# Check resources were created
kubectl get variantautoscaling,hpa -n <namespace>

# CRITICAL: Verify WVA detected the VariantAutoscaling
kubectl logs -n workload-variant-autoscaler-system \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "VariantAutoscaling"
# Should NOT show "No active VariantAutoscalings found"

# Check METRICSREADY status (wait 2 minutes for Prometheus scrape)
sleep 120
kubectl get variantautoscaling -n <namespace>
# Should show METRICSREADY: True
```

### Success Metrics

A successful WVA deployment should show:
- ✅ WVA controller logs show "VariantAutoscaling" detected (not "No active VariantAutoscalings found")
- ✅ METRICSREADY: True within 2 minutes
- ✅ HPA shows valid metrics (not <unknown>)
- ✅ Deployment scales up under load
- ✅ Deployment scales down after stabilization window
- ✅ No error messages in WVA controller logs


### Upgrading WVA

**Critical**: Helm doesn't auto-update CRDs. Manual CRD update required before upgrade:

```bash
# 1. Apply updated CRDs first
kubectl apply -f charts/workload-variant-autoscaler/crds/

# 2. Then upgrade Helm release
helm upgrade workload-variant-autoscaler ./charts/workload-variant-autoscaler \
  --namespace workload-variant-autoscaler-system
```

**Breaking change v0.5.1**: `scaleTargetRef` now required. Update existing VAs:
```yaml
spec:
  scaleTargetRef:
    kind: Deployment  # or StatefulSet, LeaderWorkerSet
    name: <deployment-name>
```

### Repository Resources

**WVA Repository** (`${WVA_REPO_PATH}`):
- **Deployment**: `deploy/install.sh`, `deploy/install-multi-model.sh`, `deploy/lib/*.sh`
- **Configuration**: `deploy/configmap-*.yaml` (saturation, queueing, serviceclass)
- **Documentation**: `docs/user-guide/` (configuration.md, troubleshooting.md, crd-reference.md, hpa-integration.md, keda-integration.md)
- **Makefile**: Run `make help` for 40+ deployment/testing targets
- **Samples**: `config/samples/`

**llm-d Repository** (`${LLMD_REPO_PATH}`):
- **WVA Integration**: `guides/workload-autoscaling/README.wva.md`
- **Helmfile**: `guides/workload-autoscaling/helmfile.yaml.gotmpl`
- **Values**: `guides/workload-autoscaling/workload-autoscaling/values.yaml`

**Common Makefile Targets** (run in `${WVA_REPO_PATH}`):
```bash
make deploy-wva-on-k8s                    # Kubernetes deployment
make deploy-wva-on-openshift              # OpenShift deployment
make deploy-e2e-infra ENVIRONMENT=kubernetes  # Full stack
make deploy-multi-model-infra MODELS="m1,m2"  # Multi-model
make test-e2e-smoke                       # Quick tests
```

**Benchmark Templates**: `deployments/*/benchmark-templates/` - See run-llm-d-benchmark skill

## Verification, Troubleshooting & Critical Requirements

### Critical Configuration Requirements

**Two requirements for WVA to work**:
1. **VariantAutoscaling MUST have accelerator label**: `inference.optimization/acceleratorName: nvidia`
2. **HPA selector MUST match both labels**: `variant_name` and `exported_namespace`

**Using existing WVA controller**: If a controller exists, **ASK USER** based on isolation requirements (Section 1):
- Reuse existing (VariantAutoscaling auto-discovered) OR
- Deploy new namespace-scoped controller (complete isolation)

### Verification Scripts

**After deployment** (automatic via `${WVA_REPO_PATH}/deploy/install.sh`):
- Checks: WVA controller, Prometheus, llm-d infra, VariantAutoscaling, scaler backend

**Runtime verification** (skill scripts):
```bash
./scripts/verify-wva.sh <namespace>                    # Status, HPA, logs, metrics
./scripts/troubleshoot-metrics.sh <namespace> <pod>    # Metrics diagnostics
./scripts/troubleshoot-scaling.sh <namespace>          # Scaling diagnostics
```

**Manual verification** (WVA repository):
```bash
source ${WVA_REPO_PATH}/deploy/lib/verify.sh
export WVA_NS="workload-variant-autoscaler-system"
verify_deployment
```

### Common Issues

**Most common**: Missing accelerator label, wrong HPA selector, metrics not scraped yet

See [`Troubleshooting.md`](./Troubleshooting.md) and [`scripts/SCRIPTS.md`](./scripts/SCRIPTS.md) for detailed solutions.

## Best Practices

1. **Start with defaults**: Use default thresholds initially, tune based on observed behavior
2. **Align thresholds**: Keep WVA and EPP thresholds synchronized
3. **Monitor first**: Observe saturation patterns before aggressive tuning
4. **Stabilization windows**: Use longer windows (120s+ scale-up, 300s+ scale-down) to prevent flapping
5. **Test with load**: Use llm-d-benchmark to validate scaling behavior under realistic load
6. **Cost optimization**: For multi-variant setups, set variantCost accurately to reflect actual costs
7. **Scale-to-zero**: Only enable in dev/test environments, not production (cold start latency)

## Automated Deployment Script

**IMPORTANT**: Generate a customized deployment script based on user requirements using the template system.

### Generating a Deployment Script

The agent should use [`generate-deploy-script.sh`](scripts/generate-deploy-script.sh) to create a customized deployment script from the [`deploy-wva.sh.template`](scripts/deploy-wva.sh.template):

**Agent Workflow:**
1. Gather user requirements (namespace, deployment name, model ID, thresholds, etc.)
2. Run the generator script with collected values
3. Review the generated script with the user
4. Execute the generated script to deploy WVA

**Generator Usage:**
```bash
cd skills/configure-wva-autoscaling-llm-d/scripts

# Interactive mode (prompts for all values)
./generate-deploy-script.sh

# Command-line mode (all values provided)
./generate-deploy-script.sh \
  --namespace example-namespace \
  --deployment my-llm-deployment \
  --wva-repo /path/to/wva-repo \
  --model-id "Qwen/Qwen3-32B" \
  --variant-cost "100" \
  --min-replicas 2 \
  --max-replicas 10 \
  --kv-threshold 0.80 \
  --queue-threshold 5 \
  --scale-up-window 120 \
  --scale-down-window 300 \
  --output deploy-wva-qwen32.sh
```

**What the generated script does:**
1. **Phase 1**: Deploys WVA controller into target namespace with `namespaceScoped: true`
2. **Phase 2**: Adds required labels and creates metrics infrastructure
3. **Phase 3**: Creates VariantAutoscaling and HPA resources with user-specified configuration
4. **Verification**: Checks deployment status and waits for metrics to be ready

**Template Variables:**
- `{{NAMESPACE}}` - Target namespace
- `{{DEPLOYMENT_NAME}}` - Deployment name
- `{{WVA_REPO_PATH}}` - Path to WVA repository
- `{{MODEL_ID}}` - Model identifier
- `{{VARIANT_COST}}` - Variant cost (default: "100")
- `{{PROMETHEUS_URL}}` - Prometheus URL (optional)
- `{{MIN_REPLICAS}}` / `{{MAX_REPLICAS}}` - Replica limits (default: 2/10)
- `{{KV_CACHE_THRESHOLD}}` - KV cache threshold (default: 0.80)
- `{{QUEUE_LENGTH_THRESHOLD}}` - Queue threshold (default: 5)
- `{{SCALE_UP_STABILIZATION}}` - Scale-up window in seconds (default: 120)
- `{{SCALE_DOWN_STABILIZATION}}` - Scale-down window in seconds (default: 300)

**Benefits of Template Approach:**
- ✅ Agent can customize all parameters based on user requirements
- ✅ No need for separate YAML files - everything embedded in script
- ✅ User gets a single executable script for their specific deployment
- ✅ Script can be version controlled with the deployment
- ✅ Easy to regenerate with different parameters

## Phase 4: Optional Load Testing

**IMPORTANT**: After successful deployment and verification, **ASK THE USER** if they want to test WVA autoscaling with load.

### When to Test
- User wants to validate WVA is working correctly
- User wants to see scaling in action
- User wants to tune thresholds based on observed behavior

### Using test-wva-scaling.sh

The [`test-wva-scaling.sh`](scripts/test-wva-scaling.sh) script automates load testing and monitoring:

**Usage:**
```bash
cd skills/configure-wva-autoscaling-llm-d/scripts
./test-wva-scaling.sh <namespace> <deployment-name> [model-id] [num-requests]
```

**Example:**
```bash
./test-wva-scaling.sh example-namespace my-llm-deployment "Qwen/Qwen3-32B" 100
```

**What the script does:**
1. Records baseline state (current replicas, VariantAutoscaling status)
2. Sends concurrent requests with long outputs to increase KV cache usage
3. Monitors WVA logs for scaling decisions
4. Checks vLLM metrics (KV cache usage, queue depth)
5. Waits for scaling to occur (respects stabilization window)
6. Reports final status and provides analysis

**Parameters:**
- `namespace`: Target namespace
- `deployment-name`: Name of the deployment to test
- `model-id`: (Optional) Model ID - will auto-detect if not provided
- `num-requests`: (Optional) Number of concurrent requests (default: 100)

**Note**: The script automatically detects the EPP service name and model ID if not provided.

### Expected Behavior

**During load:**
- KV cache usage should increase (visible in vLLM metrics)
- Queue depth may increase if load exceeds capacity
- WVA should detect saturation and recommend scale-up
- HPA should scale deployment up (after stabilization window ~120s)

**After load stops:**
- KV cache usage returns to low levels
- Queue depth returns to 0
- WVA should recommend scale-down
- HPA should scale deployment down (after stabilization window ~300s)

### Troubleshooting Test Results

The test script provides automatic analysis and troubleshooting suggestions. Common issues:

**If no scale-up occurs:**
- Load was insufficient to trigger saturation
  - Solution: Increase `num-requests` parameter or use longer `max_tokens`
- Deployment already at maxReplicas
  - Solution: Check and increase maxReplicas in VariantAutoscaling
- Stabilization window not elapsed
  - Solution: Wait longer (default scale-up window is 120s)
- WVA not monitoring correctly
  - Solution: Run `./troubleshoot-scaling.sh <namespace>`

**If scale-up is too slow:**
- Reduce `scaleUpStabilization` window in HPA (default 120s)
- Lower saturation thresholds in ConfigMap (e.g., `kvCacheThreshold: 0.70`)

**If scale-down doesn't occur:**
- Wait longer: Scale-down stabilization is typically 300s
- Check if new requests are still coming in
- Verify `scaleDownSafe: true` in WVA logs

For detailed troubleshooting, see [`Troubleshooting.md`](./Troubleshooting.md).

## Output Format

When helping users configure WVA:

1. **Ask clarifying questions** about their requirements (especially scaling backend preference)
2. **Provide specific YAML configurations** based on their needs
3. **Explain the reasoning** behind configuration choices (e.g., why HPA is recommended)
4. **Deploy WVA correctly** - INTO the target namespace with `namespaceScoped: true`
5. **Create a deployment script** to automate the setup
6. **Include monitoring commands** to verify the setup
7. **After successful deployment, ASK if user wants to test** - don't assume
8. **If user agrees to test, execute Phase 4** - send load and monitor scaling
9. **Link to relevant documentation** for deeper understanding

**Do not**:
- Skip asking about scaling backend preference
- Deploy WVA to wrong namespace (must be IN target namespace, not workload-variant-autoscaler-system)
- Assume HPA without explaining why it's recommended
- Create generic configurations without understanding requirements
- Skip threshold alignment with EPP
- Forget to explain the "why" behind configurations
- Create comprehensive README files (user doesn't want them)
- Stop at just creating scripts/configs - **actually deploy to the user's llm-d deployment**
- Skip asking about testing - always offer Phase 4 as optional after successful deployment

## Online Resources

- **WVA**: https://github.com/llm-d/llm-d-workload-variant-autoscaler
- **llm-d**: https://github.com/llm-d/llm-d
- **llm-d-benchmark**: https://github.com/llm-d/llm-d-benchmark
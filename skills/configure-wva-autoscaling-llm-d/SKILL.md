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

**Configuration**:
```bash
# Deploy WVA to watch only your specific namespace
helm upgrade -i wva ./charts/workload-variant-autoscaler \
  --namespace wva-system \
  --set controller.watchNamespace=my-namespace
```

Or set via environment variable in the deployment:
```yaml
env:
- name: WATCH_NAMESPACE
  value: "my-namespace"
```

**Behavior**:
- ✅ Only manages VariantAutoscaling resources in your namespace
- ✅ Ignores all other namespaces completely
- ✅ Perfect for multi-tenant clusters where each team has their own controller
- ✅ No interference with other teams' deployments

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

**Choose Scaling Backend:**
- **HPA** (Kubernetes native): Standard Kubernetes clusters, simpler setup
- **KEDA**: OpenShift (via CMA), native scale-to-zero support, event-driven scaling

**Important**: All threshold values below are examples. Ask the user for their preferred values based on their workload characteristics. If the user is unsure, use the default values shown in parentheses.

Configure these components:

**1. VariantAutoscaling Resource** ([template](scripts/configs/variantautoscaling-basic.yaml))
- **`scaleTargetRef`** (required): Points to the Deployment/StatefulSet/LWS that WVA will scale. Must match the actual resource name and kind.
- **`modelID`** (required): Identifies which model this variant serves (e.g., `meta-llama/Llama-3.1-8B`). Used for metrics grouping and multi-variant coordination.
- **`variantCost`** (optional): Relative cost value (e.g., H100=100, A100=70, L4=30). WVA scales cheaper variants first when multiple variants serve the same model.

**2. Saturation Thresholds** (via ConfigMap: `wva-saturation-scaling-config`)
- `kvCacheThreshold` (0.80): When KV cache usage exceeds this (80%), replica is considered saturated. Triggers scale-up.
- `queueLengthThreshold` (5): When request queue exceeds this length, replica is saturated. Prevents latency spikes.
- `kvSpareTrigger` (0.10): Proactive scaling - adds capacity when average spare KV capacity drops below 10%. Scales before saturation.
- `queueSpareTrigger` (3): Proactive scaling - adds capacity when average spare queue capacity drops below 3 requests.
- **Namespace-local overrides**: Create same ConfigMap in target namespace to override global thresholds for specific namespaces (e.g., production vs dev).

**3. Controller Configuration** (via ConfigMap: `wva-variantautoscaling-config`)
- `PROMETHEUS_BASE_URL` (required): Where WVA fetches metrics from (e.g., `http://prometheus:9090`). Must be accessible from WVA controller.
- `GLOBAL_OPT_INTERVAL` (default: 60s): How frequently WVA recalculates desired replicas. Lower = faster response, higher = more stable.

**4. Scaling Backend**
- **HPA** ([template](scripts/configs/hpa-basic.yaml), [guide](${WVA_REPO_PATH}/docs/user-guide/hpa-integration.md))
  - Reads `wva_desired_replicas` metric and adjusts deployment replicas
  - Stabilization: 0-60s scale-up (fast response), 240-300s scale-down (prevent flapping)
- **KEDA** ([guide](${WVA_REPO_PATH}/docs/user-guide/keda-integration.md))
  - Alternative to HPA with native scale-to-zero support
  - Preferred for OpenShift (via Custom Metrics Autoscaler)

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
- Include `inference.optimization/acceleratorName` label on deployments
- HPA selector must match `variant_name` + `exported_namespace`
- Multi-variant: WVA scales cheaper variants first based on `variantCost`
- Align thresholds with Inference Scheduler (EPP) - see section 4

See [`scripts/SCRIPTS.md`](./scripts/SCRIPTS.md) for detailed examples.

### 4. Threshold Alignment with Inference Scheduler (EPP)

**Critical**: WVA and EPP must use identical thresholds. Each model has its own EPP instance (1-to-1 relationship).

**Why misalignment causes issues:**
- EPP stops routing to saturated replicas → WVA still sees capacity → doesn't scale
- WVA scales up → EPP still routes to old replicas → new capacity unused

**Threshold mapping:**
- WVA `kvCacheThreshold` = EPP `kvCacheUtilThreshold` (default: 0.80)
- WVA `queueLengthThreshold` = EPP `queueDepthThreshold` (default: 5)

**How to align per model:**
1. Update WVA ConfigMap: `wva-saturation-scaling-config` (changes apply immediately)
2. Update EPP config for that model's EPP instance
3. Restart EPP pod: `kubectl rollout restart deployment/gaie-<model-name>-epp -n <namespace>`

**Note**: EPP requires pod restart for config changes; WVA auto-reloads ConfigMap changes.

## Installation and Deployment

**Single Source of Truth**: All deployment scripts in WVA repository (`${WVA_REPO_PATH}`).

### Quick Start - Deploy WVA

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

### Key WVA Repository Resources

**Deployment Scripts** (`${WVA_REPO_PATH}/deploy/`):
- `install.sh` - Main installation script (handles all environments)
- `install-multi-model.sh` - Multi-model deployment
- `kind-emulator/setup.sh` - Kind cluster with GPU emulation
- `lib/*.sh` - Modular deployment functions

**Configuration** (`${WVA_REPO_PATH}/deploy/`):
- `configmap-saturation-scaling.yaml` - Saturation thresholds
- `configmap-queueing-model.yaml` - Queue depth configuration
- `configmap-serviceclass.yaml` - Service class definitions

**Documentation** (`${WVA_REPO_PATH}/docs/`):
- `user-guide/configuration.md` - Complete configuration guide
- `user-guide/troubleshooting.md` - Troubleshooting guide
- `user-guide/crd-reference.md` - CRD API reference
- `saturation-scaling-config.md` - Saturation scaling details

**Makefile** (`${WVA_REPO_PATH}/Makefile`):
- 40+ targets for deployment, testing, and development
- Run `make help` for full list of available targets

### llm-d Repository Resources

**WVA Integration** (`${LLMD_REPO_PATH}/guides/workload-autoscaling/`):
- `README.wva.md` - Complete WVA setup guide
- `helmfile.yaml.gotmpl` - Helmfile templates
- `workload-autoscaling/values.yaml` - Helm values

### Common Makefile Targets

For complete list of targets and options, run `make help` in `${WVA_REPO_PATH}` or see the [Makefile](${WVA_REPO_PATH}/Makefile).

**Most Used Targets**:
```bash
# Deployment
make deploy-wva-on-k8s          # Deploy on Kubernetes
make deploy-wva-on-openshift    # Deploy on OpenShift
make deploy-wva-emulated-on-kind # Deploy on Kind (local)
make deploy-e2e-infra           # Deploy full stack (WVA + llm-d + monitoring)

# Multi-model
make deploy-multi-model-infra MODELS="model1,model2"

# Testing
make test-e2e-smoke             # Quick smoke tests (basic functionality check)
make test-e2e-full              # Full test suite (comprehensive tests)
make test-benchmark             # Benchmark tests (performance validation)

# Development
make docker-build               # Build controller image
make manifests                  # Generate CRDs
make test                       # Run unit tests

```

**Key Configuration Variables**:
- `ENVIRONMENT`: `kind-emulator`, `kubernetes`, `openshift`
- `SCALER_BACKEND`: `prometheus-adapter`, `keda`, `none`
- `MODELS`: Comma-separated model list for multi-model
- `BENCHMARK_SCENARIO`: `prefill_heavy`, `decode_heavy`, `symmetrical`
- `IMG`: Controller image
- `NAMESPACE_SCOPED`: Deploy namespace-scoped controller

For complete variable list, see `${WVA_REPO_PATH}/Makefile` (lines 1-50).

### Testing with llm-d-benchmark

Benchmark templates are in deployment directories:
- Templates: `deployments/*/benchmark-templates/` (guide.yaml, guidellm.yaml, sanity.yaml, shared_prefix.yaml)
- Use `run_only.sh` with instantiated config files
- See the run-llm-d-benchmark skill for detailed workflow

## Monitoring and Verification

### Deployment Verification (from WVA Repository)

After deploying WVA using the installation scripts, the deployment is automatically verified by `${WVA_REPO_PATH}/deploy/lib/verify.sh`. This checks:
- WVA controller pods are running
- Prometheus is running (if deployed)
- llm-d infrastructure is deployed (if enabled)
- VariantAutoscaling resources exist
- Scaler backend (KEDA or Prometheus Adapter) is running

The `deploy/install.sh` script automatically calls this verification and provides a comprehensive summary.

### Runtime Verification (from Skill Scripts)

After WVA is deployed and configured, use the skill verification scripts to check runtime status:

```bash
# Comprehensive runtime verification
./scripts/verify-wva.sh <namespace>
```

This script checks:
- VariantAutoscaling status (METRICSREADY, CURRENTREPLICAS, DESIREDREPLICAS, SATURATION)
- HPA status and metrics
- WVA controller logs
- External metrics API availability

### Troubleshooting Scripts

```bash
# Troubleshoot metrics issues
./scripts/troubleshoot-metrics.sh <namespace> <pod-name>

# Troubleshoot scaling decisions
./scripts/troubleshoot-scaling.sh <namespace>
```

### Using WVA Repository Verification

You can also use the WVA repository's verification function directly:

```bash
# Source the verification library
source ${WVA_REPO_PATH}/deploy/lib/verify.sh
source ${WVA_REPO_PATH}/deploy/lib/common.sh

# Set required variables
export WVA_NS="workload-variant-autoscaler-system"
export LLMD_NS="llm-d-inference-scheduler"
export MONITORING_NAMESPACE="monitoring"

# Run verification
verify_deployment
```

## Critical Configuration Requirements

**Two critical requirements for WVA to work**:

1. **VariantAutoscaling MUST have accelerator label**: `inference.optimization/acceleratorName: nvidia`
2. **HPA selector MUST match both labels**: `variant_name` and `exported_namespace`

See [`scripts/SCRIPTS.md`](./scripts/SCRIPTS.md) for detailed examples and [`Troubleshooting.md`](./Troubleshooting.md) for common issues.

**Using existing WVA controller**: If a WVA controller exists in another namespace, just create your VariantAutoscaling - it will be automatically discovered. Update saturation config in the controller's namespace if needed.

## Troubleshooting

For detailed troubleshooting guidance, see [`Troubleshooting.md`](./Troubleshooting.md) and [`scripts/SCRIPTS.md`](./scripts/SCRIPTS.md).

**Quick diagnostics**:
```bash
./scripts/verify-wva.sh <namespace>              # Comprehensive verification
./scripts/troubleshoot-metrics.sh <namespace>    # Check metrics issues
./scripts/troubleshoot-scaling.sh <namespace>    # Check scaling behavior
```

**Most common issues**: Missing accelerator label, wrong HPA label selector, or metrics not yet scraped. See Troubleshooting.md for solutions.

## Best Practices

1. **Start with defaults**: Use default thresholds initially, tune based on observed behavior
2. **Align thresholds**: Keep WVA and EPP thresholds synchronized
3. **Monitor first**: Observe saturation patterns before aggressive tuning
4. **Stabilization windows**: Use longer windows (120s+ scale-up, 300s+ scale-down) to prevent flapping
5. **Test with load**: Use llm-d-benchmark to validate scaling behavior under realistic load
6. **Cost optimization**: For multi-variant setups, set variantCost accurately to reflect actual costs
7. **Scale-to-zero**: Only enable in dev/test environments, not production (cold start latency)

## Output Format

When helping users configure WVA:

1. **Ask clarifying questions** about their requirements
2. **Provide specific YAML configurations** based on their needs
3. **Explain the reasoning** behind configuration choices
4. **Include monitoring commands** to verify the setup
5. **Link to relevant documentation** for deeper understanding

**Do not**:
- Create new automation scripts (use existing ones)
- Provide generic configurations without understanding requirements
- Skip threshold alignment with EPP
- Forget to explain the "why" behind configurations

## Reference Documentation

For detailed information, refer to these files in the repositories:

**WVA Repository** (`${WVA_REPO_PATH}`):
- **Configuration Guide**: `docs/user-guide/configuration.md`
- **Saturation Scaling**: `docs/saturation-scaling-config.md`
- **CRD Reference**: `docs/user-guide/crd-reference.md`
- **Troubleshooting**: `docs/user-guide/troubleshooting.md`
- **HPA Integration**: `docs/user-guide/hpa-integration.md`
- **KEDA Integration**: `docs/user-guide/keda-integration.md`
- **Configuration Samples**: `config/samples/`

**llm-d Repository** (`${LLMD_REPO_PATH}`):
- **Installation Guide**: `guides/workload-autoscaling/README.wva.md`
- **HPA+IGW Guide**: `guides/workload-autoscaling/README.hpa-igw.md`
- **Helmfile Templates**: `guides/workload-autoscaling/helmfile.yaml.gotmpl`
- **Values Configuration**: `guides/workload-autoscaling/workload-autoscaling/values.yaml`

**Benchmark Templates** (in deployment directories):
- **Template Location**: `deployments/*/benchmark-templates/`
- **Available Templates**: guide.yaml, guidellm.yaml, sanity.yaml, shared_prefix.yaml
- **Benchmark Script**: `run_only.sh` (use with instantiated config files)

**Online Resources**:
- **WVA GitHub**: https://github.com/llm-d/llm-d-workload-variant-autoscaler
- **llm-d GitHub**: https://github.com/llm-d/llm-d
- **llm-d-benchmark GitHub**: https://github.com/llm-d/llm-d-benchmark
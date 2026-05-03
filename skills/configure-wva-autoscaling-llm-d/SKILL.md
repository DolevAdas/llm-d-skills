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

**Configuration Detection and User Input:**

1. **Auto-detect from deployment:**
   - Deployment/StatefulSet/LWS name, kind, namespace
   - Model ID (from labels/env vars if available)
   - Current replicas, existing HPA
   - Accelerator type

2. **Ask user for:**
   - **Scaling backend**: HPA or KEDA
   - **Scaling behavior**: Fast/balanced/cost-optimized
   - **Stabilization windows**: Scale-up (default 120s, range 0-300s), Scale-down (default 300s, range 120-600s)
   - **Replica limits**: minReplicas, maxReplicas
   - **Saturation thresholds** (if custom needed):
     - `kvCacheThreshold` (default 0.80), `queueLengthThreshold` (default 5)
     - `kvSpareTrigger` (default 0.10), `queueSpareTrigger` (default 3)
   - **Multi-variant**: Variant cost, other variants for same model

3. **Must ask if not detected:**
   - Model ID, variant cost, scale-to-zero requirement

**Key Components:**

**VariantAutoscaling** ([template](scripts/configs/variantautoscaling-basic.yaml)):
- `scaleTargetRef`: Target deployment/statefulset/LWS
- `modelID`: Model identifier (e.g., `meta-llama/Llama-3.1-8B`)
- `variantCost`: Relative cost (H100=100, A100=70, L4=30) for multi-variant optimization

**Saturation Thresholds** (ConfigMap: `wva-saturation-scaling-config`):
- `kvCacheThreshold` (0.80): Saturation trigger when KV cache exceeds 80%
- `queueLengthThreshold` (5): Saturation trigger when queue exceeds 5 requests
- `kvSpareTrigger` (0.10): Proactive scaling when spare capacity drops below 10%
- `queueSpareTrigger` (3): Proactive scaling when spare queue capacity drops below 3
- **Namespace overrides**: Create ConfigMap in target namespace for local thresholds

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
- Include `inference.optimization/acceleratorName` label on deployments
- HPA selector must match `variant_name` + `exported_namespace`
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

**IMPORTANT - Two-Phase Process:**

### Phase 1: Deploy WVA Controller (Infrastructure)

This phase deploys the WVA controller infrastructure with **default/generic settings**. The user-specific configuration from Section 2 is applied in Phase 2.

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

**What gets deployed in Phase 1:**
- WVA controller with default settings
- Prometheus (if not already present)
- Scaler backend (HPA or KEDA)
- Default ConfigMaps with standard thresholds

### Phase 2: Apply User-Specific Configuration

After Phase 1 completes, apply the configuration built in Section 2 based on user requirements:

```bash
# 1. Apply VariantAutoscaling resource (built from user input in Section 2)
kubectl apply -f <generated-variantautoscaling>.yaml

# 2. Apply HPA with user-specified stabilization windows
kubectl apply -f <generated-hpa>.yaml

# 3. Update saturation thresholds if user requested custom values
kubectl apply -f <generated-saturation-config>.yaml

# 4. Verify configuration
./scripts/verify-wva.sh <namespace>
```

**Configuration Flow:**
1. Section 2: Gather user requirements and auto-detect deployment details
2. Phase 1: Deploy WVA infrastructure (generic)
3. Phase 2: Apply user-specific VariantAutoscaling, HPA, and threshold configs
4. Verification: Ensure everything works as expected

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

## Online Resources

- **WVA**: https://github.com/llm-d/llm-d-workload-variant-autoscaler
- **llm-d**: https://github.com/llm-d/llm-d
- **llm-d-benchmark**: https://github.com/llm-d/llm-d-benchmark
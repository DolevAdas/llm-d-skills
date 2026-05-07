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

## Prerequisites

**Required Repositories**: llm-d, llm-d-workload-variant-autoscaler

**Setup Process**:
1. Check for repositories in common locations
2. Ask user for paths if not found
3. Clone missing repositories with user approval
4. Set environment variables: `LLMD_REPO_PATH`, `WVA_REPO_PATH`

**Note**: llm-d-benchmark is optional for testing/validation. Benchmark templates are in deployment directories.

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

## Core Workflow

### 1. Confirm Namespace Scope

**CRITICAL - User Confirmation Required**: WVA operations can be destructive (scaling deployments, modifying resources). **ALWAYS ask the user to confirm the namespaces that will be in scope** before proceeding with any WVA deployment or configuration.

**SAFETY FIRST**: This skill ONLY supports **Namespace-Scoped Mode** to prevent accidental changes to unintended llm-d stacks. WVA will ONLY watch the specific namespace(s) you explicitly configure.

#### Namespace-Scoped Deployment (Only Supported Mode)

Deploy WVA with `NAMESPACE_SCOPED=true` to watch ONLY a specific namespace:

```bash
cd ${WVA_REPO_PATH}

make deploy-wva-on-k8s \
  IMG=ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest \
  WVA_NS=my-target-namespace \
  NAMESPACE_SCOPED=true
```

**Behavior**:
- ✅ WVA deployed in `my-target-namespace`
- ✅ Watches ONLY `my-target-namespace` (completely ignores all other namespaces)
- ✅ Perfect isolation - zero risk of affecting other namespaces
- ✅ Multiple WVA instances can safely coexist (one per namespace)
- ✅ Each namespace has its own independent WVA controller

**Multi-namespace**: Run the same command once per namespace, changing `WVA_NS` each time. Each WVA instance is fully isolated:

```
Cluster with multiple isolated WVA deployments:
├── namespace: team-a-prod  → WVA watches ONLY team-a-prod
├── namespace: team-b-prod  → WVA watches ONLY team-b-prod
└── namespace: team-c-dev   → WVA watches ONLY team-c-dev
```

**Existing WVA controller**: If a controller already exists, **ASK USER** based on isolation requirements:
- Reuse existing controller (VariantAutoscaling auto-discovered), OR
- Deploy a new namespace-scoped controller (complete isolation)

#### User Confirmation Prompt

Before deploying or configuring WVA, present the user with:

```
⚠️  WVA Configuration Confirmation Required

Deployment Mode: Namespace-Scoped (Safe Mode)

Target namespaces for WVA deployment:
  • namespace1 (X existing llm-d deployments detected)
  • namespace2 (Y existing llm-d deployments detected)
  • namespace3 (Z existing llm-d deployments detected)

⚠️  WARNING: WVA can scale deployments and modify resources in these namespaces.

Configuration:
  - Separate WVA controller will be deployed in each namespace
  - Each WVA watches ONLY its own namespace
  - Complete isolation - no cross-namespace effects
  - Total WVA instances to deploy: [N]

Do you want to proceed with this configuration? (yes/no)
```

**IMPORTANT**:
- Always list ALL namespaces where WVA will be deployed
- Show how many llm-d deployments are detected in each namespace
- Clearly state that each namespace gets its own isolated WVA controller
- Wait for explicit user confirmation before proceeding
- If user says no, ask which namespaces should be included/excluded
- **NEVER deploy WVA in cluster-wide mode** - this skill only supports namespace-scoped mode for safety

### 2. Gather Configuration

**Configuration Detection and User Input:**

1. **Auto-detect from deployment:**
   - Deployment/StatefulSet/LWS name, kind, namespace
   - Model ID (from labels/env vars if available)
   - Current replicas, existing HPA
   - Accelerator type

2. **ALWAYS ask user for** (these become Makefile vars in the deploy command):

   **Ask the scaling backend FIRST** — never assume:

   | Backend | `SCALER_BACKEND` value | When to use |
   |---------|----------------------|-------------|
   | **HPA** (Prometheus Adapter) | `prometheus-adapter` | Standard; works out-of-box with kube-prometheus-stack |
   | **KEDA** | `keda` | KEDA already on cluster, or scale-to-zero is needed |

   **First, ask the user:** "Do you already know the values you want to set, or would you like help deciding them based on your requirements?"
   - If they **know the values** → collect them directly and proceed to deploy
   - If they **need guidance** → ask about their priorities to help decide each value:
     - **Latency sensitivity**: fast response to load spikes → lower thresholds, shorter stabilization
     - **Stability**: production workload, avoid flapping → higher thresholds, longer stabilization
     - **Cost**: multiple GPU types available → use multi-variant with `variantCost`
     - **Scale-to-zero**: dev/test only → `HPA_MIN_REPLICAS=0` requires KEDA

   Always show the value being set — never hide it from the user.

   Then collect:
   - **Stabilization window**: `HPA_STABILIZATION_SECONDS` (default 240s; shorter = faster reaction, longer = more stable) — sets both scale-up and scale-down windows **symmetrically**; for asymmetric behavior (e.g., fast scale-up / slow scale-down), use `helm upgrade` after deploy to set them independently (see [example3](scripts/configs/example.yaml) for the HPA YAML structure)
   - **Replica limits**: `HPA_MIN_REPLICAS` (default 1), `HPA_MAX_REPLICAS` (default 2 — override via `--set hpa.maxReplicas=N` post-deploy)
   - **Saturation Thresholds**:
     - `KV_CACHE_THRESHOLD` (default 0.80): Replica saturated when KV cache ≥ threshold
     - `QUEUE_LENGTH_THRESHOLD` (default 5): Replica saturated when queue ≥ threshold
     - `KV_SPARE_TRIGGER` (default 0.10): Proactive scale-up when spare KV capacity < trigger
     - `QUEUE_SPARE_TRIGGER` (default 3): Proactive scale-up when spare queue capacity < trigger
   - **Multi-variant**: Variant cost (default `"10.0"` — override via `--set va.variantCost="<value>"` post-deploy)

3. **Must ask if not detected:**
   - `MODEL_ID`, `ACCELERATOR_TYPE`, scale-to-zero requirement

### 3. Deploy

Deploy WVA controller + VariantAutoscaling + HPA all at once, with the user's configuration baked in from the start.

**Prerequisites**:
- Go must be installed on the system
- kubectl configured to access your cluster
- For OpenShift: `oc` CLI installed

**CRITICAL**: Set `DEPLOY_VA=true DEPLOY_HPA=true` so the chart creates the scaling resources at deploy time.

```bash
cd ${WVA_REPO_PATH}

make deploy-wva-on-k8s \
  IMG=ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest \
  WVA_NS=<target-namespace> \
  NAMESPACE_SCOPED=true \
  DEPLOY_LLM_D=false \
  DEPLOY_VA=true \
  DEPLOY_HPA=true \
  MODEL_ID="<model-id>" \
  ACCELERATOR_TYPE=<accelerator> \
  KV_CACHE_THRESHOLD=<kv_threshold> \
  QUEUE_LENGTH_THRESHOLD=<queue_threshold> \
  KV_SPARE_TRIGGER=<kv_spare> \
  QUEUE_SPARE_TRIGGER=<queue_spare> \
  HPA_MIN_REPLICAS=<min_replicas> \
  HPA_STABILIZATION_SECONDS=<stabilization_seconds> \
  SCALER_BACKEND=<prometheus-adapter|keda>
```

**Example — aggressive scaling on H100, user chose HPA:**
```bash
cd ${WVA_REPO_PATH}

make deploy-wva-on-k8s \
  IMG=ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest \
  WVA_NS=my-llm-namespace \
  NAMESPACE_SCOPED=true \
  DEPLOY_LLM_D=false \
  DEPLOY_VA=true \
  DEPLOY_HPA=true \
  MODEL_ID="Qwen/Qwen3-32B" \
  ACCELERATOR_TYPE=H100 \
  KV_CACHE_THRESHOLD=0.70 \
  QUEUE_LENGTH_THRESHOLD=3 \
  KV_SPARE_TRIGGER=0.15 \
  QUEUE_SPARE_TRIGGER=2 \
  HPA_MIN_REPLICAS=1 \
  HPA_STABILIZATION_SECONDS=60 \
  SCALER_BACKEND=prometheus-adapter
```

**Same example with KEDA:** replace the last line with `SCALER_BACKEND=keda`

**What gets deployed:**
- WVA controller (namespace-scoped, watches only `WVA_NS`)
- VariantAutoscaling resource for the model
- HPA or ScaledObject (depending on `SCALER_BACKEND`) with user-specified thresholds
- Saturation ConfigMap with custom thresholds
- Prometheus Adapter (if `SCALER_BACKEND=prometheus-adapter`) or KEDA (if `SCALER_BACKEND=keda`)
- ServiceMonitor for WVA metrics

**Non-default `maxReplicas` or `variantCost`**: the Makefile has no dedicated vars for these. Override them with a `helm upgrade` immediately after the deploy:
```bash
helm upgrade workload-variant-autoscaler ${WVA_REPO_PATH}/charts/workload-variant-autoscaler \
  -n <target-namespace> \
  --reuse-values \
  --set hpa.maxReplicas=10 \
  --set va.variantCost="70"
```

> The Makefile targets are thin wrappers around `deploy/install.sh` — no need to call it directly.

### 4. Verify

#### Metrics Infrastructure

If the llm-d deployment doesn't already expose Prometheus metrics (e.g., it was deployed outside the standard llm-d guide), ensure the metrics pipeline exists:

```bash
# Check if ServiceMonitor exists for the deployment
kubectl get servicemonitor -n <namespace> | grep <deployment-name>

# If missing, create Service + ServiceMonitor for vLLM metrics
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
---
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

#### WVA + VA + HPA

```bash
# Check controller is running in target namespace
kubectl get deployment -n <target-namespace> -l app.kubernetes.io/name=workload-variant-autoscaler

# Verify namespace-scoping is correct
kubectl logs -n <target-namespace> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "Watching"
# Should show: "Watching single namespace: <target-namespace>"

# Check VA and HPA were created
kubectl get variantautoscaling,hpa -n <target-namespace>

# Verify WVA detected the VariantAutoscaling
kubectl logs -n <target-namespace> \
  -l app.kubernetes.io/name=workload-variant-autoscaler | grep "VariantAutoscaling"
# Should NOT show "No active VariantAutoscalings found"
```

#### Metrics Ready

```bash
# Check METRICSREADY status (wait 2 minutes for Prometheus scrape)
sleep 120
kubectl get variantautoscaling -n <target-namespace>
# Should show METRICSREADY: True

# Verify metrics infrastructure
kubectl get service,servicemonitor -n <target-namespace> | grep metrics
```

#### Verification Scripts

```bash
./scripts/verify-wva.sh <namespace>                    # Status, HPA, logs, metrics
./scripts/troubleshoot-metrics.sh <namespace> <pod>    # Metrics diagnostics
./scripts/troubleshoot-scaling.sh <namespace>          # Scaling diagnostics
```

#### Success Criteria

A successful WVA deployment should show:
- ✅ WVA controller logs show "VariantAutoscaling" detected (not "No active VariantAutoscalings found")
- ✅ METRICSREADY: True within 2 minutes
- ✅ HPA shows valid metrics (not `<unknown>`)
- ✅ Deployment scales up under load
- ✅ Deployment scales down after stabilization window
- ✅ No error messages in WVA controller logs

See [`Troubleshooting.md`](./Troubleshooting.md) for detailed solutions to common issues.

### 5. Optional Load Testing

**IMPORTANT**: After successful deployment and verification, **ASK THE USER** if they want to test WVA autoscaling with load.

#### When to Test
- User wants to validate WVA is working correctly
- User wants to see scaling in action
- User wants to tune thresholds based on observed behavior

#### Using test-wva-scaling.sh

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

#### Expected Behavior

**During load:**
- KV cache usage should increase (visible in vLLM metrics)
- Queue depth may increase if load exceeds capacity
- WVA should detect saturation and recommend scale-up
- HPA should scale deployment up after the scale-up stabilization window you configured

**After load stops:**
- KV cache usage returns to low levels
- Queue depth returns to 0
- WVA should recommend scale-down
- HPA should scale deployment down after the scale-down stabilization window you configured

#### Troubleshooting Test Results

**If no scale-up occurs:**
- Load was insufficient to trigger saturation → increase `num-requests` or use longer `max_tokens`
- Deployment already at maxReplicas → check and increase maxReplicas in VariantAutoscaling
- Stabilization window not elapsed → wait for your configured `HPA_STABILIZATION_SECONDS`
- WVA not monitoring correctly → run `./troubleshoot-scaling.sh <namespace>`

**If scale-up is too slow:**
- Lower `HPA_STABILIZATION_SECONDS` (redeploy with a smaller value)
- Lower saturation thresholds (e.g., `KV_CACHE_THRESHOLD=0.70`)

**If scale-down doesn't occur:**
- Wait longer: scale-down stabilization is typically 300s
- Check if new requests are still coming in
- Verify `scaleDownSafe: true` in WVA logs

For detailed troubleshooting, see [`Troubleshooting.md`](./Troubleshooting.md).

## Reference

### Configuration Files

**`scripts/configs/`** — Use these YAML files to **save and version-control** the configuration you've chosen. They are not used for deployment (the Makefile generates all resources via Helm), but saving your settings here lets you track what was configured, re-apply it later, or share it with your team.

### Makefile Variables

Run `make help` in `${WVA_REPO_PATH}` to view all 40+ available targets.

*Infrastructure (set once, rarely change):*

| Variable | Description | Default | Example |
|----------|-------------|---------|---------|
| `IMG` | WVA container image | `ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest` | Custom image tag |
| `WVA_NS` | Target namespace for WVA deployment | `workload-variant-autoscaler-system` | `my-namespace` |
| `NAMESPACE_SCOPED` | **CRITICAL**: Limit WVA to single namespace | `false` | **`true` (REQUIRED)** |
| `DEPLOY_LLM_D` | Deploy llm-d stack with WVA | `true` | `false` (set when llm-d already exists) |
| `DEPLOY_VA` | Deploy VariantAutoscaling resource via chart | `false` | **`true` (REQUIRED for single-step deploy)** |
| `DEPLOY_HPA` | Deploy HPA resource via chart | `false` | **`true` (REQUIRED for single-step deploy)** |
| `ENVIRONMENT` | Platform type | `kubernetes` | `openshift`, `kind-emulator` |

*User configuration (tuned per model and scaling policy):*

| Variable | Description | Default | Example |
|----------|-------------|---------|---------|
| `MODEL_ID` | Model identifier for VA resource | `unsloth/Meta-Llama-3.1-8B` | `Qwen/Qwen3-32B` |
| `ACCELERATOR_TYPE` | GPU accelerator type | `H100` | `A100`, `L40S` |
| `KV_CACHE_THRESHOLD` | KV cache saturation threshold (0.0–1.0) | `0.80` | `0.70` (aggressive) |
| `QUEUE_LENGTH_THRESHOLD` | Queue depth saturation threshold | `5` | `3` (aggressive) |
| `KV_SPARE_TRIGGER` | Proactive scale-up spare KV trigger | `0.10` | `0.15` |
| `QUEUE_SPARE_TRIGGER` | Proactive scale-up spare queue trigger | `3` | `2` |
| `HPA_MIN_REPLICAS` | Minimum replicas | `1` | `0` (scale-to-zero) |
| `HPA_STABILIZATION_SECONDS` | Scale-up and scale-down stabilization window | `240` | `60` (fast), `300` (conservative) |
| `SCALER_BACKEND` | Scaler backend type | `prometheus-adapter` | `keda`, `none` |

*Multi-model only:*

| Variable | Description | Default | Example |
|----------|-------------|---------|---------|
| `MODELS` | Comma-separated model list | `unsloth/Meta-Llama-3.1-8B` | `model1,model2` |

Other targets for specific scenarios:
```bash
# Kind (local testing with emulated GPUs)
make deploy-wva-emulated-on-kind NAMESPACE_SCOPED=true

# Full e2e infrastructure (WVA + llm-d + monitoring, no VA/HPA — for e2e test suites)
make deploy-e2e-infra ENVIRONMENT=kubernetes NAMESPACE_SCOPED=true

# Multi-model deployment (one WVA per model)
make deploy-multi-model-infra \
  ENVIRONMENT=kubernetes \
  MODELS="Qwen/Qwen3-0.6B,unsloth/Meta-Llama-3.1-8B" \
  WVA_NS=my-target-namespace \
  NAMESPACE_SCOPED=true
```

### Parameter → Resource Mapping

Each Makefile variable lands in a specific Kubernetes resource. Understanding this helps with troubleshooting and post-deploy tuning.

**ConfigMap `wva-saturation-scaling-config`** — tells WVA *when* to recommend a replica change:

| Makefile var | ConfigMap field | Meaning |
|---|---|---|
| `KV_CACHE_THRESHOLD` | `kvCacheThreshold` | Mark replica as saturated when KV cache ≥ this |
| `QUEUE_LENGTH_THRESHOLD` | `queueLengthThreshold` | Mark replica as saturated when queue depth ≥ this |
| `KV_SPARE_TRIGGER` | `kvSpareTrigger` | Proactively scale up when spare KV capacity < this |
| `QUEUE_SPARE_TRIGGER` | `queueSpareTrigger` | Proactively scale up when spare queue < this |

**HPA / ScaledObject** — actually *executes* the scaling:

| Source | HPA field | Meaning |
|---|---|---|
| `HPA_MIN_REPLICAS` | `spec.minReplicas` | Floor |
| `--set hpa.maxReplicas` (helm) | `spec.maxReplicas` | Ceiling |
| `HPA_STABILIZATION_SECONDS` | `behavior.scaleUp/Down.stabilizationWindowSeconds` | Both windows — symmetric by default |

> **Asymmetric stabilization**: `HPA_STABILIZATION_SECONDS` sets both scale-up and scale-down to the same value. For different windows (e.g., fast scale-up / slow scale-down), do a `helm upgrade` after the initial deploy to override them independently — see [example3](scripts/configs/example.yaml) for the HPA YAML structure.

**VariantAutoscaling** — drives multi-variant *priority ordering*:

| Source | VA field | Meaning |
|---|---|---|
| `ACCELERATOR_TYPE` | `inference.optimization/acceleratorName` label | GPU vendor (nvidia / amd / cpu) |
| `MODEL_ID` | `spec.modelID` | Model this VA governs |
| `--set va.variantCost` (helm) | `spec.variantCost` | Relative cost — WVA scales cheapest variant first |

### Configuration Rules

**Always applies (Makefile or manual):**
- **CRITICAL**: HPA cannot scale from 0 replicas
  - If deployment is at 0 replicas, manually scale to 1 first: `kubectl scale deployment <name> --replicas=1`
  - HPA will then manage scaling from 1 to maxReplicas
  - For true scale-to-zero, use KEDA (`SCALER_BACKEND=keda`)
- **CRITICAL**: `variantCost` must be a STRING when overriding via helm (e.g., `--set va.variantCost="100"` not `100`)
- Multi-variant: WVA scales cheaper variants first based on `variantCost`
- Align thresholds with Inference Scheduler (EPP) — see [EPP Threshold Alignment](#epp-threshold-alignment)

**Manual YAML only** (Makefile + `DEPLOY_VA=true DEPLOY_HPA=true` handles these automatically):
- Include `inference.optimization/acceleratorName` label on VariantAutoscaling (e.g., `nvidia`, `amd`, `cpu`) — without it, METRICSREADY stays False
- HPA selector must match BOTH labels: `variant_name` (= VA resource name) + `exported_namespace` (= deployment namespace)
- API version must be `llmd.ai/v1alpha1` (NOT `inference.llmd.ai/v1alpha1`)
- `scaleTargetRef` must include `apiVersion: apps/v1` — without it WVA fails with "no matches for kind Deployment in version \"\""
- VariantAutoscaling spec must NOT include `metrics` — only: `scaleTargetRef`, `modelID`, `variantCost`, `minReplicas`, `maxReplicas`

**Troubleshooting: if METRICSREADY stays False**, verify the acceleratorName label exists:
```bash
kubectl get variantautoscaling <name> -n <namespace> -o jsonpath='{.metadata.labels}'
```

### EPP Threshold Alignment

**Critical**: WVA and EPP must use identical thresholds. One EPP instance per model (shared across variants of same model).

**Threshold mapping:**
- WVA `kvCacheThreshold` = EPP `kvCacheUtilThreshold`
- WVA `queueLengthThreshold` = EPP `queueDepthThreshold`

**To align:**
1. Update WVA ConfigMap `wva-saturation-scaling-config` (auto-reloads)
2. Update EPP config
3. Restart EPP: `kubectl rollout restart deployment/gaie-<model-name>-epp -n <namespace>`

### Undeploy

```bash
cd ${WVA_REPO_PATH}

# Undeploy from Kubernetes
make undeploy-wva-on-k8s \
  WVA_NS=my-target-namespace

# Undeploy from OpenShift
make undeploy-wva-on-openshift \
  WVA_NS=my-target-namespace

# Undeploy from Kind
make undeploy-wva-emulated-on-kind

# Undeploy multi-model infrastructure
make undeploy-multi-model-infra \
  MODELS="model1,model2"
```

### Upgrade

```bash
cd ${WVA_REPO_PATH}

# 1. Pull latest changes
git pull origin main

# 2. Apply updated CRDs first
kubectl apply -f config/crd/bases/

# 3. Redeploy WVA with new image
make deploy-wva-on-k8s \
  IMG=ghcr.io/llm-d/llm-d-workload-variant-autoscaler:v0.6.0 \
  WVA_NS=<target-namespace> \
  NAMESPACE_SCOPED=true \
  DEPLOY_LLM_D=false
```

**Breaking change v0.5.1**: `scaleTargetRef` now required. Update existing VAs:
```yaml
spec:
  scaleTargetRef:
    apiVersion: apps/v1
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
- **Configuration Examples**: `guides/workload-autoscaling/`

**Benchmark Templates**: `deployments/*/benchmark-templates/` - See run-llm-d-benchmark skill

## Best Practices

1. **Start with defaults**: Use default thresholds initially, tune based on observed behavior
2. **Align thresholds**: Keep WVA and EPP thresholds synchronized
3. **Monitor first**: Observe saturation patterns before aggressive tuning
4. **Stabilization windows**: Use longer windows (120s+ scale-up, 300s+ scale-down) to prevent flapping
5. **Test with load**: Use llm-d-benchmark to validate scaling behavior under realistic load
6. **Cost optimization**: For multi-variant setups, set variantCost accurately to reflect actual costs
7. **Scale-to-zero**: Only enable in dev/test environments, not production (cold start latency)

## Online Resources

- **WVA**: https://github.com/llm-d/llm-d-workload-variant-autoscaler
- **llm-d**: https://github.com/llm-d/llm-d
- **llm-d-benchmark**: https://github.com/llm-d/llm-d-benchmark

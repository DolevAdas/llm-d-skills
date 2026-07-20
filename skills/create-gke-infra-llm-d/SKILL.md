---
name: create-gke-infra-llm-d
description: Create or prepare GKE infrastructure (cluster, GPU node pools, RDMA/RoCE networking, DRA and DRANET drivers, Gateway API prerequisites) so that an llm-d stack can be deployed on it. Use this skill when users want to provision a new GKE cluster for llm-d, or make an existing GKE cluster llm-d-ready, especially for prefill/decode disaggregation or wide expert parallelism which require RDMA.
---

# Create GKE Infrastructure for llm-d

## 📋 Command Execution Notice

**Before executing any command, I will:**

1. **Explain what the command does** - a clear description of the command's purpose and expected outcome
2. **Show the actual command** - the exact command that will be executed
3. **Explain why it's needed** - how this command fits into the overall provisioning workflow

> ## 🔔 ALWAYS NOTIFY THE USER BEFORE CREATING ANYTHING
>
> **RULE**: Before creating ANY cloud resource - VPCs, subnets, firewall rules, clusters, node pools, DaemonSets, Helm releases, or any Kubernetes object - you MUST first tell the user what you are about to create, in which project/region, and why.
>
> GPU node pools (A3 Ultra, A4) are **expensive**. Always confirm machine type, node count, and consumption model (reservation, Spot, flex-start) with the user before creating a node pool. Never guess the GCP project or region - ask if not provided.

**Scope of this skill.** This skill owns everything up to "the cluster is llm-d-ready": cluster, node pools, RDMA networking, DRA drivers, and Gateway API prerequisites. It is the one skill in this repository that is *expected* to create cluster-level and cloud-level resources. It does NOT deploy llm-d itself - when the cluster passes validation, hand off to [`deploy-llm-d`](../deploy-llm-d/) (config already decided) or the [autoconfig skill](../../autoconfig/skill/llm-d-autoconfig/) (config to be recommended).

## Sources of Truth

Do not improvise commands. Every step in this skill is anchored in:

* llm-d GKE infrastructure guide: `${LLMD_PATH}/docs/infrastructure/providers/gke/README.md`
* llm-d RDMA and networking guide: `${LLMD_PATH}/docs/infrastructure/rdma/README.md`
* llm-d GKE gateway guide: `${LLMD_PATH}/docs/infrastructure/gateway/gke.md`
* GCP: [AI Hypercomputer custom GKE cluster](https://docs.cloud.google.com/ai-hypercomputer/docs/create/gke-ai-hypercompute-custom)
* GCP: [Set up GPU Dynamic Resource Allocation (DRA)](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/set-up-dra)
* GCP: [Allocate network resources using GKE managed DRANET](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/allocate-network-resources-dra)
* GCP: [Deploying Gateways](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/deploying-gateways)

If `LLMD_PATH` is set (or an llm-d clone is found nearby), prefer reading the local docs above for the current state of the recipes; the GCP links are authoritative for gcloud flags and may have been updated since the llm-d docs were written.

## Prerequisites

* `gcloud` CLI authenticated with permissions to create networks, clusters, and node pools in the target project
* `kubectl` and `helm` installed
* Tested configurations (from the llm-d GKE guide): machine types A3, A4, ct5p, ct5lp, ct6e; GKE 1.33.4+
* Version floors from GCP docs: managed DRANET needs GKE Standard 1.34.1-gke.1829001+; GPU DRA setup targets GKE 1.35+; RDMA needs 1.32.2-gke.1475000+ (A4) or 1.31.4-gke.1183000+ (A3 Ultra); nodes must run Container-Optimized OS

## Step 1: Establish the Target and Choose a Networking Path

Ask the user (or detect) three things before creating anything:

1. **Which llm-d guide is the cluster for?** The target guide determines the networking path:

   | Target guide | Networking path | Reference |
   |---|---|---|
   | `pd-disaggregation` (GKE overlay, DRA-based) | **Path A: DRA + managed DRANET** | [references/dra-dranet-setup.md](references/dra-dranet-setup.md) |
   | `wide-ep-lws` (GKE overlay, multi-NIC annotations) | **Path B: multi-networking + gIB** | [references/multinet-rdma-setup.md](references/multinet-rdma-setup.md) |
   | Aggregated serving only (e.g. `optimized-baseline`), no RDMA needed | Neither - a standard GPU cluster suffices; only Step 3 (gateway) and Step 4 (validation) apply | - |

   Path A is what the pd-disaggregation guide's `modelserver/gpu/vllm/gke` overlay consumes: a `ResourceClaimTemplate` requesting `gpu.nvidia.com` + `mrdma.google.com` devices aligned on the same PCIe root. Path B is what the wide-ep-lws `modelserver/gpu/vllm/gke` overlay consumes: `networking.gke.io/interfaces` pod annotations (`rdma-0` through `rdma-7`), gIB host mounts, and `set_nccl_env.sh`.

   > [!WARNING]
   > Per the GCP DRANET documentation: do not combine the DRANET driver with the multi-network API's `Device`-type `Network` resources in one cluster - both manage the same NICs and cause an incorrect setup and unpredictable behavior. Pick ONE path per cluster. If the user needs both guides, they need either two clusters or a deliberate decision to run both guides on the same path (verify current guide support before promising this).

2. **New cluster or existing cluster?** Run `gcloud container clusters list --project=${PROJECT}` and ask. For an existing cluster, the deciding factor is **Dataplane V2**, which can only be enabled at cluster creation:
   * **Existing cluster WITH Dataplane V2**: skip cluster creation; go straight to node pool creation in the chosen path.
   * **Existing cluster WITHOUT Dataplane V2**: managed DRANET is not supported (llm-d GKE guide). Options: (a) create a new cluster (recommended for Path A), or (b) Path B with manually created subnets and `--additional-node-network` flags, which does not strictly require DRANET.

3. **Capacity model.** A3 Ultra / A4 capacity is usually reservation-bound. Ask which of: specific reservation (`--reservation-affinity=specific --reservation=...`), Spot (`--spot`), or flex-start. Record the choice; it changes the node pool command.

Set the shared environment variables (from the llm-d GKE guide):

```bash
export PROJECT="<your GCP project>"
export LOCATION="<your GCP location>"
export CLUSTER_NAME="<your cluster name>"
```

## Step 2: Execute the Chosen Path

* **Path A (DRA + managed DRANET)**: follow [references/dra-dranet-setup.md](references/dra-dranet-setup.md). Summary: create cluster with `--enable-dataplane-v2`; create node pool with `gpu-driver-version=disabled`, `--accelerator-network-profile=auto`, and the DRA/DRANET node labels; install the COS NVIDIA driver DaemonSet and the NVIDIA GPU DRA driver Helm chart; verify `ResourceSlices` show `gpu.nvidia.com` and `mrdma.google.com` devices.

* **Path B (multi-networking + gIB)**: follow [references/multinet-rdma-setup.md](references/multinet-rdma-setup.md). Summary: create the gVNIC VPC and the RoCE-profile RDMA VPC with 8 subnets plus firewall rule; create cluster with `--enable-dataplane-v2 --enable-ip-alias --enable-multi-networking`; create node pool with nine `--additional-node-network` flags; apply the `Network`/`GKENetworkParamSet` CRs; install the gIB/NCCL RDMA installer DaemonSet.

## Step 3: Gateway API Prerequisites (Optional)

Only needed if the user will run llm-d in Gateway mode with a GKE-managed GatewayClass (`gke-l7-rilb` or `gke-l7-regional-external-managed`). Follow [references/gateway-prereqs.md](references/gateway-prereqs.md): enable the Gateway API on the cluster and create a proxy-only subnet in the cluster's region. Standalone mode (the llm-d guides' default) needs none of this - skip cleanly.

## Step 4: Validate llm-d Readiness

Run the checks in [references/validation.md](references/validation.md) for the chosen path. Do not declare the infrastructure ready until the path-specific checks pass. Report the results to the user as a checklist.

## Step 5: Generate a Provisioning Script and Hand Off

**After successful validation, ALWAYS generate a reusable provisioning script** containing the exact commands executed (actual values, not placeholders), with `set -e`, prerequisite checks, and comments. Name it `provision-gke-YYYYMMDD.sh` and tell the user where it is saved.

Then hand off:

* Config already decided: continue with [`deploy-llm-d`](../deploy-llm-d/), which deploys onto this cluster using the Well-Lit Path guides.
* Config to be recommended from workload + SLA: continue with the [autoconfig skill](../../autoconfig/skill/llm-d-autoconfig/). Note for Path A clusters: autoconfig's Phase 1 RDMA sweep was written for the multi-networking model (Path B signals: `Network` CRs, `networking.gke.io` node resources). A Path A cluster surfaces RDMA through `ResourceSlices`/`DeviceClasses` instead, so tell the user autoconfig may report "RDMA-capable: no" even though the pd-disaggregation DRA path will work.

## Known Issues to Warn About (from the llm-d GKE guide)

These bite at *deploy* time but are caused by *infrastructure* choices, so surface them at hand-off:

* **gIB 1.10+ required for vLLM 0.11.0+** (NCCL 2.27). Use at least the RDMA installer DaemonSet version from [container-engine-accelerators PR #511](https://github.com/GoogleCloudPlatform/container-engine-accelerators/pull/511). Path B only.
* **NCCL tuner crash when gIB is installed**: engines fail with `NCCL_TUNER_CONFIG_PATH` errors unless `NCCL_TUNER_PLUGIN=none` and `NCCL_NET_PLUGIN=""` are set. The llm-d GKE overlays apply this automatically via the `disable-gke-nccl-tuner-patch` component; only manual deployments need to add it.
* **`LD_LIBRARY_PATH` must include `/usr/local/nvidia/lib64`** in custom vLLM images, or vLLM fails with `Failed to infer device type` (llm-d images already carry the fix).
* **DeepEP / NVSHMEM (wide-ep) needs GPU-initiated RDMA**: pods must run `privileged: true`, or the node needs `PeerMappingOverride=1` via a manual GPU driver installation. Set `NVSHMEM_DISABLE_GDRCOPY=1` per the llm-d guide.
* **All-to-all RDMA connectivity (wide-ep)**: every NIC on a host must reach every NIC on all other hosts; rail-only connectivity fails DeepEP.
* **Topology-aware placement**: RDMA-enabled nodes carry `cloud.google.com/gce-topology-block` / `gce-topology-subblock` labels; the llm-d GKE overlays add pod affinity on these. For multi-host replicas at scale, GKE recommends Topology Aware Scheduling with Kueue and LeaderWorkerSet.

## Teardown

This skill does not delete infrastructure automatically. If the user asks, the reverse order is: node pool, cluster, `Network`/`GKENetworkParamSet` CRs (deleted with the cluster), then VPC subnets, firewall rules, and VPCs created by this skill. Always list exactly what will be deleted and get confirmation first; reservations are not touched.

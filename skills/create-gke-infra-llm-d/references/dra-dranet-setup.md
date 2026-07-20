# Path A: GPU DRA + Managed DRANET (RoCE) on GKE

This is the path consumed by the pd-disaggregation guide's GKE overlay
(`${LLMD_PATH}/guides/pd-disaggregation/modelserver/gpu/vllm/gke`), which attaches a
`ResourceClaimTemplate` requesting `gpu.nvidia.com` and `mrdma.google.com` devices
constrained to the same PCIe root.

Sources:

* `${LLMD_PATH}/docs/infrastructure/providers/gke/README.md`, section "GPU Dynamic Resource Allocation (DRA) and DRANET (RoCE) on GKE"
* GCP: [Set up GPU DRA](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/set-up-dra)
* GCP: [GKE managed DRANET](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/allocate-network-resources-dra)
* GCP: [AI Hypercomputer custom GKE cluster](https://docs.cloud.google.com/ai-hypercomputer/docs/create/gke-ai-hypercompute-custom)

The llm-d guide notes: GPU DRA is not yet fully managed by GKE and requires manual node
label configuration and driver installation. Managed DRANET requires support for both
Hairpin (same-node loopback) and Cross-rail (inter-node multi-rail) routing for proper
KV cache exchange between prefill and decode nodes. The current recipe targets the
GKE A3/A4 platform.

## Version requirements

* Managed DRANET: GKE Standard 1.34.1-gke.1829001+ (Autopilot 1.35.2-gke.1842000+)
* GPU DRA setup doc targets GKE Standard 1.35+
* Dataplane V2 is mandatory and can only be set at cluster creation

## Step 1: Create the cluster (skip if reusing a DPv2 cluster)

From the llm-d GKE guide - managed DRANET needs no custom networking drivers, but
Dataplane V2 must be enabled at creation:

```bash
gcloud container clusters create "${CLUSTER_NAME}" \
    --enable-dataplane-v2 \
    --location="${LOCATION}" \
    --project="${PROJECT}"
```

Existing-cluster rules (from the same guide):

* Existing cluster WITH Dataplane V2: skip this step, go to Step 2 using
  `--accelerator-network-profile=auto`.
* Existing cluster WITHOUT Dataplane V2: GKE-managed automated DRANET is NOT supported.
  Either create a new cluster, or fall back to manual multi-networking
  (see [multinet-rdma-setup.md](multinet-rdma-setup.md)).

## Step 2: Create the node pool (DRANET enabled, default GPU stack disabled)

From the llm-d GKE guide. The example targets A3 Ultra (8x H200, Spot); adjust
machine type, accelerator type/count, node count, and the capacity flags
(reservation vs `--spot` vs flex-start) per the user's Step 1 answers:

```bash
gcloud beta container node-pools create a3u-dra-pool-1 \
  --project="${PROJECT}" \
  --location="${LOCATION}" \
  --cluster="${CLUSTER_NAME}" \
  --accelerator type=nvidia-h200-141gb,count=8,gpu-driver-version=disabled \
  --machine-type=a3-ultragpu-8g \
  --num-nodes=2 \
  --spot \
  --accelerator-network-profile=auto \
  --node-labels="cloud.google.com/gke-networking-dra-driver=true,goog-gke-accelerator-type=nvidia-h200-141gb,nvidia.com/gpu.present=true,cloud.google.com/gke-nvidia-gpu-dra-driver=true,gke-no-default-nvidia-gpu-device-plugin=true"
```

Why each non-obvious piece (per the GCP DRA and DRANET docs):

| Flag / label | Purpose |
|---|---|
| `gpu-driver-version=disabled` | GPU DRA requires disabling GKE's automated GPU driver install |
| `gke-no-default-nvidia-gpu-device-plugin=true` | Disables the default GPU device plugin (DRA replaces it) |
| `nvidia.com/gpu.present=true` | Lets the NVIDIA DRA driver DaemonSet schedule onto the nodes |
| `cloud.google.com/gke-nvidia-gpu-dra-driver=true` | Cluster autoscaler support for the GPU DRA driver |
| `--accelerator-network-profile=auto` | GKE auto-configures VPC networks/subnets for the accelerator VMs (managed DRANET) |
| `cloud.google.com/gke-networking-dra-driver=true` | Enables the GKE networking DRA (DRANET) driver on the nodes |

For A4 (B200), the AI Hypercomputer doc uses `--machine-type=a4-highgpu-8g` with
`--accelerator type=nvidia-b200,count=8,...`. For reservation-bound capacity, replace
`--spot` with:

```bash
  --reservation-affinity=specific \
  --reservation=projects/RESERVATION_PROJECT/reservations/RESERVATION_NAME/reservationBlocks/RESERVATION_BLOCK
```

## Step 3: Install the NVIDIA driver and the GPU DRA driver

Automated GPU driver management was disabled, so both must be installed manually
(from the llm-d GKE guide).

### 3.1 NVIDIA driver (preloaded COS DaemonSet)

```bash
kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/master/nvidia-driver-installer/cos/daemonset-preloaded.yaml
```

### 3.2 NVIDIA GPU DRA driver (Helm)

If the `nvidia` Helm repo is not present yet (from the GCP DRA doc):

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia && helm repo update
```

Then install (version 25.8.0 or later):

```bash
helm install nvidia-dra-driver-gpu nvidia/nvidia-dra-driver-gpu \
    --version="25.8.0" --create-namespace --namespace=nvidia-dra-driver-gpu \
    --set nvidiaDriverRoot="/home/kubernetes/bin/nvidia/" \
    --set gpuResourcesEnabledOverride=true \
    --set resources.computeDomains.enabled=false \
    --set kubeletPlugin.priorityClassName="" \
    --set 'kubeletPlugin.tolerations[0].key=nvidia.com/gpu' \
    --set 'kubeletPlugin.tolerations[0].operator=Exists' \
    --set 'kubeletPlugin.tolerations[0].effect=NoSchedule'
```

Note (GCP DRA doc): `nvidiaDriverRoot` is `/home/kubernetes/bin/nvidia/` on COS;
on Ubuntu nodes it is `/opt/nvidia`.

## Step 4: Verify

See [validation.md](validation.md), Path A section. The short version:

```bash
kubectl get pods -n nvidia-dra-driver-gpu          # kubelet-plugin pods Running
kubectl get deviceclass mrdma.google.com           # DRANET DeviceClass exists
kubectl get resourceslices -o yaml                 # devices from gpu.nvidia.com AND mrdma.google.com
```

## What the workload consumes (context, not action)

The pd-disaggregation GKE overlay applies this template
(`guides/pd-disaggregation/modelserver/components/gke-rdma/gke-rdma-template.yaml`),
which is why both drivers must be publishing devices with matching `pcieRoot` attributes:

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: gke-rdma-template
spec:
  spec:
    devices:
      requests:
      - name: gpu
        exactly:
          deviceClassName: gpu.nvidia.com
          count: 1
      - name: nic
        exactly:
          deviceClassName: mrdma.google.com
          count: 1
      constraints:
      - matchAttribute: "resource.kubernetes.io/pcieRoot"
```

The overlay also sets `UCX_IB_ROCE_REACHABILITY_MODE=all`, adds `nvidia.com/gpu`
tolerations, removes the classic `nvidia.com/gpu` resource requests (DRA claims replace
them), and pulls in the `disable-gke-nccl-tuner-patch` component. Nothing to do here at
infra time; this is what deploy-time consumes.

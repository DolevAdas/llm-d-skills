# Path B: Multi-Networking + gIB (GPUDirect RDMA) on GKE

This is the path consumed by the wide-ep-lws guide's GKE overlay
(`${LLMD_PATH}/guides/wide-ep-lws/modelserver/gpu/vllm/gke`), which uses
`networking.gke.io/interfaces` pod annotations (`eth2`-`eth9` mapped to `rdma-0` through
`rdma-7`), gIB host mounts (`/usr/local/gib`), and `set_nccl_env.sh`.

Sources:

* GCP: [AI Hypercomputer custom GKE cluster](https://docs.cloud.google.com/ai-hypercomputer/docs/create/gke-ai-hypercompute-custom) (all commands below)
* `${LLMD_PATH}/docs/infrastructure/providers/gke/README.md` (A3/A4 guidance, gIB versions, workload configuration)
* `${LLMD_PATH}/docs/infrastructure/rdma/README.md` (GKE platform notes)

> [!NOTE]
> IAM: this path issues `gcloud compute networks|subnets|firewall-rules create` commands
> and therefore needs `roles/compute.networkAdmin` and `roles/compute.securityAdmin` in
> addition to the cluster roles. See "Required IAM Roles" in SKILL.md and run the
> preflight before starting.

> [!WARNING]
> Do not enable managed DRANET (`cloud.google.com/gke-networking-dra-driver=true` /
> `--accelerator-network-profile=auto`) on a cluster using this path. Per the GCP DRANET
> doc, the DRANET driver and `Device`-type `Network` resources manage the same NICs and
> must not be combined in one cluster.

## Step 0: Environment

From the AI Hypercomputer doc. Prefix values: `a3ultra-gvnic`/`a3ultra-rdma` for A3 Ultra,
`a4high-gvnic`/`a4high-rdma` for A4:

```bash
export REGION="<compute region>"
export ZONE="<compute zone>"
export PROJECT="<project id>"
export GVNIC_NETWORK_PREFIX="a3ultra-gvnic"
export RDMA_NETWORK_PREFIX="a3ultra-rdma"
```

## Step 1: Create VPCs, subnets, and firewall rule

Create a VPC for the additional Google Titanium CPU NIC:

```bash
gcloud compute --project=${PROJECT} \
  networks create \
  ${GVNIC_NETWORK_PREFIX}-net \
  --subnet-mode=custom

gcloud compute --project=${PROJECT} \
  networks subnets create \
  ${GVNIC_NETWORK_PREFIX}-sub \
  --network=${GVNIC_NETWORK_PREFIX}-net \
  --region=${REGION} \
  --range=192.168.0.0/24

gcloud compute --project=${PROJECT} \
  firewall-rules create \
  ${GVNIC_NETWORK_PREFIX}-internal \
  --network=${GVNIC_NETWORK_PREFIX}-net \
  --action=ALLOW \
  --rules=tcp:0-65535,udp:0-65535,icmp \
  --source-ranges=192.168.0.0/16
```

Create the HPC VPC for the RDMA NICs with the zone-specific RoCE network profile,
plus 8 subnets (one per RDMA NIC):

```bash
gcloud beta compute --project=${PROJECT} \
  networks create ${RDMA_NETWORK_PREFIX}-net \
  --network-profile=${ZONE}-vpc-roce \
  --subnet-mode=custom

for N in $(seq 0 7); do
  gcloud compute --project=${PROJECT} \
    networks subnets create \
    ${RDMA_NETWORK_PREFIX}-sub-$N \
    --network=${RDMA_NETWORK_PREFIX}-net \
    --region=${REGION} \
    --range=192.168.$((N+1)).0/24 &
done
wait
```

## Step 2: Create the cluster (multi-networking enabled)

```bash
gcloud container clusters create ${CLUSTER_NAME} \
  --region=${REGION} \
  --cluster-version=<CLUSTER_VERSION> \
  --enable-dataplane-v2 --enable-ip-alias --enable-multi-networking
```

Minimum versions for RDMA (AI Hypercomputer doc): 1.32.2-gke.1475000+ for A4,
1.31.4-gke.1183000+ for A3 Ultra. Nodes must run Container-Optimized OS.

## Step 3: Create the node pool with additional node networks

One `--additional-node-network` for the gVNIC net and one per RDMA subnet.
`GPU_TYPE`/`MACHINE_TYPE`: `nvidia-h200-141gb`/`a3-ultragpu-8g` (A3 Ultra) or
`nvidia-b200`/`a4-highgpu-8g` (A4); count 8. Reservation-bound example:

```bash
gcloud container node-pools create ${NODE_POOL_NAME} \
  --region ${REGION} --cluster ${CLUSTER_NAME} \
  --node-locations ${ZONE} \
  --accelerator type=<GPU_TYPE>,count=8,gpu-driver-version=<DRIVER_VERSION> \
  --machine-type <MACHINE_TYPE> \
  --num-nodes=<NUM_NODES> \
  --reservation-affinity=specific \
  --reservation=<RESERVATION_NAME>/reservationBlocks/<BLOCK_NAME> \
  --additional-node-network network=${GVNIC_NETWORK_PREFIX}-net,subnetwork=${GVNIC_NETWORK_PREFIX}-sub \
  --additional-node-network network=${RDMA_NETWORK_PREFIX}-net,subnetwork=${RDMA_NETWORK_PREFIX}-sub-0 \
  --additional-node-network network=${RDMA_NETWORK_PREFIX}-net,subnetwork=${RDMA_NETWORK_PREFIX}-sub-1 \
  --additional-node-network network=${RDMA_NETWORK_PREFIX}-net,subnetwork=${RDMA_NETWORK_PREFIX}-sub-2 \
  --additional-node-network network=${RDMA_NETWORK_PREFIX}-net,subnetwork=${RDMA_NETWORK_PREFIX}-sub-3 \
  --additional-node-network network=${RDMA_NETWORK_PREFIX}-net,subnetwork=${RDMA_NETWORK_PREFIX}-sub-4 \
  --additional-node-network network=${RDMA_NETWORK_PREFIX}-net,subnetwork=${RDMA_NETWORK_PREFIX}-sub-5 \
  --additional-node-network network=${RDMA_NETWORK_PREFIX}-net,subnetwork=${RDMA_NETWORK_PREFIX}-sub-6 \
  --additional-node-network network=${RDMA_NETWORK_PREFIX}-net,subnetwork=${RDMA_NETWORK_PREFIX}-sub-7
```

Flex-start variant (AI Hypercomputer doc): replace the two reservation flags with
`--flex-start --enable-autoscaling --num-nodes=0 --total-max-nodes <N> --no-enable-autorepair --location-policy=ANY --reservation-affinity=none`
(optionally `--enable-queued-provisioning`).

Unlike Path A, `gpu-driver-version` here can be `default` or `latest` (GKE manages the
GPU driver; there is no DRA in this path).

Connect kubectl:

```bash
gcloud container clusters get-credentials ${CLUSTER_NAME} --location=${REGION}
```

## Step 4: Apply the GKE Network objects

One `GKENetworkParamSet` + `Network` pair for the gVNIC (`deviceMode: NetDevice`) and
eight pairs for RDMA (`deviceMode: RDMA`, names `rdma-0` through `rdma-7` - these names
are exactly what the wide-ep-lws GKE overlay's pod annotations reference):

```yaml
apiVersion: networking.gke.io/v1
kind: GKENetworkParamSet
metadata:
  name: gvnic-1
spec:
  vpc: ${GVNIC_NETWORK_PREFIX}-net
  vpcSubnet: ${GVNIC_NETWORK_PREFIX}-sub
  deviceMode: NetDevice
---
apiVersion: networking.gke.io/v1
kind: Network
metadata:
  name: gvnic-1
spec:
  type: "Device"
  parametersRef:
    group: networking.gke.io
    kind: GKENetworkParamSet
    name: gvnic-1
---
apiVersion: networking.gke.io/v1
kind: GKENetworkParamSet
metadata:
  name: rdma-0
spec:
  vpc: ${RDMA_NETWORK_PREFIX}-net
  vpcSubnet: ${RDMA_NETWORK_PREFIX}-sub-0
  deviceMode: RDMA
---
apiVersion: networking.gke.io/v1
kind: Network
metadata:
  name: rdma-0
spec:
  type: "Device"
  parametersRef:
    group: networking.gke.io
    kind: GKENetworkParamSet
    name: rdma-0
# ... repeat the rdma pair for rdma-1 through rdma-7,
# incrementing the vpcSubnet suffix to -sub-1 ... -sub-7
```

## Step 5: Install the RDMA binary and configure NCCL (gIB)

```bash
kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/refs/heads/master/gpudirect-rdma/nccl-rdma-installer.yaml
```

The DaemonSet places RDMA binaries in `/home/kubernetes/bin/gib` and NCCL in
`/home/kubernetes/bin/nvidia/lib64` on each node.

Version notes:

* llm-d GKE guide: vLLM 0.11.0+ requires NCCL 2.27, supported in gIB 1.10+. Use at least
  the installer DaemonSet version from
  [container-engine-accelerators PR #511](https://github.com/GoogleCloudPlatform/container-engine-accelerators/pull/511).
* AI Hypercomputer doc caveat: with gIB 1.0.6 or earlier, non-NCCL RDMA apps (e.g. NIXL)
  should install the `nccl-gib-plugins` or `nccl-gib` package inside the container
  instead of relying on the installer DaemonSet.

## Step 6: Verify

See [validation.md](validation.md), Path B section.

## What the workload consumes (context, not action)

The wide-ep-lws GKE overlay (see its README at
`guides/wide-ep-lws/modelserver/gpu/vllm/gke/README.md`) handles the pod-side wiring:
`networking.gke.io/default-interface`/`interfaces` annotations, `privileged: true` for
GPU-initiated RDMA, `DEEP_EP_DEVICE_TO_HCA_MAPPING`, `NVSHMEM_DISABLED_GDRCOPY`, gce
topology affinity, sourcing `/usr/local/gib/scripts/set_nccl_env.sh`, the
`disable-gke-nccl-tuner-patch` component, and the GKE Warden workaround (setting legacy
non-`.IP` `networking.gke.io.networks/rdma-X` limits to `0` because modern A3 Ultra
nodes only advertise the `.IP` variant). None of that is created at infra time, but the
network names (`rdma-0`..`rdma-7`) created in Step 4 must match it exactly.

Per the AI Hypercomputer doc, RDMA devices mount only into the first non-init container,
and a single pod must consume all GPUs and secondary NICs of a node (no RDMA sharing
across pods). GPUDirect RDMA is not compatible with NCCL Fast Socket or GPUDirect
TCPX/TCPXO.

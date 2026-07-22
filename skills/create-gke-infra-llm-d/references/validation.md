# Validating llm-d Readiness of GKE Infrastructure

Run the section matching the path that was provisioned, plus the common checks.
Report results to the user as a pass/fail checklist. Do not hand off to
deploy-llm-d/autoconfig until the relevant checks pass.

## Common checks (all paths)

```bash
# Nodes exist, are Ready, and expose the expected accelerator
kubectl get nodes -l cloud.google.com/gke-accelerator -o wide

# Topology labels used by the llm-d GKE overlays for placement
kubectl get nodes -l cloud.google.com/gke-accelerator \
  -o custom-columns='NAME:.metadata.name,BLOCK:.metadata.labels.cloud\.google\.com/gce-topology-block,SUBBLOCK:.metadata.labels.cloud\.google\.com/gce-topology-subblock'
```

## Path A: DRA + managed DRANET

From the llm-d GKE guide ("Verify DRA Device Readiness") and the GCP DRANET doc.

1. NVIDIA GPU DRA driver pods are Running:

   ```bash
   kubectl get pods -n nvidia-dra-driver-gpu
   # expect: nvidia-dra-driver-gpu-kubelet-plugin-*  1/1  Running
   ```

2. DRANET DeviceClasses installed (auto-installed by GKE 1.34.1-gke.1829001+):

   ```bash
   kubectl get deviceclass mrdma.google.com
   ```

3. ResourceSlices populated with BOTH device drivers:

   ```bash
   kubectl get resourceslices -o yaml
   ```

   Expect `ResourceSlice` objects (`resource.k8s.io/v1`) with:
   * `driver: gpu.nvidia.com` devices carrying GPU attributes (productName, and a
     `resource.kubernetes.io/pcieRoot` attribute)
   * `driver: mrdma.google.com` devices of `type: nic`, also with
     `resource.kubernetes.io/pcieRoot`

   Both must expose `pcieRoot` because the pd-disaggregation `gke-rdma-template`
   constrains GPU and NIC to the same PCIe root via
   `matchAttribute: "resource.kubernetes.io/pcieRoot"`.

4. Optional end-to-end claim test (from the GCP DRANET doc). Create a
   `ResourceClaimTemplate` requesting `mrdma.google.com` with `allocationMode: All`,
   run the `agnhost` test pod referencing it, then:

   ```bash
   kubectl exec agnhost-rdma -- ls /sys/class/net
   # expect interfaces like gpu0rdma0 ... in addition to eth0 and lo
   ```

   Delete the test pod and template afterwards.

## Path B: Multi-networking + gIB

1. Multi-networking CRs exist, one gvnic + eight rdma (names must be exactly
   `rdma-0` ... `rdma-7` to match the wide-ep-lws GKE overlay annotations):

   ```bash
   kubectl get networks.networking.gke.io
   kubectl get gkenetworkparamsets.networking.gke.io
   ```

2. GPU nodes advertise the per-network IP resources injected by GKE
   (modern A3 Ultra nodes advertise the `.IP` variant):

   ```bash
   kubectl get nodes -l cloud.google.com/gke-accelerator \
     -o jsonpath='{.items[0].status.allocatable}' | tr ',' '\n' | grep networking.gke.io
   ```

3. gIB installer DaemonSet is running on all GPU nodes:

   ```bash
   kubectl get daemonset -A | grep -i rdma
   ```

   And on a node (or via a debug pod): `/home/kubernetes/bin/gib` is non-empty.

4. gIB version is 1.10+ if vLLM 0.11.0+ will be used (NCCL 2.27 requirement from the
   llm-d GKE guide; installer version per
   [container-engine-accelerators PR #511](https://github.com/GoogleCloudPlatform/container-engine-accelerators/pull/511)).

## Gateway checks (only if Gateway mode was prepared)

```bash
kubectl get gatewayclass
# expect gke-l7-rilb / gke-l7-regional-external-managed, controller networking.gke.io/gateway, ACCEPTED True

gcloud compute networks subnets list --filter="purpose=REGIONAL_MANAGED_PROXY" \
  --format="value(name,region,network)"
# expect a proxy-only subnet in the cluster's region

kubectl api-resources --api-group=inference.networking.k8s.io
# expect InferencePool etc. (auto on GKE 1.34.0-gke.1626000+, else installed manually)
```

## Deeper network performance verification (optional)

For fabric-level verification after model servers are deployed (not at infra time),
`${LLMD_PATH}/docs/infrastructure/rdma/README.md` describes `ibv_devinfo`,
`nvidia-smi topo -m`, and `nixlbench` procedures. Point the user there rather than
duplicating them here.

## Note for autoconfig hand-off

The autoconfig skill's Phase 1 RDMA sweep
(`autoconfig/skill/llm-d-autoconfig/references/phase-1-cluster-discovery.md`) detects
the multi-networking model: DPv2 + `Network`/`GKENetworkParamSet` CRs + RDMA-named node
resources. A Path B cluster passing the checks above will be reported "RDMA-capable:
yes". A Path A (DRA/DRANET) cluster surfaces RDMA via DeviceClasses/ResourceSlices
instead and may be reported "RDMA-capable: no" by that sweep even though the
pd-disaggregation DRA overlay works on it - tell the user explicitly at hand-off.

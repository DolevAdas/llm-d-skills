# GKE Gateway API Prerequisites

Only needed when llm-d will run in Gateway mode with a GKE-managed GatewayClass
(`gke-l7-rilb` for VPC-internal traffic, `gke-l7-regional-external-managed` for
internet-facing). Standalone mode (the llm-d guides' default) needs none of this.

Sources:

* `${LLMD_PATH}/docs/infrastructure/gateway/gke.md`
* GCP: [Deploying Gateways](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/deploying-gateways)
* GCP: [Deploy GKE Inference Gateway](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/deploy-gke-inference-gateway)

IAM: enabling the Gateway API needs `roles/container.admin`; creating the proxy-only
subnet needs `roles/compute.networkAdmin`. See "Required IAM Roles" in SKILL.md.
Enabling the Gateway API is one of the two permitted mutations of an existing cluster
(see "Guardrails" in SKILL.md) - confirm with the user before running it.

## Step 1: Enable the Gateway API on the cluster

```bash
gcloud container clusters update ${CLUSTER_NAME} \
    --location=${LOCATION} \
    --gateway-api=standard
```

Per the GCP doc, this installs the Gateway API Standard Channel CRDs; reconciliation can
take up to 45 minutes. Confirm with:

```bash
gcloud container clusters describe ${CLUSTER_NAME} --location=${LOCATION} --format json
# look for networkConfig.gatewayApiConfig.channel == "CHANNEL_STANDARD"
```

## Step 2: Create a proxy-only subnet

Both GKE regional load balancers (`gke-l7-rilb` and `gke-l7-regional-external-managed`)
require a proxy-only subnet in the cluster's region and VPC:

```bash
gcloud compute networks subnets create ${PROXY_SUBNET_NAME} \
    --purpose=REGIONAL_MANAGED_PROXY \
    --role=ACTIVE \
    --region=${REGION} \
    --network=${VPC_NETWORK_NAME} \
    --range=${CIDR_RANGE}
```

Per the GCP doc, the range must be `/26` or larger (at least 64 proxy IPs); `/23` is
recommended. Check whether one already exists before creating:

```bash
gcloud compute networks subnets list \
    --filter="purpose=REGIONAL_MANAGED_PROXY" \
    --format="value(name,region,network)"
```

## Step 3: Verify the GatewayClasses

```bash
kubectl get gatewayclass
```

Expected (GCP doc): classes such as `gke-l7-rilb` and `gke-l7-regional-external-managed`
with controller `networking.gke.io/gateway` and `ACCEPTED: True`.

## Step 4: Gateway API Inference Extension CRDs

From the llm-d GKE gateway guide: GKE `1.34.0-gke.1626000` or later automatically
installs all GA CRDs for Gateway API and the Gateway API Inference Extension - nothing
to do. On earlier versions, install manually:

```bash
GAIE_VERSION=v1.5.0
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/${GAIE_VERSION}/v1-manifests.yaml
```

(When deploying via a Well-Lit Path guide, use the `${GAIE_VERSION}` pinned by
`${LLMD_PATH}/guides/env.sh` instead of hardcoding.)

Verify:

```bash
kubectl api-resources --api-group=gateway.networking.k8s.io
kubectl api-resources --api-group=inference.networking.k8s.io
```

The Gateway itself (`llm-d-inference-gateway`) is NOT created by this skill - it is
deployed at llm-d install time via
`${LLMD_PATH}/guides/recipes/gateway/gke-l7-rilb` or
`.../gke-l7-regional-external-managed` (see the deploy-llm-d skill and
`${LLMD_PATH}/docs/infrastructure/gateway/gke.md`).

#!/usr/bin/env bash
# Apply WVA autoscaling resources for a single llm-d deployment.
# Supports three modes:
#   va-hpa     - VariantAutoscaling CRD + HPA  (Prometheus Adapter backend)
#   keda       - VariantAutoscaling CRD + ScaledObject  (KEDA backend)
#   annotated  - Annotated HPA only (preferred, no VA CRD required)
#
# Usage:
#   ./apply-hpa.sh --mode <va-hpa|keda|annotated> \
#     --namespace <ns> \
#     --deployment <full-deployment-name> \
#     --model-id <model-id> \
#     --variant-cost <cost> \
#     --accelerator <nvidia|amd|cpu> \
#     --min-replicas <n> \
#     --max-replicas <n> \
#     --scale-up-window <seconds> \
#     --scale-down-window <seconds> \
#     [--prometheus-url <url>]     # required for keda mode
#
# The short name used for resource names is derived from --deployment
# by stripping the trailing -decode suffix (if present).

set -euo pipefail

MODE="" NAMESPACE="" DEPLOYMENT="" MODEL_ID="" VARIANT_COST=""
ACCELERATOR="" MIN=1 MAX=10 SCALE_UP=120 SCALE_DOWN=300 PROMETHEUS_URL=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --mode)            MODE=$2;            shift 2 ;;
    --namespace)       NAMESPACE=$2;       shift 2 ;;
    --deployment)      DEPLOYMENT=$2;      shift 2 ;;
    --model-id)        MODEL_ID=$2;        shift 2 ;;
    --variant-cost)    VARIANT_COST=$2;    shift 2 ;;
    --accelerator)     ACCELERATOR=$2;     shift 2 ;;
    --min-replicas)    MIN=$2;             shift 2 ;;
    --max-replicas)    MAX=$2;             shift 2 ;;
    --scale-up-window) SCALE_UP=$2;        shift 2 ;;
    --scale-down-window) SCALE_DOWN=$2;    shift 2 ;;
    --prometheus-url)  PROMETHEUS_URL=$2;  shift 2 ;;
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
  esac
done

# Validate required args
for var in MODE NAMESPACE DEPLOYMENT MODEL_ID VARIANT_COST; do
  [[ -z "${!var}" ]] && { echo "ERROR: --${var,,} is required" >&2; exit 1; }
done
[[ "$MODE" != "annotated" && -z "$ACCELERATOR" ]] && {
  echo "ERROR: --accelerator is required for mode '$MODE'" >&2; exit 1
}
[[ "$MODE" == "keda" && -z "$PROMETHEUS_URL" ]] && {
  echo "ERROR: --prometheus-url is required for keda mode" >&2; exit 1
}

# Derive short name: strip -decode suffix for resource names
SHORT="${DEPLOYMENT%-decode}"

apply_va_hpa() {
  kubectl apply -n "$NAMESPACE" -f - <<EOF
apiVersion: llmd.ai/v1alpha1
kind: VariantAutoscaling
metadata:
  name: ${SHORT}-va
  namespace: ${NAMESPACE}
  labels:
    inference.optimization/acceleratorName: ${ACCELERATOR}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: ${DEPLOYMENT}
  modelID: "${MODEL_ID}"
  variantCost: "${VARIANT_COST}"
  minReplicas: ${MIN}
  maxReplicas: ${MAX}
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: ${SHORT}-hpa
  namespace: ${NAMESPACE}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: ${DEPLOYMENT}
  minReplicas: ${MIN}
  maxReplicas: ${MAX}
  metrics:
  - type: External
    external:
      metric:
        name: wva_desired_replicas
        selector:
          matchLabels:
            variant_name: ${SHORT}-va
            exported_namespace: ${NAMESPACE}
      target:
        type: AverageValue
        averageValue: "1"
  behavior:
    scaleUp:
      stabilizationWindowSeconds: ${SCALE_UP}
      policies:
      - type: Pods
        value: 10
        periodSeconds: 15
    scaleDown:
      stabilizationWindowSeconds: ${SCALE_DOWN}
      policies:
      - type: Pods
        value: 10
        periodSeconds: 15
EOF
}

apply_keda() {
  kubectl apply -n "$NAMESPACE" -f - <<EOF
apiVersion: llmd.ai/v1alpha1
kind: VariantAutoscaling
metadata:
  name: ${SHORT}-va
  namespace: ${NAMESPACE}
  labels:
    inference.optimization/acceleratorName: ${ACCELERATOR}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: ${DEPLOYMENT}
  modelID: "${MODEL_ID}"
  variantCost: "${VARIANT_COST}"
  minReplicas: ${MIN}
  maxReplicas: ${MAX}
---
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: ${SHORT}-scaler
  namespace: ${NAMESPACE}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: ${DEPLOYMENT}
  pollingInterval: 5
  cooldownPeriod: 30
  maxReplicaCount: ${MAX}
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleUp:
          stabilizationWindowSeconds: ${SCALE_UP}
          policies:
          - type: Pods
            value: 10
            periodSeconds: 15
        scaleDown:
          stabilizationWindowSeconds: ${SCALE_DOWN}
          policies:
          - type: Pods
            value: 10
            periodSeconds: 15
  triggers:
  - type: prometheus
    name: wva-desired-replicas
    metadata:
      serverAddress: ${PROMETHEUS_URL}
      query: |
        wva_desired_replicas{variant_name="${SHORT}-va",exported_namespace="${NAMESPACE}"}
      threshold: '1'
      activationThreshold: '0'
      metricType: "AverageValue"
      unsafeSsl: "true"
EOF
}

apply_annotated() {
  kubectl apply -n "$NAMESPACE" -f - <<EOF
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: ${SHORT}-hpa
  namespace: ${NAMESPACE}
  annotations:
    llm-d.ai/managed: "true"
    llm-d.ai/model-id: "${MODEL_ID}"
    llm-d.ai/variant-cost: "${VARIANT_COST}"
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: ${DEPLOYMENT}
  minReplicas: ${MIN}
  maxReplicas: ${MAX}
  metrics:
  - type: External
    external:
      metric:
        name: wva_desired_replicas
        selector:
          matchLabels:
            variant_name: ${SHORT}-hpa
            exported_namespace: ${NAMESPACE}
      target:
        type: AverageValue
        averageValue: "1"
  behavior:
    scaleUp:
      stabilizationWindowSeconds: ${SCALE_UP}
      policies:
      - type: Pods
        value: 10
        periodSeconds: 15
    scaleDown:
      stabilizationWindowSeconds: ${SCALE_DOWN}
      policies:
      - type: Pods
        value: 10
        periodSeconds: 15
EOF
}

echo "Applying WVA resources: mode=$MODE namespace=$NAMESPACE deployment=$DEPLOYMENT"
case $MODE in
  va-hpa)    apply_va_hpa ;;
  keda)      apply_keda   ;;
  annotated) apply_annotated ;;
  *) echo "ERROR: unknown mode '$MODE' — must be va-hpa, keda, or annotated" >&2; exit 1 ;;
esac
echo "Done."

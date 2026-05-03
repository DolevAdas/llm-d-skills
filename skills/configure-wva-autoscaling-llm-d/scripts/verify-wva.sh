#!/bin/bash
# Verify WVA Configuration and Status
# Usage: ./verify-wva.sh <namespace> [wva-controller-namespace]

set -e

# Validate namespace input
if [[ -n "$1" && ! "$1" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "Error: Invalid namespace format"
  exit 1
fi

NAMESPACE=${1:-llm-inference}
WVA_NS=${2:-workload-variant-autoscaler-system}

echo "=== Checking VariantAutoscaling Status ==="
kubectl get variantautoscaling -n "$NAMESPACE" || echo "No VariantAutoscaling found"
echo ""

echo "=== Checking HPA Status ==="
kubectl get hpa -n "$NAMESPACE" || echo "No HPA found"
echo ""

echo "=== Checking WVA Controller Logs (last 20 lines) ==="
kubectl logs -n "$WVA_NS" \
  -l app.kubernetes.io/name=workload-variant-autoscaler \
  --tail=20 2>/dev/null || echo "WVA controller not found in namespace $WVA_NS"
echo ""

echo "=== Checking WVA Metrics ==="
if command -v jq &> /dev/null; then
  kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/$NAMESPACE/wva_desired_replicas" 2>/dev/null | jq '.' || echo "Metrics not available"
else
  kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/$NAMESPACE/wva_desired_replicas" 2>/dev/null || echo "Metrics not available (install jq for formatted output)"
fi
echo ""

echo "=== VariantAutoscaling Details ==="
kubectl get variantautoscaling -n "$NAMESPACE" -o yaml 2>/dev/null || echo "No VariantAutoscaling resources found"

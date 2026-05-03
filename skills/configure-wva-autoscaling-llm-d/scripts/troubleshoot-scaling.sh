#!/bin/bash
# Troubleshoot WVA Scaling Issues
# Usage: ./troubleshoot-scaling.sh <namespace> [wva-controller-namespace]

set -e

if [[ -n "$1" && ! "$1" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "Error: Invalid namespace format"
  exit 1
fi

NAMESPACE=${1:-llm-inference}
WVA_NS=${2:-workload-variant-autoscaler-system}

echo "=== WVA Scaling Decisions ==="
kubectl logs -n "$WVA_NS" \
  -l app.kubernetes.io/name=workload-variant-autoscaler \
  --tail=50 2>/dev/null | grep "desired replicas" || echo "No scaling decisions in recent logs"
echo ""

echo "=== Current Saturation ==="
kubectl get variantautoscaling -n "$NAMESPACE" -o yaml 2>/dev/null | grep -A 5 "saturation" || echo "No saturation data"
echo ""

echo "=== HPA Metrics ==="
kubectl describe hpa -n "$NAMESPACE" 2>/dev/null || echo "No HPA found"
echo ""

echo "=== HPA Events ==="
kubectl get events -n "$NAMESPACE" --sort-by='.lastTimestamp' 2>/dev/null | grep -i hpa | tail -20 || echo "No HPA events"
echo ""

echo "=== Replica Status ==="
kubectl get variantautoscaling -n "$NAMESPACE" -o custom-columns=NAME:.metadata.name,CURRENT:.status.currentReplicas,DESIRED:.status.desiredReplicas,SATURATION:.status.saturation 2>/dev/null || echo "No VariantAutoscaling found"

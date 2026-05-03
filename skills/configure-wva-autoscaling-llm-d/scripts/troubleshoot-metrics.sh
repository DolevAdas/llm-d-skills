#!/bin/bash
# Troubleshoot WVA Metrics Issues
# Usage: ./troubleshoot-metrics.sh <namespace> <pod-name>

set -e

if [[ -n "$1" && ! "$1" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "Error: Invalid namespace format"
  exit 1
fi

NAMESPACE=${1:-llm-inference}
POD_NAME=${2}

if [ -z "$POD_NAME" ]; then
  echo "Usage: $0 <namespace> <pod-name>"
  echo "Example: $0 llm-inference vllm-pod-abc123"
  exit 1
fi

echo "=== Checking if pod exposes metrics ==="
kubectl exec -n "$NAMESPACE" "$POD_NAME" -- curl -s localhost:8000/metrics 2>/dev/null | grep vllm || echo "No vllm metrics found or pod not accessible"
echo ""

echo "=== Test Request Instructions ==="
echo "Port-forward gateway: kubectl port-forward -n $NAMESPACE svc/<gateway-service> 8080:80"
echo "Send test request:"
echo 'curl -X POST http://localhost:8080/v1/chat/completions -H "Content-Type: application/json" -d '"'"'{"model": "<model-id>", "messages": [{"role": "user", "content": "test"}]}'"'"
echo ""

echo "=== Checking PodMonitor ==="
kubectl get podmonitor -n "$NAMESPACE" -o yaml 2>/dev/null || echo "No PodMonitor found"

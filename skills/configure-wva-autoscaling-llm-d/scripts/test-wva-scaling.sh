#!/bin/bash
# test-wva-scaling.sh - WVA autoscaling load test
# This script tests WVA autoscaling by sending load and monitoring the response
#
# Usage: ./test-wva-scaling.sh <namespace> <deployment-name> [model-id] [num-requests]
#
# Arguments:
#   namespace        - Target namespace
#   deployment-name  - Name of the deployment to test
#   model-id         - (Optional) Model ID for requests
#   num-requests     - (Optional) Number of concurrent requests (default: 100)

set -e

# Check arguments
if [ $# -lt 2 ]; then
    echo "Usage: $0 <namespace> <deployment-name> [model-id] [num-requests]"
    echo ""
    echo "Example:"
    echo "  $0 example-namespace my-llm-deployment \"Qwen/Qwen3-32B\" 100"
    exit 1
fi

NAMESPACE="$1"
DEPLOYMENT="$2"
MODEL_ID="${3:-}"
NUM_REQUESTS="${4:-100}"

# Try to auto-detect model ID if not provided
if [ -z "$MODEL_ID" ]; then
    echo "Attempting to auto-detect model ID..."
    MODEL_ID=$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="MODEL_ID")].value}' 2>/dev/null || echo "")
    
    if [ -z "$MODEL_ID" ]; then
        # Try to get from VariantAutoscaling
        MODEL_ID=$(kubectl get variantautoscaling -n "$NAMESPACE" -o jsonpath='{.items[0].spec.modelID}' 2>/dev/null || echo "")
    fi
    
    if [ -z "$MODEL_ID" ]; then
        echo "ERROR: Could not auto-detect model ID. Please provide it as the third argument."
        exit 1
    fi
    echo "✓ Auto-detected model ID: $MODEL_ID"
fi

# Detect EPP service name
EPP_SERVICE="${DEPLOYMENT}-epp"
if ! kubectl get service "$EPP_SERVICE" -n "$NAMESPACE" &>/dev/null; then
    # Try alternative naming patterns
    EPP_SERVICE=$(kubectl get service -n "$NAMESPACE" -o name | grep -i epp | head -1 | cut -d'/' -f2)
    if [ -z "$EPP_SERVICE" ]; then
        echo "ERROR: Could not find EPP service in namespace $NAMESPACE"
        echo "Available services:"
        kubectl get service -n "$NAMESPACE"
        exit 1
    fi
fi

echo "=========================================="
echo "WVA Autoscaling Load Test"
echo "=========================================="
echo "Namespace:       $NAMESPACE"
echo "Deployment:      $DEPLOYMENT"
echo "Model ID:        $MODEL_ID"
echo "EPP Service:     $EPP_SERVICE"
echo "Requests:        $NUM_REQUESTS"
echo "=========================================="
echo ""

# Step 1: Record baseline state
echo "=========================================="
echo "Step 1: Recording Baseline State"
echo "=========================================="
echo ""

echo "Deployment status:"
kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE"
echo ""

echo "VariantAutoscaling and HPA status:"
kubectl get variantautoscaling,hpa -n "$NAMESPACE"
echo ""

INITIAL_REPLICAS=$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o jsonpath='{.spec.replicas}')
echo "Initial replicas: $INITIAL_REPLICAS"
echo ""

# Step 2: Send load
echo "=========================================="
echo "Step 2: Sending Load"
echo "=========================================="
echo "Sending $NUM_REQUESTS concurrent requests with long outputs..."
echo "This will increase KV cache usage and potentially trigger scaling"
echo ""

# Create load test command
LOAD_TEST_CMD="
for i in \$(seq 1 $NUM_REQUESTS); do
  curl -s -X POST http://${EPP_SERVICE}/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{
      \"model\": \"${MODEL_ID}\",
      \"messages\": [{\"role\": \"user\", \"content\": \"Write a very long detailed story about the history of computing, artificial intelligence, machine learning, and the future of technology. Include specific examples and make it at least 1000 words with lots of technical details.\"}],
      \"max_tokens\": 1000
    }' > /dev/null &
done
echo 'Sent $NUM_REQUESTS concurrent requests'
sleep 10
echo 'Load test complete'
"

# Run load test from within cluster
kubectl run load-test-$$  --image=curlimages/curl:latest --rm -i --restart=Never -n "$NAMESPACE" -- sh -c "$LOAD_TEST_CMD"

echo ""
echo "✓ Load sent successfully"
echo ""

# Step 3: Monitor WVA response
echo "=========================================="
echo "Step 3: Monitoring WVA Response"
echo "=========================================="
echo "Checking WVA logs for scaling decisions..."
echo ""

sleep 5
kubectl logs -n "$NAMESPACE" -l app.kubernetes.io/name=workload-variant-autoscaler --tail=50 | grep -E "scale-up|Processing decision|Saturation analysis|avgSpareKv|avgSpareQueue" || echo "No scaling decisions found yet"
echo ""

# Step 4: Check metrics
echo "=========================================="
echo "Step 4: Checking vLLM Metrics"
echo "=========================================="
echo "Current KV cache and queue metrics:"
echo ""

POD=$(kubectl get pod -n "$NAMESPACE" -l app="$DEPLOYMENT" -o jsonpath='{.items[0].metadata.name}')
if [ -n "$POD" ]; then
    kubectl exec -n "$NAMESPACE" "$POD" -- curl -s localhost:8000/metrics 2>/dev/null | grep -E "vllm:kv_cache_usage_perc|vllm:num_requests_waiting" || echo "Could not retrieve metrics"
else
    echo "⚠ Could not find pod to check metrics"
fi
echo ""

# Step 5: Wait and check for scaling
echo "=========================================="
echo "Step 5: Waiting for Scaling"
echo "=========================================="
echo "Waiting 3 minutes for scaling to occur (respects stabilization window)..."
echo "You can monitor in another terminal with:"
echo "  kubectl get deployment $DEPLOYMENT -n $NAMESPACE -w"
echo ""

for i in {1..18}; do
    sleep 10
    CURRENT_REPLICAS=$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o jsonpath='{.spec.replicas}')
    echo "[$((i*10))s] Current replicas: $CURRENT_REPLICAS (initial: $INITIAL_REPLICAS)"
    
    if [ "$CURRENT_REPLICAS" -gt "$INITIAL_REPLICAS" ]; then
        echo ""
        echo "✓ Scale-up detected! Replicas increased from $INITIAL_REPLICAS to $CURRENT_REPLICAS"
        break
    fi
done
echo ""

# Step 6: Final status
echo "=========================================="
echo "Step 6: Final Status"
echo "=========================================="
echo ""

echo "Deployment status:"
kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE"
echo ""

echo "VariantAutoscaling and HPA status:"
kubectl get variantautoscaling,hpa -n "$NAMESPACE"
echo ""

FINAL_REPLICAS=$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o jsonpath='{.spec.replicas}')
echo "Replica change: $INITIAL_REPLICAS → $FINAL_REPLICAS"
echo ""

# Check WVA metrics
echo "Recent WVA scaling decisions:"
kubectl logs -n "$NAMESPACE" -l app.kubernetes.io/name=workload-variant-autoscaler --tail=50 | grep -E "avgSpareKv|avgSpareQueue|shouldScaleUp|desired replicas" || echo "No recent scaling decisions found"
echo ""

# Step 7: Analysis
echo "=========================================="
echo "Analysis"
echo "=========================================="
echo ""

if [ "$FINAL_REPLICAS" -gt "$INITIAL_REPLICAS" ]; then
    echo "✅ SUCCESS: WVA autoscaling is working!"
    echo "   - Initial replicas: $INITIAL_REPLICAS"
    echo "   - Final replicas: $FINAL_REPLICAS"
    echo "   - Scale-up occurred as expected"
    echo ""
    echo "Next: Wait for scale-down after load stops (~5 minutes)"
    echo "Monitor with: kubectl get deployment $DEPLOYMENT -n $NAMESPACE -w"
elif [ "$FINAL_REPLICAS" -eq "$INITIAL_REPLICAS" ]; then
    echo "⚠ NO SCALING OCCURRED"
    echo ""
    echo "Possible reasons:"
    echo "1. Load was insufficient to trigger saturation"
    echo "   - Check: avgSpareKv should drop below kvSpareTrigger (0.10)"
    echo "   - Solution: Increase NUM_REQUESTS or use longer max_tokens"
    echo ""
    echo "2. Deployment already at maxReplicas"
    echo "   - Check: kubectl get variantautoscaling -n $NAMESPACE"
    echo "   - Solution: Increase maxReplicas if needed"
    echo ""
    echo "3. Stabilization window not elapsed yet"
    echo "   - Check: kubectl describe hpa -n $NAMESPACE"
    echo "   - Solution: Wait longer (default scale-up window is 120s)"
    echo ""
    echo "4. WVA not monitoring correctly"
    echo "   - Check: kubectl logs -n $NAMESPACE -l app.kubernetes.io/name=workload-variant-autoscaler"
    echo "   - Look for: 'Saturation analysis completed'"
    echo ""
    echo "Troubleshooting commands:"
    echo "  ./troubleshoot-scaling.sh $NAMESPACE"
    echo "  ./troubleshoot-metrics.sh $NAMESPACE $POD"
else
    echo "⚠ UNEXPECTED: Replicas decreased during load test"
    echo "This should not happen. Check WVA logs for errors."
fi
echo ""

echo "=========================================="
echo "Test Complete"
echo "=========================================="
echo ""
echo "For detailed troubleshooting, see:"
echo "  - Troubleshooting.md"
echo "  - ./troubleshoot-scaling.sh $NAMESPACE"
echo "  - ./troubleshoot-metrics.sh $NAMESPACE <pod-name>"
echo ""

# Made with Bob

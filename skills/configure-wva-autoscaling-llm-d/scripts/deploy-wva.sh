#!/bin/bash
# deploy-wva.sh - Automated WVA deployment script
# This script automates the complete WVA setup process for llm-d deployments
#
# Usage: ./deploy-wva.sh <namespace> <deployment-name> <wva-repo-path> [model-id] [variant-cost]
#
# Arguments:
#   namespace        - Target namespace for WVA deployment
#   deployment-name  - Name of the llm-d deployment to autoscale
#   wva-repo-path    - Path to WVA repository
#   model-id         - (Optional) Model ID (e.g., "Qwen/Qwen3-32B")
#   variant-cost     - (Optional) Variant cost as string (e.g., "100")

set -e

# Check arguments
if [ $# -lt 3 ]; then
    echo "Usage: $0 <namespace> <deployment-name> <wva-repo-path> [model-id] [variant-cost]"
    echo ""
    echo "Example:"
    echo "  $0 dolev-inf qwen32-dolev-inf /path/to/wva-repo \"Qwen/Qwen3-32B\" \"100\""
    exit 1
fi

NAMESPACE="$1"
DEPLOYMENT="$2"
WVA_REPO="$3"
MODEL_ID="${4:-}"
VARIANT_COST="${5:-100}"

echo "=========================================="
echo "WVA Deployment Configuration"
echo "=========================================="
echo "Namespace:       $NAMESPACE"
echo "Deployment:      $DEPLOYMENT"
echo "WVA Repository:  $WVA_REPO"
echo "Model ID:        ${MODEL_ID:-<will auto-detect>}"
echo "Variant Cost:    $VARIANT_COST"
echo "=========================================="
echo ""

# Verify WVA repository exists
if [ ! -d "$WVA_REPO" ]; then
    echo "ERROR: WVA repository not found at: $WVA_REPO"
    exit 1
fi

# Verify deployment exists
if ! kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" &>/dev/null; then
    echo "ERROR: Deployment '$DEPLOYMENT' not found in namespace '$NAMESPACE'"
    exit 1
fi

echo "✓ Pre-flight checks passed"
echo ""

# Phase 1: Deploy WVA controller
echo "=========================================="
echo "Phase 1: Deploying WVA Controller"
echo "=========================================="
echo "Deploying WVA into namespace: $NAMESPACE"
echo "This will make WVA watch only this namespace"
echo ""

cd "$WVA_REPO"
helm upgrade --install workload-variant-autoscaler ./charts/workload-variant-autoscaler \
  --namespace "$NAMESPACE" \
  --create-namespace \
  --set controller.namespaceScoped=true

echo ""
echo "✓ WVA controller deployed"
echo ""

# Wait for controller to be ready
echo "Waiting for WVA controller to be ready..."
kubectl wait --for=condition=available --timeout=120s \
  deployment -l app.kubernetes.io/name=workload-variant-autoscaler -n "$NAMESPACE"

echo ""
echo "✓ WVA controller is ready"
echo ""

# Verify namespace-scoping
echo "Verifying WVA is watching correct namespace..."
sleep 5
WATCH_NS=$(kubectl logs -n "$NAMESPACE" -l app.kubernetes.io/name=workload-variant-autoscaler --tail=50 | grep "Watching" | tail -1)
echo "$WATCH_NS"

if echo "$WATCH_NS" | grep -q "$NAMESPACE"; then
    echo "✓ WVA is correctly watching namespace: $NAMESPACE"
else
    echo "⚠ WARNING: WVA may not be watching the correct namespace"
    echo "Check logs: kubectl logs -n $NAMESPACE -l app.kubernetes.io/name=workload-variant-autoscaler"
fi
echo ""

# Phase 2: Prepare deployment
echo "=========================================="
echo "Phase 2: Preparing Deployment"
echo "=========================================="

echo "Adding required accelerator label..."
kubectl label deployment "$DEPLOYMENT" -n "$NAMESPACE" \
  inference.optimization/acceleratorName=nvidia --overwrite

echo "✓ Accelerator label added"
echo ""

# Verify label
echo "Verifying label..."
if kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" --show-labels | grep -q "acceleratorName=nvidia"; then
    echo "✓ Label verified"
else
    echo "⚠ WARNING: Label may not have been applied correctly"
fi
echo ""

echo "Checking for metrics infrastructure..."
if kubectl get service "${DEPLOYMENT}-metrics" -n "$NAMESPACE" &>/dev/null; then
    echo "✓ Metrics Service already exists"
else
    echo "Creating metrics Service..."
    kubectl apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: ${DEPLOYMENT}-metrics
  namespace: ${NAMESPACE}
  labels:
    app: ${DEPLOYMENT}
spec:
  selector:
    app: ${DEPLOYMENT}
  ports:
  - name: metrics
    port: 8000
    targetPort: 8000
    protocol: TCP
EOF
    echo "✓ Metrics Service created"
fi
echo ""

if kubectl get servicemonitor "${DEPLOYMENT}-metrics" -n "$NAMESPACE" &>/dev/null; then
    echo "✓ ServiceMonitor already exists"
else
    echo "Creating ServiceMonitor..."
    kubectl apply -f - <<EOF
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: ${DEPLOYMENT}-metrics
  namespace: ${NAMESPACE}
spec:
  selector:
    matchLabels:
      app: ${DEPLOYMENT}
  endpoints:
  - port: metrics
    path: /metrics
    interval: 30s
EOF
    echo "✓ ServiceMonitor created"
fi
echo ""

# Phase 3: Apply configurations
echo "=========================================="
echo "Phase 3: Applying WVA Configuration"
echo "=========================================="

# Check if configuration files exist in current directory
CONFIG_DIR="$(pwd)"
if [ -f "$CONFIG_DIR/variantautoscaling.yaml" ]; then
    echo "Applying VariantAutoscaling from: $CONFIG_DIR/variantautoscaling.yaml"
    kubectl apply -f "$CONFIG_DIR/variantautoscaling.yaml"
    echo "✓ VariantAutoscaling applied"
else
    echo "⚠ WARNING: variantautoscaling.yaml not found in current directory"
    echo "You will need to create and apply this manually"
fi
echo ""

if [ -f "$CONFIG_DIR/hpa.yaml" ]; then
    echo "Applying HPA from: $CONFIG_DIR/hpa.yaml"
    kubectl apply -f "$CONFIG_DIR/hpa.yaml"
    echo "✓ HPA applied"
else
    echo "⚠ WARNING: hpa.yaml not found in current directory"
    echo "You will need to create and apply this manually"
fi
echo ""

if [ -f "$CONFIG_DIR/configmap-saturation.yaml" ]; then
    echo "Applying saturation ConfigMap from: $CONFIG_DIR/configmap-saturation.yaml"
    kubectl apply -f "$CONFIG_DIR/configmap-saturation.yaml"
    echo "✓ Saturation ConfigMap applied"
else
    echo "ℹ Using default saturation thresholds (no custom ConfigMap found)"
fi
echo ""

# Verification
echo "=========================================="
echo "Verification"
echo "=========================================="

echo "Checking resources..."
kubectl get variantautoscaling,hpa -n "$NAMESPACE"
echo ""

echo "Waiting for WVA to detect VariantAutoscaling (10 seconds)..."
sleep 10

echo ""
echo "Checking WVA controller logs..."
kubectl logs -n "$NAMESPACE" -l app.kubernetes.io/name=workload-variant-autoscaler --tail=30 | grep -E "VariantAutoscaling|No active"
echo ""

echo "Waiting for metrics to be ready (120 seconds for Prometheus scrape)..."
echo "You can monitor progress with: kubectl get variantautoscaling -n $NAMESPACE -w"
sleep 120

echo ""
echo "Final status:"
kubectl get variantautoscaling -n "$NAMESPACE"
echo ""

echo "=========================================="
echo "Deployment Complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "1. Verify METRICSREADY is True: kubectl get variantautoscaling -n $NAMESPACE"
echo "2. Monitor WVA logs: kubectl logs -n $NAMESPACE -l app.kubernetes.io/name=workload-variant-autoscaler -f"
echo "3. Check HPA status: kubectl describe hpa -n $NAMESPACE"
echo "4. (Optional) Run load test: ./test-wva-scaling.sh $NAMESPACE $DEPLOYMENT"
echo ""

# Made with Bob

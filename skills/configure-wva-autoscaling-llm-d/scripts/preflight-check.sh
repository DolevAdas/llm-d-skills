#!/usr/bin/env bash
# Pre-flight checks before deploying WVA.
# Usage: ./preflight-check.sh <wva-namespace> [--scaler-backend prometheus-adapter|keda]
# Exit 0 = all clear; exit 1 = issues found (details on stderr).

set -euo pipefail

WVA_NS=${1:?Usage: $0 <wva-namespace> [--scaler-backend prometheus-adapter|keda]}
SCALER_BACKEND="prometheus-adapter"
[[ "${2:-}" == "--scaler-backend" ]] && SCALER_BACKEND="${3:-prometheus-adapter}"

issues=0

echo "=== WVA Pre-flight Checks ==="
echo "Namespace: $WVA_NS | Scaler: $SCALER_BACKEND"
echo ""

# 1. Check for existing WVA controller
echo "--- Checking for existing WVA controller ---"
if kubectl get deployment workload-variant-autoscaler-controller-manager \
     -n "$WVA_NS" &>/dev/null; then
  echo "WARNING: WVA controller already deployed in '$WVA_NS'."
  echo "  To remove: WVA_NS=$WVA_NS NAMESPACE=$WVA_NS ./deploy/install.sh --undeploy"
  issues=$((issues + 1))
else
  echo "OK: No existing WVA controller found."
fi
echo ""

# 2. Check external metrics API (Prometheus Adapter)
if [[ "$SCALER_BACKEND" == "prometheus-adapter" ]]; then
  echo "--- Checking Prometheus Adapter (external metrics API) ---"
  status=$(kubectl get apiservice v1beta1.external.metrics.k8s.io \
    -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null || true)
  if [[ "$status" == "True" ]]; then
    echo "OK: External metrics API is available."
  else
    echo "INFO: External metrics API not yet available (will be deployed by install.sh if DEPLOY_PROMETHEUS_ADAPTER=true)."
  fi
  echo ""
fi

# 3. Check KEDA
if [[ "$SCALER_BACKEND" == "keda" ]]; then
  echo "--- Checking KEDA ---"
  if kubectl get deployment keda-operator -A &>/dev/null; then
    echo "OK: KEDA operator found."
  else
    echo "WARNING: KEDA operator not found. Install KEDA before deploying with SCALER_BACKEND=keda."
    issues=$((issues + 1))
  fi
  echo ""
fi

# 4. Check kubectl connectivity
echo "--- Checking cluster connectivity ---"
kubectl cluster-info --request-timeout=5s &>/dev/null \
  && echo "OK: Cluster reachable." \
  || { echo "ERROR: Cannot reach cluster."; issues=$((issues + 1)); }
echo ""

if [[ $issues -gt 0 ]]; then
  echo "Pre-flight: $issues issue(s) found — review warnings above before proceeding."
  exit 1
else
  echo "Pre-flight: all checks passed."
fi

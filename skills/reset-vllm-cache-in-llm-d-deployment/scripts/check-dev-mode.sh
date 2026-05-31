#!/bin/bash
# Checks whether VLLM_SERVER_DEV_MODE is enabled on vLLM pods.
# Requires: NAMESPACE.
set -euo pipefail

NAMESPACE="${NAMESPACE:?NAMESPACE must be set}"
LABEL_SELECTOR="${LABEL_SELECTOR:-app.kubernetes.io/component=vllm}"

echo "=== Checking VLLM_SERVER_DEV_MODE on vLLM pods ==="

POD_NAMES=$(kubectl get pods -n "$NAMESPACE" -l "$LABEL_SELECTOR" --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}')

if [ -z "$POD_NAMES" ]; then
  echo "ERROR: No running vLLM pods found"
  exit 1
fi

ENABLED=0
DISABLED=0

for POD in $POD_NAMES; do
  DEV_MODE=$(kubectl exec -n "$NAMESPACE" "$POD" -- printenv VLLM_SERVER_DEV_MODE 2>/dev/null || echo "unset")

  if [ "$DEV_MODE" = "1" ]; then
    echo "  $POD: ENABLED"
    ENABLED=$((ENABLED + 1))
  else
    echo "  $POD: DISABLED (value: $DEV_MODE)"
    DISABLED=$((DISABLED + 1))
  fi
done

echo ""
if [ $DISABLED -gt 0 ]; then
  echo "WARNING: $DISABLED pod(s) do NOT have VLLM_SERVER_DEV_MODE=1"
  echo ""
  echo "To enable, add to your vLLM pod spec:"
  echo "  env:"
  echo "    - name: VLLM_SERVER_DEV_MODE"
  echo "      value: \"1\""
  echo ""
  echo "The /reset_prefix_cache endpoint will return 404 on pods without dev mode."
  exit 1
fi

echo "All pods have VLLM_SERVER_DEV_MODE=1 — reset endpoint is available."
exit 0

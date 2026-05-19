#!/usr/bin/env bash
# Detect the GPU accelerator vendor label for a llm-d deployment.
# Outputs one of: nvidia, amd, cpu
#
# Usage: ./detect-accelerator.sh <namespace> <deployment-name>
# Exit 0 with result on stdout; exit 1 if detection fails.

set -euo pipefail

NAMESPACE=${1:?Usage: $0 <namespace> <deployment-name>}
DEPLOYMENT=${2:?Usage: $0 <namespace> <deployment-name>}

result=""

# 1. Deployment-level label
result=$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" \
  -o jsonpath='{.metadata.labels.inference\.optimization/acceleratorName}' 2>/dev/null || true)
[[ -n "$result" ]] && echo "$result" && exit 0

# 2. Pod-template labels
result=$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" \
  -o jsonpath='{.spec.template.metadata.labels.inference\.optimization/acceleratorName}' 2>/dev/null || true)
[[ -n "$result" ]] && echo "$result" && exit 0

# 3. Node selector hints
node_selector=$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" \
  -o jsonpath='{.spec.template.spec.nodeSelector}' 2>/dev/null || true)
if echo "$node_selector" | grep -qi "nvidia"; then echo "nvidia" && exit 0; fi
if echo "$node_selector" | grep -qi "amd";    then echo "amd"    && exit 0; fi

# 4. Node labels from the pod's running node
node_name=$(kubectl get pod -n "$NAMESPACE" \
  -l "llm-d.ai/role=decode" \
  --field-selector "spec.nodeName!=" \
  -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null || true)
if [[ -n "$node_name" ]]; then
  node_labels=$(kubectl get node "$node_name" -o jsonpath='{.metadata.labels}' 2>/dev/null || true)
  if echo "$node_labels" | grep -q '"nvidia\.com'; then echo "nvidia" && exit 0; fi
  if echo "$node_labels" | grep -q '"amd\.com';    then echo "amd"    && exit 0; fi
fi

echo "ERROR: could not auto-detect accelerator for deployment '$DEPLOYMENT' in namespace '$NAMESPACE'" >&2
echo "Please specify manually: nvidia, amd, or cpu" >&2
exit 1

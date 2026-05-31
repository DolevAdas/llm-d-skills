#!/bin/bash
# Floods vLLM pods with random unique prompts to evict cached KV entries via LRU.
# Fallback method when /reset_prefix_cache is not available (dev mode disabled).
# Requires: NAMESPACE, VLLM_PORT (default 8000), MODEL_NAME.
set -uo pipefail

NAMESPACE="${NAMESPACE:?NAMESPACE must be set}"
VLLM_PORT="${VLLM_PORT:-8000}"
MODEL_NAME="${MODEL_NAME:?MODEL_NAME must be set (e.g. meta-llama/Llama-3-8B-Instruct)}"
LABEL_SELECTOR="${LABEL_SELECTOR:-app.kubernetes.io/component=vllm}"
NUM_REQUESTS="${NUM_FLOOD_REQUESTS:-200}"
MAX_TOKENS="${FLOOD_MAX_TOKENS:-1}"
PROMPT_LENGTH="${FLOOD_PROMPT_LENGTH:-4000}"

echo "=== Flooding vLLM pods with random prompts to evict KV cache ==="
echo "Namespace:       $NAMESPACE"
echo "Model:           $MODEL_NAME"
echo "Requests/pod:    $NUM_REQUESTS"
echo "Prompt length:   ~$PROMPT_LENGTH chars"
echo ""

POD_NAMES=$(kubectl get pods -n "$NAMESPACE" -l "$LABEL_SELECTOR" --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}')

if [ -z "$POD_NAMES" ]; then
  echo "ERROR: No running vLLM pods found with selector '$LABEL_SELECTOR' in namespace '$NAMESPACE'"
  exit 1
fi

POD_COUNT=$(echo "$POD_NAMES" | wc -w | tr -d ' ')
echo "Found $POD_COUNT vLLM pod(s)"
echo ""

PARALLEL_JOBS="${PARALLEL_JOBS:-5}"

generate_random_prompt() {
  LC_ALL=C tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c "$PROMPT_LENGTH"
}

send_request() {
  local POD="$1"
  local RANDOM_PROMPT
  RANDOM_PROMPT=$(generate_random_prompt)

  local PAYLOAD="{\"model\":\"$MODEL_NAME\",\"prompt\":\"$RANDOM_PROMPT\",\"max_tokens\":$MAX_TOKENS,\"temperature\":1.0}"

  kubectl exec -n "$NAMESPACE" "$POD" -- \
    curl -s -o /dev/null -w "%{http_code}" -X POST \
    "http://localhost:${VLLM_PORT}/v1/completions" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" 2>/dev/null || echo "000"
}

flood_pod() {
  local POD="$1"
  local SUCCESS=0
  local FAILED=0
  local BATCH_PIDS=()

  echo "Flooding $POD with $NUM_REQUESTS requests ($PARALLEL_JOBS parallel)..."

  for i in $(seq 1 "$NUM_REQUESTS"); do
    send_request "$POD" > "/tmp/flood_${POD}_${i}.out" &
    BATCH_PIDS+=($!)

    if [ ${#BATCH_PIDS[@]} -ge "$PARALLEL_JOBS" ] || [ "$i" -eq "$NUM_REQUESTS" ]; then
      for PID in "${BATCH_PIDS[@]}"; do
        wait "$PID" 2>/dev/null
      done

      for j in $(seq $((i - ${#BATCH_PIDS[@]} + 1)) "$i"); do
        RESULT_FILE="/tmp/flood_${POD}_${j}.out"
        if [ -f "$RESULT_FILE" ]; then
          HTTP_CODE=$(cat "$RESULT_FILE")
          rm -f "$RESULT_FILE"
          if [ "$HTTP_CODE" = "200" ]; then
            SUCCESS=$((SUCCESS + 1))
          else
            FAILED=$((FAILED + 1))
          fi
        fi
      done
      BATCH_PIDS=()
    fi

    if [ $((i % 10)) -eq 0 ]; then
      echo "  $POD: $i/$NUM_REQUESTS sent (success: $SUCCESS, failed: $FAILED)"
    fi
  done

  echo "  $POD: Done — $SUCCESS/$NUM_REQUESTS successful"
  return 0
}

OVERALL_SUCCESS=0
for POD in $POD_NAMES; do
  flood_pod "$POD" && OVERALL_SUCCESS=$((OVERALL_SUCCESS + 1))
done

echo ""
echo "=== Flood complete ==="
echo "Pods flooded: $OVERALL_SUCCESS / $POD_COUNT"
echo ""
echo "KV cache should now be filled with random data, evicting previous entries."
echo "Wait 5s before running benchmarks to let queued requests drain."
exit 0

---
name: reset-vllm-cache-in-llm-d-deployment
description: Clears the KV / prefix cache on all vLLM pods in an llm-d deployment for a clean state. Use when the user wants to flush the cache, reset vLLM state, or start fresh before a test run — even if they don't say "cache" explicitly. Prefers the /reset_prefix_cache API; falls back to random-prompt flooding or pod restart.
---

# Reset vLLM Cache in llm-d

## Purpose

Clear the KV / prefix cache on all vLLM pods in an llm-d deployment so the next run starts from a clean state. Prefers non-restart methods; falls back to pod restart when necessary.

---

## Step 1: Ask for Namespace and Deployment

Detect the current namespace and related options:
```bash
CURRENT_NS=$(oc project -q 2>/dev/null || kubectl config view --minify -o jsonpath='{..namespace}' 2>/dev/null || echo "")
SUGGESTED_NS=$(kubectl get namespaces -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
  | tr ' ' '\n' | grep -iE "llm-d|inference|serving|benchmark" | grep -v "^$CURRENT_NS$" | head -5)
```

Ask the user:
> "Which namespace?
> **1. \<CURRENT_NS\>** *(current context)*
> **2–N.** \<SUGGESTED_NS\>
> *(or type any namespace)*"

Set `NAMESPACE` to the user's answer. If the user just confirms without specifying, use `CURRENT_NS` — the namespace they are currently working in.

List llm-d deployments:
```bash
kubectl get deployments -n $NAMESPACE -l app.kubernetes.io/part-of=llm-d -o custom-columns="NAME:.metadata.name,READY:.status.readyReplicas,DESIRED:.spec.replicas,AGE:.metadata.creationTimestamp"
# If empty, broaden:
kubectl get deployments -n $NAMESPACE | grep -iE "llm-d|vllm"
```

- **One result** → use automatically, inform the user.
- **Multiple** → ask: *"Which deployment? (or 'all' to reset every vLLM pod in the namespace)"*

Derive `LABEL_SELECTOR`:
```bash
# Specific deployment:
LABEL_SELECTOR="app.kubernetes.io/component=vllm,app.kubernetes.io/instance=$DEPLOYMENT_NAME"
# If no pods found, fall back to the deployment's own selector:
LABEL_SELECTOR=$(kubectl get deployment -n $NAMESPACE $DEPLOYMENT_NAME -o jsonpath='{.spec.selector.matchLabels}' | jq -r 'to_entries | map("\(.key)=\(.value)") | join(",")')
# For 'all':
LABEL_SELECTOR="app.kubernetes.io/component=vllm"
```

Show the affected pods and ask the user to confirm:
```bash
kubectl get pods -n $NAMESPACE -l "$LABEL_SELECTOR" --field-selector=status.phase=Running -o wide
```

---

## Step 2: Check Dev Mode Status

Run the dev-mode check script:
```bash
bash skills/reset-vllm-cache-in-llm-d-deployment/scripts/check-dev-mode.sh
```

`VLLM_SERVER_DEV_MODE=1` is required for the `/reset_prefix_cache` endpoint. It only registers extra API routes at startup — no effect on inference speed (TPS, TTFT).

If **not enabled**, offer:
> **1. Enable dev mode** — patches the deployment and restarts pods. The restart clears the cache as a side effect. **Skill ends here.**
> **2. Flood fallback** — no restart, slower, GPU-only.
> **3. Abort**

**Option 1:**
```bash
kubectl patch deployment $DEPLOYMENT_NAME -n $NAMESPACE --type='json' \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"VLLM_SERVER_DEV_MODE","value":"1"}}]'
kubectl rollout status deployment/$DEPLOYMENT_NAME -n $NAMESPACE --timeout=300s
```
Report success and stop. Future resets can use Step 3 without restarting.

**Option 2** → go to Step 4. **Option 3** → stop.

---

## Step 3: Reset via /reset_prefix_cache (Preferred)

If dev mode is confirmed, run the reset script:
```bash
export NAMESPACE="$NAMESPACE"
export VLLM_PORT="${VLLM_PORT:-8000}"
export LABEL_SELECTOR="$LABEL_SELECTOR"
export RESET_RUNNING_REQUESTS=true
export RESET_EXTERNAL=true
bash skills/reset-vllm-cache-in-llm-d-deployment/scripts/reset-prefix-cache.sh
```

| Variable | Default | Description |
|----------|---------|-------------|
| `VLLM_PORT` | `8000` | Port vLLM listens on inside the pod |
| `RESET_RUNNING_REQUESTS` | `true` | Preempts in-flight requests; use `false` to clear cache without interrupting active work |
| `RESET_EXTERNAL` | `true` | Clears CPU offload blocks **only if** `CPUOffloadingSpec` is used (2-tier). With `TieringOffloadingSpec` (3-tier: GPU+CPU+disk), this is a no-op — neither CPU nor disk is cleared. No-op for NixL/LMCache connectors too. |

**Warn the user before running:** `RESET_RUNNING_REQUESTS=true` terminates in-flight requests. Ensure no active traffic is hitting the pods.

**On success:** report done, user can proceed after 2 seconds.

**On failure** — auto-diagnose per pod before asking the user:

| Error | Auto-fix |
|-------|----------|
| **404** — dev mode missing on pod | Patch deployment + rollout (same as Step 2 option 1). Restart clears cache — end skill. |
| **Connection refused** | Check pod status/logs. Wait up to 60s for Ready, retry once. Report OOMKilled/CrashLoop to user. |
| **RBAC / exec error** | Try port-forward: `kubectl port-forward pod/$POD -n $NAMESPACE 18000:$VLLM_PORT &`, then curl `http://localhost:18000/reset_prefix_cache?...`. |

If still failing: *"Reset failed on \<pod(s)\>: \<reason\>. Try flood fallback?"*

**Last resort** (flood also declined or fails): restart the decoder pods:
```bash
kubectl rollout restart deployment/$DEPLOYMENT_NAME -n $NAMESPACE
kubectl rollout status deployment/$DEPLOYMENT_NAME -n $NAMESPACE --timeout=300s
```

---

## Step 4: Fallback — Flood with Random Prompts

> **How it works:** sends unique random prompts to saturate the GPU KV cache, evicting old entries via LRU. After flooding, GPU cache is ≈ 100% full — **not** empty. Success is confirmed when `kv_cache_usage_perc ≥ 0.9`.
>
> **CPU offloading:** the flood does **not** clear the CPU cache. Blocks evicted from GPU are copied to the (larger) CPU cache and remain accessible. If CPU offloading is enabled, use Step 3 with `reset_external=true` instead.

Before starting, offer a restart as an alternative:
> "Pod restart (~1-2 min) guarantees a fully clean cache (GPU + CPU). Flood is faster but GPU-only. Which do you prefer?"

Detect model name:
```bash
FIRST_POD=$(kubectl get pods -n $NAMESPACE -l "$LABEL_SELECTOR" -o jsonpath='{.items[0].metadata.name}')
MODEL_NAME=$(kubectl exec -n $NAMESPACE $FIRST_POD -- printenv SERVED_MODEL_NAME 2>/dev/null || echo "")
[ -z "$MODEL_NAME" ] && MODEL_NAME=$(kubectl exec -n $NAMESPACE $FIRST_POD -- \
  curl -s http://localhost:${VLLM_PORT}/v1/models 2>/dev/null | jq -r '.data[0].id // empty')
```
If still empty, ask the user.

Run:
```bash
export NAMESPACE="$NAMESPACE" VLLM_PORT="${VLLM_PORT:-8000}" MODEL_NAME="$MODEL_NAME"
export LABEL_SELECTOR="$LABEL_SELECTOR"
export NUM_FLOOD_REQUESTS="${NUM_FLOOD_REQUESTS:-200}"   # increase for 70B+ models
export FLOOD_PROMPT_LENGTH="${FLOOD_PROMPT_LENGTH:-4000}"
bash skills/reset-vllm-cache-in-llm-d-deployment/scripts/flood-random-prompts.sh
```

| Variable | Default | Description |
|----------|---------|-------------|
| `NUM_FLOOD_REQUESTS` | `200` | Requests per pod |
| `FLOOD_PROMPT_LENGTH` | `4000` | Chars per prompt |
| `FLOOD_MAX_TOKENS` | `1` | Keep low — output doesn't matter |
| `PARALLEL_JOBS` | `5` | Concurrent requests per pod |

Verify saturation after flooding (up to 10 retries, no sleep):
```bash
POD_NAMES=$(kubectl get pods -n $NAMESPACE -l "$LABEL_SELECTOR" --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}')
for POD in $POD_NAMES; do
  SATURATED="no"
  for i in $(seq 1 10); do
    USAGE=$(kubectl exec -n $NAMESPACE $POD -- \
      curl -s http://localhost:$VLLM_PORT/metrics 2>/dev/null \
      | grep "vllm:kv_cache_usage_perc" | grep -v "HELP\|TYPE" | awk '{print $2}')
    [ -z "$USAGE" ] && echo "$POD: could not read metrics, retrying..." && continue
    SATURATED=$(awk "BEGIN {print ($USAGE >= 0.9) ? \"yes\" : \"no\"}")
    [ "$SATURATED" = "yes" ] && echo "$POD: saturated ($USAGE) — old entries displaced" && break
    echo "$POD: at $USAGE, retrying..."
  done
  [ "$SATURATED" != "yes" ] && echo "WARNING: $POD only reached $USAGE — increase NUM_FLOOD_REQUESTS"
done
```

Report success once all pods show `≥ 0.9`. If any pod stays below, tell the user and suggest increasing `NUM_FLOOD_REQUESTS` or `FLOOD_PROMPT_LENGTH`.

---

## Step 5: Verification (Optional)

These are live gauges — they reflect current state without sending any requests:

```bash
POD=$(kubectl get pods -n $NAMESPACE -l "$LABEL_SELECTOR" --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n $NAMESPACE $POD -- \
  curl -s http://localhost:${VLLM_PORT}/metrics \
  | grep -E "vllm:kv_cache_usage_perc|vllm:cpu_kv_cache_usage_perc" \
  | grep -v "HELP\|TYPE"
```

| Method | Expected GPU | Expected CPU |
|--------|-------------|--------------|
| Step 3 (`/reset_prefix_cache`) | `0.0` | `0.0` |
| Step 4 (flood) | `≥ 0.9` | not meaningful — flood doesn't clear CPU |

> Do **not** use `prefix_cache_hits_total` / `prefix_cache_queries_total` — these counters accumulate since pod start and never reset.

---

## Important Notes

- **NixL / LMCache connectors**: `reset_external=true` is a no-op for these. The shared KV store in disaggregated prefill/decode setups will NOT be cleared.
- **CPU offloading**: Step 3 with `reset_external=true` clears the CPU cache **only when `CPUOffloadingSpec` is used** (2-tier setup). If `TieringOffloadingSpec` is configured (3-tier: GPU + CPU + disk), `reset_external=true` is a no-op for both CPU and disk — this is a vLLM limitation. The flood method never clears the CPU cache regardless.
- **In-flight requests**: `reset_running_requests=true` terminates active requests. Use `false` to clear the cache without disrupting ongoing work.
- **Dev mode security**: `VLLM_SERVER_DEV_MODE=1` exposes internal endpoints. Disable after use in production-like environments.
- **Pod restarts**: Step 3 and flood do not restart pods. Enabling dev mode (Step 2 option 1) does — but that clears the cache as a side effect, so no further reset is needed.

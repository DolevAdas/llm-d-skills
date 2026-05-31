---
name: reset-vllm-cache-in-llm-d-deployment
description: Resets the KV cache on all vLLM pods in an llm-d deployment without restarting pods. Use this skill when the user wants to clear caches before benchmarking, reset vLLM state, flush prefix cache, or get a clean deployment state for performance testing — even if they don't say "cache" explicitly. Supports both the /reset_prefix_cache API (preferred) and a random-prompt flood fallback.
---

# Reset vLLM Cache in llm-d

## Purpose

Clear the KV / prefix cache on all vLLM pods in a running llm-d deployment so benchmarks start from a clean state — without restarting pods. This avoids the overhead of pod restart (model reload, warmup) while ensuring no cached prefixes skew benchmark results.

---

## Step 1: Ask for Namespace and Deployment

**Always ask the user explicitly:**
> "Which namespace is the llm-d deployment in?"

Do not attempt to auto-detect or assume a namespace. Wait for their answer, then set `NAMESPACE` to that value.

Once the namespace is confirmed, list the llm-d deployments in it:
```bash
kubectl get deployments -n $NAMESPACE -l app.kubernetes.io/part-of=llm-d -o custom-columns="NAME:.metadata.name,READY:.status.readyReplicas,DESIRED:.spec.replicas,AGE:.metadata.creationTimestamp"
```

If that returns nothing, try a broader search:
```bash
kubectl get deployments -n $NAMESPACE | grep -i "llmd\|llm-d\|vllm"
```

Show the list to the user and ask:
> "Which deployment should be cache-reset? (or 'all' to reset every vLLM pod in the namespace)"

Set `DEPLOYMENT_NAME` to their answer. If they choose a specific deployment, scope the pod selector to that deployment:
```bash
LABEL_SELECTOR="app.kubernetes.io/component=vllm,app.kubernetes.io/instance=$DEPLOYMENT_NAME"
```

If that selector returns no pods, fall back to:
```bash
LABEL_SELECTOR=$(kubectl get deployment -n $NAMESPACE $DEPLOYMENT_NAME -o jsonpath='{.spec.selector.matchLabels}' | jq -r 'to_entries | map("\(.key)=\(.value)") | join(",")')
```

If they choose 'all', use the default selector:
```bash
LABEL_SELECTOR="app.kubernetes.io/component=vllm"
```

Confirm the pods that will be affected before proceeding:
```bash
kubectl get pods -n $NAMESPACE -l "$LABEL_SELECTOR" --field-selector=status.phase=Running -o wide
```

Show the pod list to the user and ask them to confirm before moving to Step 2.

---

## Step 2: Check Dev Mode Status

Run the dev-mode check script:
```bash
bash skills/reset-vllm-cache-in-llm-d-deployment/scripts/check-dev-mode.sh
```

This verifies whether `VLLM_SERVER_DEV_MODE=1` is set on the pods. The `/reset_prefix_cache` endpoint only exists when dev mode is enabled.

If dev mode is **not** enabled, inform the user:
- The preferred reset method requires `VLLM_SERVER_DEV_MODE=1` in the pod env
- Ask if they want to:
  1. **Enable dev mode** requires updating the deployment spec and waiting for pod rollout — this DOES restart pods
  2. **Use the flood fallback** no restart needed, but slower and less precise
  3. **Abort**

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

**Environment variables:**
| Variable | Default | Description |
|----------|---------|-------------|
| `NAMESPACE` | (required) | Kubernetes namespace |
| `VLLM_PORT` | `8000` | Port vLLM listens on inside the pod |
| `LABEL_SELECTOR` | `app.kubernetes.io/component=vllm` | Pod label selector |
| `RESET_RUNNING_REQUESTS` | `true` | Preempt running requests and free their KV blocks |
| `RESET_EXTERNAL` | `true` | Also reset external KV connector cache |

### Before running

**Warn the user:** if `reset_running_requests=true`, all in-flight requests will be preempted and clients will receive errors. Ensure no active traffic (benchmarks, users) is hitting the pods before resetting. If the user has active traffic, suggest setting `RESET_RUNNING_REQUESTS=false` (only clears the prefix cache, doesn't interrupt ongoing work).

### If reset succeeds

Report success and tell the user they can start benchmarking after a 2-second settling period.

### If reset fails

Show the user which pods failed and why. Common failure reasons:
- **404**: Dev mode not enabled on that pod
- **Connection refused**: Pod not ready or wrong port
- **Exec error**: RBAC permissions missing

Then ask the user: **"Reset failed on some pods. Would you like to try the random-prompt flood fallback instead?"**

---

## Step 4: Fallback — Flood with Random Prompts

Only run this if:
- The user explicitly chose the flood fallback, OR
- Step 3 failed and the user confirmed they want to proceed with flooding

Before running, gather the model name. Try multiple methods:
```bash
FIRST_POD=$(kubectl get pods -n $NAMESPACE -l "$LABEL_SELECTOR" -o jsonpath='{.items[0].metadata.name}')

# Method 1: env var
MODEL_NAME=$(kubectl exec -n $NAMESPACE $FIRST_POD -- printenv SERVED_MODEL_NAME 2>/dev/null || echo "")

# Method 2: query the models endpoint
if [ -z "$MODEL_NAME" ]; then
  MODEL_NAME=$(kubectl exec -n $NAMESPACE $FIRST_POD -- curl -s http://localhost:${VLLM_PORT}/v1/models 2>/dev/null | jq -r '.data[0].id // empty')
fi
```

If `MODEL_NAME` is still empty, ask the user for the served model name.

Then run the flood script:
```bash
export NAMESPACE="$NAMESPACE"
export VLLM_PORT="${VLLM_PORT:-8000}"
export MODEL_NAME="$MODEL_NAME"
export LABEL_SELECTOR="$LABEL_SELECTOR"
export NUM_FLOOD_REQUESTS="${NUM_FLOOD_REQUESTS:-50}"
export FLOOD_PROMPT_LENGTH="${FLOOD_PROMPT_LENGTH:-2000}"
bash skills/reset-vllm-cache-in-llm-d-deployment/scripts/flood-random-prompts.sh
```

**Additional environment variables for flood:**
| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_NAME` | (required) | The model served by vLLM |
| `NUM_FLOOD_REQUESTS` | `200` | Number of random requests per pod |
| `FLOOD_PROMPT_LENGTH` | `4000` | Character length of each random prompt |
| `FLOOD_MAX_TOKENS` | `1` | Max tokens to generate (keep low — we only care about filling KV) |
| `PARALLEL_JOBS` | `5` | Number of concurrent requests per pod |

**Note on cache size:** For large models (70B+) with high `gpu_memory_utilization`, the default 200 requests may not fully evict the cache. Increase `NUM_FLOOD_REQUESTS` or `FLOOD_PROMPT_LENGTH` if metrics still show cache hits after flooding.

After flooding completes, tell the user to wait 5 seconds before benchmarking.

---

## Step 5: Verification (Optional)

Check `kv_cache_usage_perc` — this is a live gauge (not a cumulative counter), so it reflects the actual current state of the cache without needing to send any requests:

```bash
kubectl exec -n $NAMESPACE <pod> -- \
  curl -s http://localhost:${VLLM_PORT}/metrics \
  | grep "vllm:kv_cache_usage_perc"
```

A value of `0.0` confirms the cache was fully cleared. A non-zero value means cached blocks are still occupied.

**Do not use `prefix_cache_hits_total` or `prefix_cache_queries_total` for verification** — these are Prometheus counters that accumulate since pod start and never reset, so they will always show the pre-reset values.

---

## Important Notes

- **NixL / LMCache connectors**: The `reset_external=true` flag is a no-op for nixl and LMCache connectors. If the llm-d deployment uses disaggregated prefill/decode with a shared KV store, the shared store will NOT be cleared. Inform the user of this limitation.
- **Security**: `VLLM_SERVER_DEV_MODE=1` exposes development endpoints. Recommend disabling it after benchmarking in production-like environments.
- **No pod restarts**: Neither method requires pod restarts. The prefix cache reset is immediate; the flood method takes longer but is equally non-disruptive.
- **In-flight requests**: When using `reset_running_requests=true`, any active inference requests are terminated. Always ensure no traffic is hitting the pods before resetting, or use `RESET_RUNNING_REQUESTS=false` to only clear the prefix cache without disrupting active work.

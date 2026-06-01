---
name: reset-vllm-cache-in-llm-d-deployment
description: Resets the KV cache on all vLLM pods in an llm-d deployment without restarting pods. Use this skill when the user wants to clear caches before proceeding, reset vLLM state, flush prefix cache, or get a clean deployment state for performance testing — even if they don't say "cache" explicitly. Supports both the /reset_prefix_cache API (preferred) and a random-prompt flood fallback.
---

# Reset vLLM Cache in llm-d

## Purpose

Clear the KV / prefix cache on all vLLM pods in a running llm-d deployment so the next run starts from a clean state — without restarting pods. This avoids the overhead of pod restart (model reload, warmup) while ensuring no cached prefixes skew results.

---

## Step 1: Ask for Namespace and Deployment

Before asking, detect the current namespace context and gather nearby options to present as suggestions:

```bash
# Current active namespace
CURRENT_NS=$(oc project -q 2>/dev/null || kubectl config view --minify -o jsonpath='{..namespace}' 2>/dev/null || echo "")

# Other namespaces that look llm-d related
SUGGESTED_NS=$(kubectl get namespaces -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
  | tr ' ' '\n' | grep -iE "llm-d|inference|serving|benchmark" | grep -v "^$CURRENT_NS$" | head -5)
```

Present the options to the user:
> "Which namespace is the llm-d deployment in?
>
> **1. \<CURRENT_NS\>** *(current context)*
> **2. \<first SUGGESTED_NS\>**
> **3. \<second SUGGESTED_NS\>**
> *(or type any namespace name)*"

Use `CURRENT_NS` as the default if the user just confirms or says "yes/this one". If they pick a number, use the corresponding namespace. If they type a name, use that.

Set `NAMESPACE` to their answer and proceed.

Once the namespace is confirmed, list the llm-d deployments in it:
```bash
kubectl get deployments -n $NAMESPACE -l app.kubernetes.io/part-of=llm-d -o custom-columns="NAME:.metadata.name,READY:.status.readyReplicas,DESIRED:.spec.replicas,AGE:.metadata.creationTimestamp"
```

If that returns nothing, try a broader search:
```bash
kubectl get deployments -n $NAMESPACE | grep -iE "llm-d|vllm"
```

Count the results. If there is **exactly one** deployment, use it automatically and inform the user:
> "Found one llm-d deployment: **\<DEPLOYMENT_NAME\>** — using it."

If there are **multiple**, show the list and ask:
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

If dev mode is **not** enabled, inform the user and offer three options:

> "Dev mode (`VLLM_SERVER_DEV_MODE=1`) is not enabled on these pods. How would you like to proceed?
>
> **1. Enable dev mode** — I will patch the deployment and roll out new pods now. The pod restart will wipe the KV cache as a side effect, so your cache is already clean when the pods come back up. **The skill will end here — no further reset step needed.**
> **2. Use flood fallback** — No restart needed. I'll flood the pods with random prompts to evict the cache via LRU. Slower and less precise.
> **3. Abort**"

**If the user chooses option 1 — Enable dev mode:**

Run:
```bash
kubectl patch deployment $DEPLOYMENT_NAME -n $NAMESPACE --type='json' \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"VLLM_SERVER_DEV_MODE","value":"1"}}]'

kubectl rollout status deployment/$DEPLOYMENT_NAME -n $NAMESPACE --timeout=300s
```

Wait for rollout to complete, then report:
> "Dev mode enabled. Pods restarted — KV cache is already cleared as a result of the pod restart. You can proceed now. **Future cache resets can use the faster `/reset_prefix_cache` endpoint without restarting pods.**"

**The skill ends here for option 1.** Do not proceed to Step 3 or Step 4.

**If the user chooses option 2**, proceed to Step 4 (flood fallback).
**If the user chooses option 3**, stop.

---

### What VLLM_SERVER_DEV_MODE=1 Does

`VLLM_SERVER_DEV_MODE=1` unlocks internal developer-only HTTP endpoints on the vLLM API server. By default, vLLM blocks these endpoints to prevent disruption or security exploits in production clusters. When enabled, it exposes several direct controls outside of the standard `/v1/` prefix, most notably:

- `/reset_prefix_cache` — instantly flushes the internal prefix cache without an engine reboot
- `/sleep` and `/wake_up` — invokes vLLM's Sleep Mode to offload weights and wipe KV blocks entirely

**Does enabling dev mode affect inference speed?** No. It only registers the developer API router paths during startup. It does not inject debug logging, change CUDA kernels, or degrade inference performance (TPS, TTFT).

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

Report success and tell the user they can proceed after a 2-second settling period.

### If reset fails

For each failed pod, diagnose and attempt to resolve automatically before involving the user:

**404 — endpoint not found (dev mode not enabled on that pod)**
The pod may have been created before the env var patch was applied. Verify:
```bash
kubectl get pod $POD -n $NAMESPACE -o jsonpath='{.spec.containers[0].env}' | grep VLLM_SERVER_DEV_MODE
```
If missing, patch the deployment and wait for rollout (same as Step 2 option 1). Since this restarts the pod, the KV cache is already cleared — report success and end the skill.

**Connection refused — pod not ready or wrong port**
Check pod status and readiness:
```bash
kubectl get pod $POD -n $NAMESPACE
kubectl logs $POD -n $NAMESPACE --tail=30
```
If the pod is starting up, wait for it to become Ready (up to 60s) and retry the reset once. If it stays unready, check for OOMKilled or CrashLoopBackOff in the logs and report the specific error to the user.

**kubectl exec error — RBAC / permissions**
Try an alternative approach — call the endpoint via port-forward instead of exec:
```bash
kubectl port-forward pod/$POD -n $NAMESPACE 18000:$VLLM_PORT &
PF_PID=$!
sleep 2
curl -s -w "\n%{http_code}" -X POST \
  "http://localhost:18000/reset_prefix_cache?reset_running_requests=true&reset_external=true"
kill $PF_PID
```
If port-forward also fails due to permissions, report to the user that RBAC prevents both exec and port-forward access.

**After attempting all automatic fixes:** if any pods are still failing, report only the unresolved pods and the specific blocker:
> "Could not reset cache on \<pod(s)\>: \<reason\>. Would you like to try the random-prompt flood fallback instead?"

**Last resort — suggest restarting the vLLM decoders:**
If the flood fallback is also declined or fails, suggest:
> "As a last resort, restarting the vLLM decoder pods will guarantee a clean KV cache. This will cause a brief model reload (~1-2 min). Run:
> ```bash
> kubectl rollout restart deployment/$DEPLOYMENT_NAME -n $NAMESPACE
> kubectl rollout status deployment/$DEPLOYMENT_NAME -n $NAMESPACE --timeout=300s
> ```"

---

## Step 4: Fallback — Flood with Random Prompts

> **How this method works and its verification limitation:**
> The flood works by sending many large unique random prompts to saturate the KV cache. Once the cache is full, vLLM's LRU policy evicts the oldest entries — including the ones from previous runs — to make room for the new random data. After flooding, the cache will be **near 100% full** of garbage data, not empty.
>
> This means `kv_cache_usage_perc` will be high (≈ 1.0) after a successful flood — **not 0.0**. There is no way to directly verify that specific old entries were evicted without resending the original prompts and checking for a cache miss, which would re-populate the cache. Instead, success is confirmed by verifying the cache reached saturation (≥ 0.9), which means the flood covered enough capacity to have displaced the old entries via LRU.

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
export NUM_FLOOD_REQUESTS="${NUM_FLOOD_REQUESTS:-200}"
export FLOOD_PROMPT_LENGTH="${FLOOD_PROMPT_LENGTH:-4000}"
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

After flooding completes, verify the cache reached saturation on each pod (up to 10 immediate retries):
```bash
for POD in $POD_NAMES; do
  SATURATED="no"
  for i in $(seq 1 10); do
    USAGE=$(kubectl exec -n $NAMESPACE $POD -- \
      curl -s http://localhost:$VLLM_PORT/metrics 2>/dev/null \
      | grep "vllm:kv_cache_usage_perc" | grep -v "HELP\|TYPE" | awk '{print $2}')
    [ -z "$USAGE" ] && echo "$POD: could not read metrics, retrying..." && continue
    SATURATED=$(awk "BEGIN {print ($USAGE >= 0.9) ? \"yes\" : \"no\"}")
    if [ "$SATURATED" = "yes" ]; then
      echo "$POD: cache saturated (usage=$USAGE) — old entries displaced"
      break
    fi
    echo "$POD: cache at $USAGE, not yet saturated — retrying..."
  done
  if [ "$SATURATED" != "yes" ]; then
    echo "WARNING: $POD cache only reached $USAGE after flooding — old entries may not be fully evicted. Consider increasing NUM_FLOOD_REQUESTS."
  fi
done
```

Only report success once `kv_cache_usage_perc ≥ 0.9` is confirmed on all pods. If a pod stays below 0.9 after all retries, report it to the user and suggest increasing `NUM_FLOOD_REQUESTS` or `FLOOD_PROMPT_LENGTH`.

---

## Step 5: Verification (Optional)

Check `kv_cache_usage_perc` — this is a live gauge (not a cumulative counter), so it reflects the actual current state of the cache without needing to send any requests:

```bash
kubectl exec -n $NAMESPACE <pod> -- \
  curl -s http://localhost:${VLLM_PORT}/metrics \
  | grep "vllm:kv_cache_usage_perc"
```

Interpret the result based on which method was used:
- **Step 3 (`/reset_prefix_cache`)**: expect `0.0` — the API clears all blocks immediately.
- **Step 4 (flood)**: expect `≥ 0.9` — the cache is full of random garbage, confirming old entries were displaced. `0.0` after a flood means the flood requests never populated the cache (wrong model name, endpoint error, etc.).

**Do not use `prefix_cache_hits_total` or `prefix_cache_queries_total` for verification** — these are Prometheus counters that accumulate since pod start and never reset, so they will always show the pre-reset values.

---

## Important Notes

- **NixL / LMCache connectors**: The `reset_external=true` flag is a no-op for nixl and LMCache connectors. If the llm-d deployment uses disaggregated prefill/decode with a shared KV store, the shared store will NOT be cleared. Inform the user of this limitation.
- **Security**: `VLLM_SERVER_DEV_MODE=1` exposes development endpoints. Recommend disabling it after benchmarking in production-like environments.
- **Pod restarts**: The `/reset_prefix_cache` API and flood fallback do not restart pods. However, enabling dev mode (Step 2 option 1) does trigger a pod restart — which also clears the cache as a side effect, so no further reset step is needed in that case.
- **In-flight requests**: When using `reset_running_requests=true`, any active inference requests are terminated. Always ensure no traffic is hitting the pods before resetting, or use `RESET_RUNNING_REQUESTS=false` to only clear the prefix cache without disrupting active work.

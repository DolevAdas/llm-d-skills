---
name: reset-vllm-cache-in-llm-d-deployment
description: Clears the KV / prefix cache on all vLLM pods in an llm-d deployment for a clean state. Use when the user wants to flush the cache, reset vLLM state, or start fresh before a test run — even if they don't say "cache" explicitly. Prefers the /reset_prefix_cache API; falls back to pod restart.
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
> **2. Restart pods now** — a plain `rollout restart` clears every tier without enabling dev mode (Step 4).
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
| `RESET_EXTERNAL` | `true` | Clears CPU offload blocks **only if** `CPUOffloadingSpec` is used (2-tier). With `TieringOffloadingSpec` (3-tier: GPU+CPU+memory): on **vLLM ≥ 0.24.0** (includes [PR #44541](https://github.com/vllm-project/vllm/pull/44541)) this drains in-flight transfers and resets the GPU primary tier — but secondary memory tier data **intentionally persists** (see Step 3b). On vLLM < 0.24.0 this is a no-op for 3-tier. No-op for NixL/LMCache connectors. |

**Warn the user before running:** `RESET_RUNNING_REQUESTS=true` terminates in-flight requests. Ensure no active traffic is hitting the pods.

**On success:** report done, user can proceed after 2 seconds.

**On failure** — auto-diagnose per pod before asking the user:

| Error | Auto-fix |
|-------|----------|
| **404** — dev mode missing on pod | Patch deployment + rollout (same as Step 2 option 1). Restart clears cache — end skill. |
| **Connection refused** | Check pod status/logs. Wait up to 60s for Ready, retry once. Report OOMKilled/CrashLoop to user. |
| **RBAC / exec error** | Try port-forward: `kubectl port-forward pod/$POD -n $NAMESPACE 18000:$VLLM_PORT &`, then curl `http://localhost:18000/reset_prefix_cache?...`. |

If still failing: *"Reset failed on \<pod(s)\>: \<reason\>. Restart the pods instead (Step 4)?"*

---

## Step 3b: Clear FS Secondary Tier (3-tier TieringOffloadingSpec, vLLM ≥ 0.24.0)

**Run this immediately after Step 3 succeeds when the deployment uses `TieringOffloadingSpec`.**

**How the reset works across all 3 tiers (verified with vLLM 0.24.0):**

| Tier | After Step 3 (API) | Needs Step 3b? |
|------|--------------------|----------------|
| GPU primary | Index + data cleared | No |
| CPU mmap (`/dev/shm/vllm_offload*.mmap`) | **Index cleared** by API — raw bytes remain in file, but the hash→block mapping is gone so no cache hits can be served from it | No (do NOT delete or zero this file while vLLM is running — it is mmap'd into the process) |
| FS secondary (`root_dir` path) | **Raw files persist** — the API does not delete them | **Yes** — needed to reclaim disk/memory space AND to prevent warm cache on pod restart when FS tier is PVC-backed |

Verify after Step 3: `vllm:external_prefix_cache_hits_total` should stay at `0` for new requests, confirming the CPU mmap index was cleared.

### 3b-1: Locate the FS secondary tier path

```bash
for POD in $POD_NAMES; do
  echo "=== $POD ==="
  kubectl exec -n "$NAMESPACE" "$POD" -- sh -c '
    for p in /mnt/kv-memory-tier /mnt/kv-cache /mnt/files-storage /tmp/vllm_tiering; do
      [ -d "$p" ] && echo "FS_TIER_PATH=$p" && du -sh "$p" 2>/dev/null && break
    done
    echo "CPU mmap (index cleared by API):"
    ls -lh /dev/shm/vllm_offload*.mmap 2>/dev/null || echo "(not found)"
  ' 2>/dev/null
done
```

### 3b-2: Clear the FS tier files

```bash
FS_TIER_PATH="<root_dir from TieringOffloadingSpec config>"
for POD in $POD_NAMES; do
  echo -n "Clearing FS secondary tier on $POD ... "
  kubectl exec -n "$NAMESPACE" "$POD" -- sh -c "rm -rf ${FS_TIER_PATH:?}/* && echo OK"
done
```

If no FS path is found (FS tier not configured or path is different): inspect the deployment's `--kv-transfer-config` arg for the `root_dir` value.

> **PVC-backed FS tier:** Run Step 3b before any pod restart to prevent the new pod from re-reading those files and starting warm. With tmpfs (`emptyDir medium: Memory`), the files disappear on pod death — Step 3b is only needed for in-session disk space reclaim.

---

## Step 4: Restart vLLM Decoder Pods

Use this step when:
- Step 3 failed or was declined (dev mode could not be enabled), OR
- The deployment runs **vLLM < 0.24.0** — the API cannot clear a 3-tier `TieringOffloadingSpec` cache, so a restart is the only way to wipe every tier.

You need a restart **only on vLLM < 0.24.0**: there the API is a no-op for 3-tier setups, so the CPU mmap data in `/dev/shm/vllm_offload*.mmap` and the secondary tiers persist until the pod restarts. On **vLLM ≥ 0.24.0**, Step 3 + Step 3b already gives a clean cache for benchmark runs — the CPU mmap index is cleared (verified by `vllm:external_prefix_cache_hits_total = 0`) and the FS tier files are deleted, so no restart is required. Model reload takes ~1-2 min.

```bash
kubectl rollout restart deployment/$DEPLOYMENT_NAME -n $NAMESPACE
kubectl rollout status deployment/$DEPLOYMENT_NAME -n $NAMESPACE --timeout=300s
```

Report success once rollout completes. The user can proceed immediately — no further verification needed.

---

## Step 5: Verification (Optional)

These are live gauges — they reflect current state without sending any requests:

```bash
POD=$(kubectl get pods -n $NAMESPACE -l "$LABEL_SELECTOR" --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n $NAMESPACE $POD -- \
  curl -s http://localhost:${VLLM_PORT}/metrics \
  | grep -E "vllm:kv_cache_usage_perc|vllm:cpu_kv_cache_usage_perc|vllm:external_prefix_cache_hits_total" \
  | grep -v "HELP\|TYPE"
```

| Method | Expected `kv_cache_usage_perc` | Expected `external_prefix_cache_hits_total` |
|--------|-------------------------------|---------------------------------------------|
| Step 3 (2-tier or 3-tier, vLLM ≥ 0.24.0) | `0.0` | `0.0` — CPU mmap index cleared |
| Step 3 + Step 3b (3-tier) | `0.0` | `0.0` + FS tier files deleted |
| Step 4 (pod restart) | `0.0` | `0.0` |

> Do **not** use `prefix_cache_hits_total` / `prefix_cache_queries_total` — these counters accumulate since pod start and never reset. Do **not** send requests to verify: that defeats the purpose of clearing the cache before a benchmark run.

---

## Important Notes

- **NixL / LMCache connectors**: `reset_external=true` is a no-op for these. The shared KV store in disaggregated prefill/decode setups will NOT be cleared.
- **CPU/memory offloading**: Step 3 with `reset_external=true` clears the CPU cache **only when `CPUOffloadingSpec` is used** (2-tier). For `TieringOffloadingSpec` (3-tier: GPU + CPU + memory), behavior is version-dependent:
  - **vLLM < 0.24.0**: `reset_external=true` is a no-op — neither GPU nor secondary tier is cleared. A pod restart (Step 4) is the only way to get a clean cache.
  - **vLLM ≥ 0.24.0** ([PR #44541](https://github.com/vllm-project/vllm/pull/44541)): the API drains in-flight transfers, resets the GPU primary tier, and clears the CPU mmap index (so no cache hits can be served from CPU data — verified by `vllm:external_prefix_cache_hits_total = 0`). Run Step 3b to also delete the FS secondary tier files. No pod restart is needed.
- **In-flight requests**: `reset_running_requests=true` terminates active requests. Use `false` to clear the cache without disrupting ongoing work.
- **Dev mode security**: `VLLM_SERVER_DEV_MODE=1` exposes internal endpoints. Disable after use in production-like environments.
- **Pod restarts**: Step 3 does not restart pods. Enabling dev mode (Step 2 option 1) does — but that clears the cache as a side effect, so no further reset is needed.

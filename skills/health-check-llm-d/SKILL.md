---
name: health-check-llm-d
description: Validates GPU health across an already-deployed llm-d stack by probing each inference pod directly with random requests and comparing time-to-first-token (TTFT) and time-per-output-token (TPOT) latency — both across pods (peers) and against each GPU's own past runs (history/drift). Use this skill whenever the user wants to verify GPUs are all working, check for slow or broken GPUs, detect GPU outliers or regressions, run a quick sanity check after deployment, confirm uniform inference performance, or investigate inconsistent latency — even if they don't say "health check" explicitly.
---

# Health Check llm-d Stack

## Purpose

Validate that all GPUs in a deployed llm-d stack are healthy and performing comparably by probing each vLLM inference pod with randomized requests and measuring **TTFT** (time-to-first-token — prefill health) and **TPOT** (time-per-output-token — decode health).

A pod is flagged in two independent ways:
- **vs peers** — its latency is a significant outlier among other pods in the same group (a single run).
- **vs history** — its latency has regressed relative to *the same GPU's own past runs*, recorded in a local per-cluster history file. This catches a slow GPU even in a **single-GPU deployment** where there are no peers to compare against.

This skill is **read-only** — it probes pods via temporary local port-forwards and (best-effort) reads GPU UUIDs; it never creates, patches, or deletes a cluster resource. History is stored **locally per user**; nothing is uploaded or shared.

---

## Step 1: Locate the Stack and Set NAMESPACE

Use the standard detection logic:

1. If the `NAMESPACE` environment variable is set, use it.
2. Otherwise run `oc project -q 2>/dev/null`.
3. If neither, ask the user.

Verify the stack is present and pods are Ready:
```bash
kubectl get pods -n $NAMESPACE
```

If pods are not all in `Running`/`Ready` state, stop and tell the user to wait for the stack to stabilize before running a health check.

---

## Step 2: Discover Inference Pods

Find all vLLM pods. Try in order until pods are found:
```bash
kubectl get pods -n $NAMESPACE -l app.kubernetes.io/component=vllm -o wide
kubectl get pods -n $NAMESPACE -l app=vllm -o wide
kubectl get pods -n $NAMESPACE -o wide | grep -i vllm
```

List the discovered pods to the user, noting:
- Whether this is a PD-disaggregated deployment (separate prefill + decode pods) or unified
- The node each pod runs on (from the `-o wide` NODE column) — this maps pods to physical GPUs

For PD-disaggregated deployments, probe **both** decode and prefill pods, but record which group each pod belongs to — prefill and decode pods have different latency baselines and must be compared **within their own group**, not against each other. You can distinguish them by pod name suffix (e.g., `-decode-`, `-prefill-`) or by label:
```bash
kubectl get pods -n $NAMESPACE -l app.kubernetes.io/role=decode -o wide 2>/dev/null
kubectl get pods -n $NAMESPACE -l app.kubernetes.io/role=prefill -o wide 2>/dev/null
```

Probing a pod directly (via its own port-forward) bypasses the disaggregation routing and exercises that pod's GPU on its own — which is exactly what we want for a per-GPU health check. Pass the group of each pod to the probe script via `--groups` (see Step 6).

> If no pods are found with any selector, ask the user how their vLLM pods are labeled. For a unified (non-PD) deployment, all pods share a single group and `--groups` can be omitted.

---

## Step 3: Determine the Model Name

Extract the model name from the first pod's environment variables:
```bash
kubectl get pod <first-pod> -n $NAMESPACE -o json | \
  python3 -c "
import sys, json
d = json.load(sys.stdin)
envs = {e['name']: e.get('value','') for c in d['spec']['containers'] for e in c.get('env',[])}
print(envs.get('MODEL_ID') or envs.get('MODEL_NAME') or envs.get('VLLM_MODEL','<not found>'))
"
```

If that returns empty, briefly port-forward the first pod to query `/v1/models`:
```bash
kubectl port-forward pod/<first-pod> 18000:8000 -n $NAMESPACE &
PF_PID=$!; sleep 2
curl -s http://localhost:18000/v1/models | python3 -c "import sys,json; print(json.load(sys.stdin)['data'][0]['id'])"
kill $PF_PID 2>/dev/null; wait $PF_PID 2>/dev/null
```

Show the detected model name to the user and confirm before proceeding.

---

## Step 4: Resolve Stable GPU Identities (for history)

Pod names are **ephemeral** (regenerated on every restart), so history must key on a stable GPU identity. Build a `GPU_IDS` array parallel to `PODS`: use the **node name**, enriched with **GPU UUID(s)** when `nvidia-smi` exec is permitted (it is in stock vLLM images). If exec is not allowed or `nvidia-smi` is missing, fall back to the node name alone.

> **Shell note (verified on a real run):** the snippets below use bash arrays (`arr+=(...)`, `"${arr[@]}"`, process substitution). The user's login shell may be **zsh**, so run each block under bash — e.g. wrap it in `bash <<'EOF' … EOF`. Two gotchas that bit this skill in testing: (1) never name an array `GROUPS` — it's a reserved bash variable holding your OS group IDs, so it silently won't be your pod list (this skill uses `POD_GROUPS`); (2) avoid `mapfile` (absent in macOS's bash 3.2) — populate arrays with `while IFS= read -r p; do PODS+=("$p"); done < <(…)`.

```bash
PODS=(<pod1> <pod2> ...)   # same list you will probe
GPU_IDS=()
for pod in "${PODS[@]}"; do
  NODE=$(kubectl get pod "$pod" -n $NAMESPACE -o jsonpath='{.spec.nodeName}' 2>/dev/null)
  # Best-effort: exact per-GPU UUID(s). Silently falls back to node name if exec is denied.
  UUIDS=$(kubectl exec "$pod" -n $NAMESPACE -- nvidia-smi --query-gpu=uuid --format=csv,noheader 2>/dev/null \
            | paste -sd, - | tr -d ' ')
  if [ -n "$UUIDS" ]; then
    GPU_IDS+=("${NODE}:${UUIDS}")
  else
    GPU_IDS+=("${NODE:-$pod}")
  fi
done
printf 'GPU identity: %s\n' "${GPU_IDS[@]}"
```

Also set the history file location. It is a **local, per-user** JSON file keyed by the current cluster context:
```bash
CLUSTER=$(kubectl config current-context 2>/dev/null | tr '/:@ ' '____')
HISTORY_DIR="${LLMD_HEALTH_HISTORY_DIR:-$HOME/.llm-d-health-check}"
HISTORY_FILE="${HISTORY_DIR}/${CLUSTER:-unknown}.json"
```

> The first one or two runs on a new cluster only *build* the baseline — drift detection needs at least 2 prior HEALTHY runs of a GPU before it will flag. Tell the user this so an all-HEALTHY first run isn't mistaken for "history checked."

---

## Step 5: Set Health Check Parameters

Confirm these parameters with the user, using defaults if they don't want to change anything:

| Parameter | Default | Description |
|-----------|---------|-------------|
| Requests per pod | 8 | Inference requests sent to each pod (plus 1 discarded warmup) |
| Max tokens | 50 | Max tokens generated per request (shorter = faster check) |
| Peer threshold | 2.0× | Flag a pod if its mean TTFT **or** TPOT exceeds this multiple of its group median |
| Drift threshold | 1.5× | Flag a pod if its TTFT/TPOT exceeds this multiple of its own historical baseline |
| API type | `chat` | `chat` for instruct/chat models; `completions` for base models with no chat template |
| History | on | Record this run and compare against past runs. Omit `--history` to disable |

The probe sends the **same** prompt set to every pod (fair comparison), discards one warmup request per pod (avoids cold-start bias), and measures both TTFT and TPOT. The drift threshold is tighter than the peer threshold because a GPU compared against its own past is far more stable than across different pods.

With 8 requests and 4 concurrent per pod, a full check across a handful of pods typically completes in under 3 minutes.

---

## Step 6: Run Health Probes

Use `scripts/gpu-health-probe.py` located alongside this skill file.

### 6a: Start port-forwards (one per pod)

Assign a unique local port starting at 18001. Start each port-forward in the background and record its PID (reuse the `PODS` array from Step 4):

```bash
PF_PIDS=()
LOCAL_PORTS=()
# One group label per pod, SAME order as PODS. e.g. (decode decode prefill) for PD; leave empty for unified.
# NOTE: do NOT name this array GROUPS — GROUPS is a reserved bash variable (your OS group IDs) and will not behave as a normal array.
POD_GROUPS=(<group1> <group2> ...)
PORT=18001

# Portable loop (no ${!PODS[@]} / 0-index assumptions, which break under zsh):
for pod in "${PODS[@]}"; do
  kubectl port-forward "pod/$pod" ${PORT}:8000 -n $NAMESPACE >/dev/null 2>&1 &
  PF_PIDS+=($!)
  LOCAL_PORTS+=($PORT)
  PORT=$((PORT + 1))
done

# Wait for tunnels to establish
sleep 5
```

> If the container serves on a port other than 8000, adjust the `:8000` target. Confirm with `kubectl get pod <pod> -n $NAMESPACE -o jsonpath='{.spec.containers[*].ports[*].containerPort}'` if unsure.

### 6b: Run the probe script

Substitute the absolute path to this skill's `scripts/` directory. Pass `--gpu-ids` (from Step 4) and `--history` to enable drift tracking. For a PD deployment, pass `--groups`; omit it for unified. Add `--api completions` if the model has no chat template.

```bash
python3 /abs/path/to/skills/health-check-llm-d/scripts/gpu-health-probe.py \
  --endpoints $(printf "http://localhost:%s " "${LOCAL_PORTS[@]}") \
  --pod-names "${PODS[@]}" \
  --gpu-ids "${GPU_IDS[@]}" \
  --groups "${POD_GROUPS[@]}" \
  --model "$MODEL_NAME" \
  --requests $REQUESTS \
  --max-tokens $MAX_TOKENS \
  --threshold $PEER_THRESHOLD \
  --drift-threshold $DRIFT_THRESHOLD \
  --history "$HISTORY_FILE" \
  --cluster "$CLUSTER"
PROBE_EXIT=$?
```

The script exits `0` (all healthy), `1` (one or more pods flagged), or `2` (fatal — e.g., no pod responded; check the model name and `--api`). It records this run to `$HISTORY_FILE` on completion (unless the file is corrupt, in which case it warns and skips recording to avoid data loss).

### 6c: Kill all port-forwards

```bash
for pid in "${PF_PIDS[@]}"; do
  kill $pid 2>/dev/null
done
wait "${PF_PIDS[@]}" 2>/dev/null
```

Always kill port-forwards even if the probe script fails.

---

## Step 7: Report Results and Recommend Actions

Present the probe script's health table to the user. Each status is one of:

- **HEALTHY** — TTFT and TPOT are within the normal range vs peers and vs this GPU's own history.
- **SUSPICIOUS** — an outlier. The status line names the signal and *which comparison*:
  - `vs peers` — slower than other pods in the same group this run.
  - `vs history` — slower than this same GPU's own past runs (a regression). This fires even for a lone pod with no peers.
  - A high **TTFT** points to a slow prefill/compute path; a high **TPOT** points to slow token generation (decode). Either can indicate a throttled GPU, a hardware fault, or a competing workload sharing the GPU.
- **UNHEALTHY** — the pod returned errors on all probes. GPU may be non-functional, the pod misconfigured, or (if *every* pod is UNHEALTHY) the model name or `--api` type is wrong.

The **History baselines** section lists each GPU's current-vs-baseline latency so the user can see the trend even when nothing is flagged. If a pod shows "no prior healthy history — baseline starts now" or "need 2 for drift check", explain that history is still being built for that GPU.

If the script prints a **"fewer than 3 responsive pods"** note for a group *and history is not yet established*, tell the user peer-based detection is unreliable for that group and history-based drift detection is the reliable path once a baseline exists.

**If any pods are SUSPICIOUS or UNHEALTHY**, suggest the following investigation steps:

1. Check for GPU-level errors in pod logs:
   ```bash
   kubectl logs -n $NAMESPACE <pod> | grep -iE "cuda|gpu|error|OOM|exception|failed"
   ```

2. Check node-level GPU resource allocation:
   ```bash
   kubectl describe node <node-name> | grep -A10 "Allocated resources"
   ```

3. Check if a GPU is being shared or throttled:
   ```bash
   kubectl get pod <pod> -n $NAMESPACE -o json | \
     python3 -c "import sys,json; d=json.load(sys.stdin); \
     [print(c['name'], c.get('resources',{})) for c in d['spec']['containers']]"
   ```

4. **STOP and ask the user before doing anything.** Present your findings and explicitly ask:
   > "Pod `<pod>` is flagged as SUSPICIOUS. Should I restart it?"
   Wait for the user's explicit approval before proceeding. Never restart automatically.

   If the user approves, restart the flagged pod and re-run the health check:
   ```bash
   kubectl rollout restart deployment/<deployment-name> -n $NAMESPACE
   ```

---

## Step 8 (Optional): Reset KV Cache

After reporting results, ask the user:

> "Would you like to reset the KV cache on all pods now? This is useful before a benchmark run to ensure a clean cache state."

**Wait for explicit confirmation before proceeding.** If the user says yes, invoke the `clear-kv-cache-tiers-in-llm-d-deployment` skill — it handles all cache tier variants (GPU-only, GPU+CPU, 3-tier GPU+CPU+FS) and both unified and disaggregated PD deployments:

```
/clear-kv-cache-tiers-in-llm-d-deployment
```

Pass the `NAMESPACE` already resolved in Step 1 so the cache-reset skill does not need to re-ask. If the user says no or does not respond, skip this step entirely — it is never run automatically.

---

## Execution Rules

1. **Read-only** — do not create, patch, or delete any Kubernetes resource. The only write is to the local history file.
2. **Always kill port-forwards** — track PIDs and clean them up in Step 6c, even on failure.
3. **Scope to $NAMESPACE** — no operations outside the target namespace.
4. **Handle individual pod failures gracefully** — if one port-forward dies early, record that pod as failed and continue probing the rest.
5. **Show live progress** — print each pod name as probing begins.
6. **GPU-id resolution is best-effort** — never fail the check because `nvidia-smi` exec was denied; fall back to the node name.

---

## What Not To Do

1. **Do NOT restart pods** — report findings only; the user decides on remediation.
2. **Do NOT modify any cluster resource** — this skill is diagnostic, not repair.
3. **Do NOT commit or upload the history file** — it contains internal node/cluster names. It is per-user and local by design. (A future shared-baseline feature would require explicit anonymization and opt-in.)
4. **Do NOT confuse this with benchmarking** — for full throughput/latency benchmarks, use the `run-llm-d-benchmark` skill instead. This skill is a quick pass/fail GPU sanity check.

---

## When to Use This Skill

- After deploying llm-d to verify all GPUs came up healthy
- When inference latency is inconsistently high (possible GPU outlier)
- To detect a GPU that has **regressed** since a previous run (drift), even on a single-GPU deployment
- Before running a benchmark to confirm all GPUs are at baseline
- After a node maintenance event or GPU driver update
- As a periodic cluster health check that accumulates a per-GPU baseline over time

---

## Prerequisites

- `kubectl` configured with access to the cluster
- Python 3.6+ available locally (stdlib only — no pip installs needed)
- The llm-d stack must already be deployed with all pods in `Running` state
- Optional: permission to `kubectl exec` into pods for exact GPU-UUID identity (falls back to node name otherwise)

---

## Security Considerations

- All operations are scoped to the target namespace
- Port-forwards are bound to localhost only and cleaned up after the check
- No cluster-level changes
- No credentials or model weights are accessed — only the inference HTTP API and (best-effort) `nvidia-smi --query-gpu=uuid`
- The history file is written under `$HOME/.llm-d-health-check` (override with `LLMD_HEALTH_HISTORY_DIR`) and contains node/cluster names — keep it local; do not commit it to a shared repo without anonymizing

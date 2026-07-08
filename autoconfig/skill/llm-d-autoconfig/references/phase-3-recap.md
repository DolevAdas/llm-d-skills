# Phase 3 — Recap and handoff (BLOCKING confirmation)

*Detailed runbook for SKILL.md Phase 3. Includes the schedulability audit + recap template. Run before calling the script in Phase 4.*


**Hard rule: DO NOT call the script in Phase 4 until the user has explicitly confirmed this recap. "yes", "looks good", "go", "proceed" all count. Silence or a follow-up clarification does NOT count. If the user corrects any field, repeat Phase 3 with the corrected recap and ask again.**

### Schedulability audit (BLOCKING for greenfield; informational for existing-pods)

**Branch on `context.modelserver_deploy_planned`** (set from Phase 2 Q0 — "configure for existing pods" = false, "deploy new" or no existing pods = true).

**If `modelserver_deploy_planned = false` (existing pods):** SKIP the density math entirely. Autoconfig is only deploying the EPP + Gateway + InferencePool wiring; the model servers are already running and their schedulability is the user's pre-existing operating state.

Replace the audit with one informational line in the recap, sourced from Phase 1's per-deployment extraction:

> "Schedulability: configuring for existing model server pods. Their current state: `<N>` total replicas × `<TP>` GPUs/pod = `<X>` GPUs requested vs `<Y>` GPUs available in the cluster (pre-existing — autoconfig isn't changing this)."

If the existing deploy is oversubscribed (`X > Y`), note it but do NOT block. The user already knows; surfacing it again is courtesy, not a gate.

**If `modelserver_deploy_planned = true` (greenfield or new deploy):** Run the full density math below.

---

Before showing the recap, compute density math against the per-node allocatable values you captured in Phase 1. The agent does this math itself; the script doesn't.

**Pod resource defaults** (from the modelserver overlays autoconfig deploys; per pod, per-role):

| Topology / role | CPU (req) | Memory (req) | GPU |
|---|---|---|---|
| Agg (optimized-baseline `gpu/vllm/`) | ~16 | ~64 Gi | tp |
| PD prefill (`pd-disaggregation` base) | 8 | 16 Gi | prefill_tp |
| PD decode (`pd-disaggregation` base) | 16 | 64 Gi | decode_tp |

If the user picks a non-default model size or tp, the memory request scales roughly linearly with `tp` — adjust accordingly.

**Density math:**
1. Compute `pods_per_node` per role from the topology and the GPU constraint:
   - `pods_per_role_per_node = floor(node_gpus / role_tp)`
2. For each role, sum the per-pod CPU + RAM requested at that density:
   - `cpu_demand_per_node = pods_per_node * pod_cpu_request`
   - `mem_demand_per_node = pods_per_node * pod_memory_request`
3. Compare to the smallest GPU node's allocatable CPU + RAM:
   - `cpu_overcommit = cpu_demand_per_node / node_allocatable_cpu`
   - `mem_overcommit = mem_demand_per_node / node_allocatable_memory`

**Decision:**
- If both overcommit ratios ≤ 0.85 → quiet pass; mention briefly in recap ("Schedulability: pods will fit at <X> per node, <Y>% of node memory, <Z>% of node CPU").
- If either > 0.85 but ≤ 1.0 → **YELLOW**: surface the math in recap and ask the user to confirm the tight headroom is intentional.
- If either > 1.0 → **RED, BLOCKING**: STOP. Do NOT show the normal recap. Surface the math, propose two concrete fixes (smaller replicas count OR larger TP that reduces pods_per_node), and let the user pick. Don't auto-pick.

**Worked example.** Qwen3-32B, PD with prefill_tp=2, prefill_replicas=20, decode_tp=2, decode_replicas=6 → 4 pods/node × 64 Gi = 256 Gi/node demanded. On `a3-mega-8g` (~1864 Gi allocatable) → 14% (pass). On `a3-highgpu-8g` (~880 Gi) → 29% (pass). On a smaller node profile with ~166 Gi allocatable → 154% (RED, blocking). Always check the smallest GPU node SKU in the user's pool, not the typical one.

### Recap

Give the user a clean summary in ONE block:

> "Here's what I've gathered — please confirm before I generate:
> - Model: Qwen/Qwen3-32B (context length 32768)
> - Cluster: prod-chat namespace, 16 H100 GPUs, RDMA-capable: no
> - Deploy mode: gateway, provider: gke-l7-rilb (you picked this in Q8.5)
> - Topology: agg, 8 replicas × TP=2 (uses all 16 GPUs)
> - SLA: TTFT 800ms, TPOT 25ms (you provided these)
> - Workload: ISL ~1000, OSL ~500, prefix-share low
> - Features: predictor on, precise-prefix-cache off
> - Workload knowledge: no multi-turn / LoRA / heterogeneous / HA
> - Recommendation (from Phase 2.5 doc reads): predicted-latency-slo plugin set per `predicted-latency-slo.values.yaml` (cited).  Summary: \"<one-line Phase 2.5 summary verbatim>.\"
>
> Type 'yes' to generate, or tell me what to change."

The "Deploy mode" line MUST attribute the provider to the user's Q8.5 answer ("you picked this") rather than presenting it as a fact about the cluster. If `DEPLOY_MODE = standalone`, drop the provider clause: just `"Deploy mode: standalone"`.

For PD topology, replace the Topology line with both the per-pool sizes and the transport — make the transport explicit since it implies a perf disclaimer:

> "- Topology: disagg (PD), prefill 8×TP=1 + decode 2×TP=4 (16 GPUs total), transport: TCP fallback (cluster has no RDMA — PD perf will be limited; benchmark vs agg before committing)"

Loop until the user explicitly says yes.

---

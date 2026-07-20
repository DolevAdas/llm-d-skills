# Supported Scope

What the autoconfig POC (v0.3.1) does and doesn't support, mapped against the published guides in [`llm-d/guides/`](https://github.com/llm-d/llm-d/tree/main/guides). The skill tracks upstream `main` (the `llm-d-router` chart and its router-schema guide values live there). Use this as a quick reference when scoping work or answering "can this configure X?" questions.

**Tracks `main`** via `autoconfig_poc.py` defaults: `_LLM_D_REF` / `_LLM_D_ROUTER_REF` = `main`, `_ROUTER_CHART_DEFAULT_VERSION` = `v0` (the rolling `llm-d-router` `-dev` OCI tag). Override at runtime with `--llm-d-ref` / `--llm-d-router-ref` / `--chart-version`. `main` is a moving target, so re-verify after large upstream refactors.

---

## At a glance

| Guide | Status | EPP config | Modelserver / cluster-side |
|---|---|---|---|
| `optimized-baseline` | ✅ **Supported** | Full (verbatim parity with `optimized-baseline.values.yaml`) | Kustomize overlay (Phase 6.3) |
| `pd-disaggregation` | ✅ **Supported** | Full (two scheduling profiles, NIXL transport, RDMA pod-patch) | Kustomize overlay + per-side patches (Phase 6.3 PD path) |
| `precise-prefix-cache-routing` | ✅ **Supported** | Full (auto-on when `prefix_share=high`; correctness inputs surfaced) | n/a |
| `workload-autoscaling` (WVA / HPA) | ✅ **Supported** | `autoscaler: wva` emits `VariantAutoscaling` CR; `hpa` emits `HorizontalPodAutoscaler` | WVA **operator** install is separate (advisory warning emitted) |
| `predicted-latency-routing` | ✅ **Supported** (agg) | Full parity with `predicted-latency-slo.values.yaml` (plugin set, named affinity filters, `weighted-random-picker`, `streamingMode`, metrics data layer); chart toggle `router.latencyPredictor.enabled=true` deploys sidecars | Under PD, only the producer is wired (no SLO admitter) |
| `tiered-prefix-cache` | 🟡 **Partial** | Comment-only advisory (overlay forks by tier/accelerator/connector) | Modelserver overlay is NOT auto-rendered; user picks + applies |
| `wide-ep-lws` | 🟡 **Partial** | Comment-only advisory (overlay forks by accelerator/infra) | LeaderWorkerSet topology is modelserver-side; user picks + applies |
| `flow-control` | 🟡 **Partial** | `enable_flow_control` flag surfaces an advisory; agent adds plugins via `recommendation.plugins` | Full multi-tenant priority/saturation config not auto-emitted |
| `asynchronous-processing` | ❌ **Not supported** | Advisory scaffold only | Separate deployment pattern (queue + workers) |
| `batch-gateway` | ❌ **Out of scope** | Different component (own API server, processor, GC, storage) | Composes with our EPP routing if installed upstream |

✅ = end-to-end deploy works · 🟡 = config rendered but operational pieces are deferred or partial · ❌ = not handled

---

## Per-guide notes

### `optimized-baseline` ✅

Aggregated topology, vLLM/SGLang. Load-aware + prefix-cache-aware balancing.

- Plugins: `queue-scorer`, `kv-cache-utilization-scorer`, `prefix-cache-scorer` (with `autoTune` + workload-bounded `maxPrefixTokensToMatch`), `no-hit-lru-scorer`.
- Profile weights: `queue=2`, `kvcache=2`, `prefix=3`, `no-hit-lru=2` — verbatim from `optimized-baseline.values.yaml`.
- Bundle: helm-templated llm-d-router chart + Gateway/HTTPRoute (gateway mode) + HF Secret scaffold.
- Test guard: `test_weights_match_optimized_baseline_yaml` in `tests/test_poc.py`.

### `pd-disaggregation` ✅

Prefill/decode split with KV transfer over NIXL (RDMA or TCP fallback). Best for very long prompts where prefill dominates.

- Two scheduling profiles (`prefill` + `decode`) with separate plugin sets + weights, verbatim from `pd-disaggregation.values.yaml`.
- Per-side knobs in input JSON: `topology.prefill_replicas / prefill_tp / decode_replicas / decode_tp / pd_transport`.
- Modelserver overlay: `pd-disaggregation/modelserver/gpu/vllm/gke/` (per-cluster choice between `gke`, `coreweave`, or `base` TCP-only).
- RDMA path: agent fetches the cluster's `Network` resource names and patches both prefill + decode deployments with NIC annotations + per-NIC resource requests + topology-aware podAffinity. TCP path skips this.
- Caveat: silent ~40× degradation if RDMA is intended but the cluster setup is broken — runbook Phase 6.0 + 6.3 explicitly validates.

### `precise-prefix-cache-routing` ✅

KV-events-driven precise prefix-cache scoring. Higher hit rates at high throughput.

- Auto-on when `prefix_share=high`; also opt-in via `enable_precise_prefix_cache=true`.
- Swaps `prefix-cache-scorer` for `precise-prefix-cache-scorer`, adds `no-hit-lru-scorer`.
- Correctness inputs surfaced via `unresolved_questions[]`: `vllm_block_size` MUST match vLLM's `--block-size`, `vllm_hash_seed` MUST match `PYTHONHASHSEED` — otherwise the cache silently misses.

### `workload-autoscaling` ✅ (with one cluster-side install)

- `features.autoscaler=wva` → bundle includes a `VariantAutoscaling` CR alongside the EPP/gateway resources.
- `features.autoscaler=hpa` → bundle includes a `HorizontalPodAutoscaler` on the `epp_queue_depth_avg` Pods metric.
- The **WVA operator** itself is not installed by autoconfig; the runbook (Phase 6.4) surfaces this as a prereq advisory.
- HPA path requires Prometheus Adapter or a custom-metrics-apiserver serving the metric; runbook flags this.

### `predicted-latency-routing` ✅ (agg)

For the aggregated SLO path, the generated config is at full parity with the canonical `predicted-latency-slo.values.yaml` — verified plugin-for-plugin against upstream. We emit:
- `queue-scorer`, `kv-cache-utilization-scorer`, `prefix-cache-scorer`
- `metrics-data-source` (with `insecureSkipVerify` / `path` / `scheme`) + `core-metrics-extractor`, emitted explicitly
- `predicted-latency-producer` with `streamingMode: true`
- both named `prefix-cache-affinity-filter` instances — `strict-affinity-filter` (0.99) and `loose-affinity-filter` (0.8)
- `latency-scorer` (with computed `ttftWeight` / `tpotWeight` from SLA + OSL when provided)
- `weighted-random-picker`
- `slo-headroom-tier-filter` + `latency-slo-admitter`

The default profile matches canonical order. The chart toggle `router.latencyPredictor.enabled=true` deploys the training + prediction sidecars in-pod and sets `TRAINING_SERVER_URL` / `PREDICTION_SERVER_URL` env vars on the EPP.

**Remaining limitation — PD + predictor:** under disagg, we wire `predicted-latency-producer` (PD-aware via `endpointRoleLabel`) but not the full SLO stack, since upstream publishes no canonical for the predicted-latency-SLO + PD combination. `latency-slo-admitter` is not added in that case.

**Inherent upstream caveats (not our gaps):** streaming-only requirement, homogeneous-pool requirement, prediction-sidecar throughput cap, untested with LoRA / speculative decoding / beam search.

### `tiered-prefix-cache` 🟡

`features.enable_tiered_cache=true` emits a comment-only advisory in the bundle (and a warning), not an applicable resource. The upstream overlay forks by tier (CPU RAM vs disk/shared storage), accelerator, and connector (`base` / `lmcache-connector` / `offloading-connector`), so there's no single overlay the script can pick for the user. The advisory names the choice and points at `guides.tiered_prefix_cache` with an example path; the user applies the chosen overlay after the modelservice base. EPP-side impact is a single tunable (`lruCapacityPerServer`); the heavy lift is modelserver-side and is not auto-rendered.

### `wide-ep-lws` 🟡

`features.enable_wide_ep=true` emits a comment-only advisory in the bundle (and a warning), not an applicable resource. Wide-EP needs LeaderWorkerSet and only makes sense for sparse-MoE models (DeepSeek, Mixtral, etc.); the script doesn't auto-detect MoE, so the user opts in. The upstream overlay forks by accelerator and infra (`base` / `gke` / `coreweave` / ...), so the advisory names the choice and points at `guides.wide_ep_lws` with an example path rather than picking one. EP/TP/DP topology suggestion is not implemented — the user picks.

### `flow-control` 🟡

`features.enable_flow_control=true` emits an advisory warning telling the agent to add flow-control plugins (saturation detector, ordering policy, top-level `flowControl` block). The script does NOT auto-render these — the agent is expected to read `guides.flow_control` from feature_docs in Phase 2.5 and synthesize them. This is the same recommendation-via-doc-read pattern the rest of the skill uses, just without a pre-baked scaffold.

### `asynchronous-processing` ❌

Different architecture entirely (queue + workers, not request-response). Doesn't write `EndpointPickerConfig`. `serving_pattern=async` emits a comment scaffold only.

### `batch-gateway` ❌

Separate `llm-d-incubation/batch-gateway` component with its own API server, processor, garbage collector, and storage layer (PostgreSQL/Redis/S3). Composes with EPP routing if installed but the deployment is wholly separate. `serving_pattern=batch` emits a comment scaffold.

---

## Deployment lifecycle coverage

| Step | Coverage |
|---|---|
| Install | ✅ Phase 6 walks pre-flight, prereq install, namespace + secrets, modelservice, EPP, gateway/HTTPRoute |
| Verify | ✅ Phase 6.6 smoke test (port-forward in standalone mode, gateway IP in gateway mode) |
| Benchmark | ✅ Phase 7 + `benchmark.py`; bundle includes `autoconfig-benchmark.yaml`; optionally `autoconfig-benchmark-job.yaml` with `--bench-target` + `--bench-namespace` |
| Customize | ✅ Implicit — the script generates customized values rather than asking the user to write a Kustomize overlay |

---

## Cross-cutting non-features

Boundaries that aren't tied to a single guide:

| Capability | Status |
|---|---|
| Modelservice values generation | ❌ — we emit EPP/gateway/Phase B resources only; modelserver overlays come from upstream Kustomize |
| Multi-pool / heterogeneous-pool routing | ❌ — single homogeneous pool only |
| Multi-cluster / federation | ❌ — single-cluster targeting |
| Per-tenant or per-model-variant routing | ❌ — one model per recommendation |
| Authoring custom plugins | ❌ — recommendation picks from registered plugin types only |
| Live re-tuning from observed metrics | ❌ — static one-shot recommendation; the latency predictor is the closest existing piece |

---

## `simulated-accelerators` and `recipes/`

- **`simulated-accelerators`** — out of scope as a workload class, but useful for our own CI (CPU-only model server simulator; validates generated configs end-to-end without burning GPU budget).
- **`recipes/`** — pre-tuned configs for specific model families. Our recommendations could cite these as the source for some T3-tier values; currently we hand-code constants. Cosmetic only.

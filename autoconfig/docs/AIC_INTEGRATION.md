# AIConfigurator Integration Design

> **Status:** Design proposal, not implemented. Captures the structure for a future integration.
> **Author/owner:** TBD
> **Last updated:** 2026-05-27

This doc describes how to integrate NVIDIA's [AIConfigurator](https://github.com/ai-dynamo/aiconfigurator) (AIC) into the `llm-d-autoconfig` skill so that the agent can size the model server topology before configuring the EPP scheduler.

## Why integrate AIC

The current POC **deploys** model servers (Phase 6.3 walks `kubectl apply -k` against per-accelerator kustomize overlays — `gpu/vllm`, `amd/vllm`, `tpu-v6/vllm`, etc. — with post-deploy patches for model + tp + replicas + GPU limits) but does NOT **size** them. We ask the user for `(topology.mode, replicas, tp)` directly and trust their answer. PD-disagg is fully supported when the user provides `prefill_replicas` / `prefill_tp` / `decode_replicas` / `decode_tp` / `pd_transport`, but the choice between agg and disagg — and the choice of TP within each — is left to the user.

**AIC fills the sizing gap.** It already takes the same `(model, GPU count, GPU type, ISL/OSL, TTFT/TPOT)` inputs, runs a search over thousands of `(architecture × parallelism × batch × prefill/decode split × replicas)` candidates against a measured perf database, and emits Pareto-best deployments. Critically, it has a `--deployment-target llm-d` mode that produces Helm values for `llm-d-modelservice` directly — meaning AIC's output is already consumable by llm-d's deploy path.

By delegating topology decisions to AIC, we:

- Stop asking users "what TP?" and start telling them "AIC suggests TP=2 × 8 replicas; here's why."
- Stop asking users "agg or disagg?" — AIC chooses based on perf data. We already know how to render either; AIC tells us which to render.
- Replace the generic per-accelerator Kustomize overlays (which hardcode `Qwen3-32B` + `TP=2` and rely on post-deploy patching) with AIC-generated Helm values tuned for the user's exact request.
- Unlock quantization-aware tuning (AIC reports FP8 vs BF16) which feeds back into our prefix-cache LRU sizing.
- Coordinate two tools doing what each is best at: AIC sizes the model server, our recommender sizes the EPP. No reinvention.

## Why we're not building AIC ourselves

AIC has an operation-level performance database with measured kernel times (GEMM, attention, KV cache, MoE, NCCL collectives, P2P) per (GPU type, backend, version). Building this for ourselves would be many engineer-months and would duplicate work that's already openly available. The right move is to depend on AIC for what it does well rather than reinvent.

## Integration architecture: Option C (skill-driven sub-flow)

We considered three integration options:

| Option | Description | Verdict |
|---|---|---|
| **A. SubProcess** | Agent invokes `aiconfigurator cli default ...` and parses the output YAMLs in `--save-dir/` | Too coupled to AIC's filesystem layout; brittle |
| **B. Python library** | Our autoconfig script imports `aiconfigurator.sdk` and calls it directly | Adds heavy hard dep (XGBoost, hundreds of MB perf data); complicates testing |
| **C. Skill sub-flow** | Skill has a Phase A.* sub-flow; agent runs AIC CLI, captures output, uses it to pre-fill subsequent phases | **Selected** |

Option C keeps AIC optional, matches our existing pattern for the latency predictor (also an optional sub-component with its own deploy story), and doesn't burden every skill user with the AIC install.

## Skill phase structure with AIC

The current runbook has Phases 1 → 7. AIC inserts a new sub-flow between Phase 2 (discovery questionnaire) and Phase 2.5 (doc-driven synthesis). Naming the new sub-phases `A.0`–`A.4` avoids collision with the existing `2.5` and signals "AIC-only branch."

```
Phase 1 — Cluster discovery (unchanged)
Phase 2 — Discovery questionnaire (unchanged: model, GPUs, namespace, context length, …)

Phase A.0 — NEW: branch on whether to use AIC
   "Want help sizing the model server topology?"
   - User says no → skip to Phase 2.5 (current flow; user-provided tp/replicas)
   - User says yes → enter Phase A.1

Phase A.1 — AIC compatibility check (read-only, fast)
   $ aiconfigurator cli support --model <id> --system <gpu_system>
   - If unsupported (no perf data for this combo): fall back to Phase 2.5 with a
     "AIC doesn't have data; falling back to manual" warning
   - If supported: proceed

Phase A.2 — Collect AIC inputs (deduplicate against Phase 2)
   - GPU system (h100_sxm | h200_sxm | b200_sxm | gb200 | a100_sxm | b60 | l40s
     | rtxpro6000_blackwell_server | ...) — NEW question, not collected today
   - Backend (vllm | sglang) — trtllm excluded since it can't target llm-d
   - Backend version (default to latest in AIC's perf DB)
   - ISL / OSL targets — SHARES with Phase 2 (ask once, reuse)
   - TTFT / TPOT targets — SHARES with Phase 2
   - Quantization hint (BF16 | FP8) — auto-detect from model `config.json` if possible

Phase A.3 — Run AIC search, present Pareto frontier
   $ aiconfigurator cli default \
       --model <id> --total-gpus <N> --system <gpu> \
       --isl <A> --osl <B> --ttft <C> --tpot <D> \
       --backend <vllm|sglang> --backend-version <V> \
       --deployment-target llm-d \
       --save-dir <work>

   Show user top-3 candidates from AIC's Pareto frontier:
     | Rank | Mode   | TP × DP × Replicas | tokens/s/gpu | TTFT  | TPOT |
     |------|--------|--------------------|--------------|-------|------|
     | 1    | agg    | 2×1×8              | 1240         | 720ms | 22ms |
     | 2    | disagg | P:4×1×2 D:2×1×6    | 1180         | 580ms | 19ms |
     | 3    | agg    | 4×1×4              | 980          | 850ms | 24ms |

   User picks one (or default to AIC's #1).

Phase A.4 — Pre-fill our autoconfig schema from AIC's pick
   - topology.mode = "agg" or "disagg"
   - topology.replicas / tp (for agg) OR prefill_replicas/prefill_tp/decode_replicas/decode_tp (for disagg)
   - topology.source = "aic" (NEW field; existing alternatives implicit: "user")
   - workload.max_num_seqs = AIC's chosen batch size
   - features.aic_modelservice_values = path to AIC's generated llm-d-values.yaml

Phase 2.5 onward — As today, but topology section pre-filled
   "AIC suggests this — confirm or override?" framing throughout the recap.

Phase 4 — Call the script (unchanged; topology + max_num_seqs + AIC values path are pre-filled by Phase A.4)

Phase 6.3 — MODIFIED: install modelservice using AIC's Helm values
   When AIC was used, swap the Kustomize-overlay install (today's default) for:
   $ helm install ms-<release> oci://<llm-d-modelservice-chart> \
       -f <aic-output>/llm-d-values.yaml \
       -n <ns>

   The AIC output is the source of truth for the modelservice; we don't second-guess it.
   No post-deploy patching needed (the values are already tuned for the user's request).

Phase 6.4 — Install EPP (unchanged)
   Our generated EPP config layered on the llm-d-router chart, regardless of whether AIC was used.

Phase 7 — Benchmark (unchanged)
   Our guidellm-based config still drives the full-stack validation; AIC also emits
   an AIPerf Job spec (`k8s_bench.yaml`) that the agent can optionally apply for
   model-server-layer validation.
```

## Schema impact on the autoconfig script

The PD-disagg schema is already in place (`Topology.prefill_replicas` / `prefill_tp` / `decode_replicas` / `decode_tp` / `pd_transport` exist; `_DEFAULT_PD_PLUGINS` + `_DEFAULT_PD_WEIGHTS` constants are wired into `_build_profile_plugins`). Only the AIC-specific provenance fields are new:

```python
@dataclass
class Topology:
    # ... existing fields (mode, replicas, tp, prefill_*, decode_*, pd_transport) ...
    source: str = "user" # NEW: "user" | "aic" — provenance for downstream UX
                         # ("AIC suggests …" framing only when source="aic")

@dataclass
class Features:
    # ... existing fields ...
    aic_modelservice_values: str | None = None  # NEW: path to AIC-generated llm-d-values.yaml
```

`topology.source` is informational — the recommendation algorithm doesn't branch on it; only the agent's narration does ("AIC suggests TP=2 × 8 replicas; confirm or override" vs the today's "you said TP=2 × 8 replicas").

## What AIC unlocks for our algorithm

| Today | With AIC integration |
|---|---|
| User picks TP manually based on rough heuristic ("≤32B → TP=2") | AIC searches the actual config space and picks based on perf data |
| User picks agg vs disagg; we support both but don't tell them when each pays off | AIC chooses based on perf data and tells them why |
| `max_num_seqs` left null (plugin default applies) | AIC's chosen batch size populates the field |
| `lruCapacityPerServer` either auto-tuned or static default | Quantization signal (FP8 vs BF16) feeds our LRU sizing math (KV cache size halves with FP8, so LRU can be larger) |
| Modelserver values pulled from generic per-accelerator Kustomize overlay (Qwen3-32B / TP=2 hardcoded) with post-deploy patching | Modelserver values custom-generated for the user's exact request, no patching needed |

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| AIC install is heavy (`pip install aiconfigurator` + `git lfs pull` for perf data; ~hundreds of MB) | Detect `aiconfigurator` on PATH; gracefully refuse Phase A.0 with "AIC not installed; falling back to manual topology" if missing. Document install requirements clearly. |
| User's GPU isn't in AIC's perf DB (e.g. legacy V100, some new CSP-specific accelerator) | `aiconfigurator cli support` returns "unsupported"; agent falls back to manual topology with a warning |
| Backend mismatch — AIC has perf data for vLLM and SGLang for llm-d target, NOT trtllm | If user is deploying trtllm, AIC integration is off the table for the modelservice side; surface and skip |
| Two tools' input schemas overlap but don't match exactly | Build a thin adapter (`aic_to_input.py`) with a clear mapping; test it round-trips |
| AIC's recommendations may not match real-world perf for the user's specific workload | AIC ships a benchmark Job alongside its output; pair with Phase 7 benchmark for full-stack validation |
| AIC version drift could break our adapter | Pin AIC version in skill `compatibility:` field; bump deliberately |
| Two modelservice install paths (Kustomize overlay vs AIC helm values) is more complexity | Keep both in Phase 6.3 with a clean branch on `features.aic_modelservice_values`; document which wins. |

## Implementation checklist

When picking this up later (PD-disagg scaffolding is already in place; everything below is genuinely AIC-specific):

- [ ] Add `Topology.source` field
- [ ] Add `Features.aic_modelservice_values` field
- [ ] Write `aic_to_input.py` adapter (parses AIC's `agg_config.yaml` or `disagg_*.yaml` output and converts to our Input schema)
- [ ] Add SKILL.md Phase A.0–A.4 sub-flow (renamed from the original "Phase 2.5" framing since 2.5 is now doc-driven synthesis)
- [ ] Add `compatibility:` requirement to SKILL.md frontmatter: `aiconfigurator >=X.Y on PATH for AIC integration; optional`
- [ ] Update Phase 6.3 to branch: AIC path uses `helm install ms-... -f <aic-llm-d-values>` instead of `kubectl apply -k <overlay>` + post-deploy patches
- [ ] Add fixture: `examples/input-aic-llama70b.json` + `examples/output-aic-llama70b.json` for AIC-driven case
- [ ] Add tests: `AICAdapterTest` (in `tests/test_topology.py` or a new `tests/test_aic.py`)
- [ ] Quantization-aware LRU sizing — once `topology.source="aic"` and quantization hint is captured, fold into `no-hit-lru-scorer.lruSize` derivation

## Effort estimate

- **Minimum viable AIC integration** (sizing pre-fill, helm-values modelservice install path, Phase A sub-flow): ~2 days
- **Stretch: quantization-aware LRU + recommendation framing polish + fixtures + tests**: +1–2 days
- **Total**: ~3–4 days end-to-end

## Open questions (resolve before starting)

1. **Adapter format**: parse AIC's emitted YAML files, or have AIC emit JSON? Latter would require an AIC PR.
2. **AIC version pinning**: which version do we test against? Pin via `requirements.txt` in our skill?
3. **Modelservice install path coexistence**: keep both Kustomize-overlay AND helm-from-AIC paths in Phase 6.3 (branching on `features.aic_modelservice_values`), or have AIC's path fully replace Kustomize when AIC is in play?
4. **Quantization-aware LRU sizing**: how do we encode "FP8 means LRU can be 2× larger" — explicit T1 derivation, or T4 heuristic?
5. **Where does Phase A.0 actually live?** Strictly between Phase 2 and Phase 2.5 (as drawn), or folded into Phase 1 cluster discovery as another optional sub-flow? The latter is closer to the latency-predictor pattern.

## Reference

- AIC repo: https://github.com/ai-dynamo/aiconfigurator
- AIC paper: https://arxiv.org/abs/2601.06288
- AIC's llm-d output template: `aiconfigurator/src/aiconfigurator/generator/config/backend_templates/vllm/llm-d-values.yaml.j2`

---

## Integration spec (concrete inputs / outputs / commands)

### Confirmation: AIC has its own benchmarking; we don't add any

Two distinct uses of "benchmark" in the AIC context, easy to confuse:

1. **Internal performance database (built-in, automatic).** AIC ships pre-collected kernel-level perf measurements (GEMM, attention, KV cache, MoE, NCCL collectives) per `(GPU type, backend, version)`. Lives under `src/aiconfigurator/systems/data/<gpu>/<backend>/<version>/`, managed via Git LFS. AIC reads this database to predict configurations — no benchmarking required from us OR the user. This is the recommendation engine.

2. **AIPerf benchmark Job (optional output).** AIC can ALSO generate a benchmark Job spec (using AIPerf) alongside its config recommendation, so users can validate the prediction matches reality post-deploy. Optional; emitted via the sflow workflow.

**What this means for us:** we don't replace either. Our existing guidellm benchmark generator (Phase 7) is complementary — it tests the deployed system end-to-end including scheduler routing decisions, not just the model server. Three benchmarking layers if all are used:
- AIC's internal perf DB → drives the topology recommendation (pre-deploy)
- AIC's AIPerf job (optional) → validates the modelserver layer (post-deploy)
- Our guidellm config → validates the full stack including EPP routing (post-deploy)

### Required AIC inputs (must collect from user before invoking)

| AIC arg | Required? | Source in our schema | Notes |
|---|---|---|---|
| `--model-path` (alias `--model`) | Yes | `model` | HF ID, e.g. `Qwen/Qwen3-32B-FP8` |
| `--total-gpus` | Yes | derive from `topology.replicas × topology.tp`, OR ask user separately | The total GPU budget AIC searches over |
| `--system` | Yes | NEW input — not in our schema today | One of: `h200_sxm`, `h100_sxm`, `b200_sxm`, `b300_sxm`, `gb200`, `gb300`, `a100_sxm`, `b60`, `l40s`, `rtxpro6000_blackwell_server` |
| `--deployment-target llm-d` | Always set by us | hardcoded | Ensures llm-d-modelservice values get rendered |

### Common-but-optional AIC inputs (defaults work, but better when populated)

| AIC arg | Default | Source in our schema |
|---|---|---|
| `--isl` | 4000 | `workload.isl` |
| `--osl` | 1000 | `workload.osl` |
| `--ttft` | 2000 | `slo.ttft_ms` (note: AIC accepts float, not int — coerce) |
| `--tpot` | (none, weak constraint) | `slo.tpot_ms` |
| `--request-latency` | (none) | `slo.request_latency_ms` |
| `--prefix` | 0 | `workload.prefix_len` |
| `--backend` | trtllm | NEW — must default to `vllm` since trtllm can't target llm-d |
| `--backend-version` | latest | NEW — surface to user only if they care; otherwise default |
| `--max-seq-len` | (none) | `model_context_length` |
| `--decode-system` | same as `--system` | NEW — only relevant for heterogeneous PD-disagg pools |

### Niche optional AIC inputs (don't surface to user unless asked)

- `--free-gpu-memory-fraction` (default 1.0) — filter batch sizes that exceed KV cache capacity
- `--enable-chunked-prefill` (default off) — vLLM/SGLang chunked-prefill feature
- `--strict-sla` — only keep configs that meet `--tpot`; otherwise allow the optimizer to suggest configs that exceed it
- `--enable-wideep` — wide-expert-parallelism for MoE models
- `--nextn`, `--nextn-accept-rates` — multi-token prediction (model must support MTP)
- `--database-mode` (default SILICON) — perf data source: SILICON (real measured), HYBRID, EMPIRICAL, SOL (theoretical speed-of-light)
- `--top-n` (default 5) — how many Pareto-best configs to return; we'll show top 3 in Phase A.3

### What AIC produces (under `--save-dir`)

For agg topology with `--deployment-target llm-d`:
```
<save-dir>/
└── agg/
    ├── top1/
    │   ├── llm-d-values.yaml      # READY for `helm install ... -f`
    │   ├── agg_config.yaml        # internal config (we don't consume directly)
    │   ├── generator_config.yaml
    │   ├── run_0.sh
    │   └── k8s_bench.yaml         # AIPerf bench Job spec (optional to apply)
    ├── top2/...
    └── top3/...
```

For disagg topology:
```
<save-dir>/
└── disagg/
    └── top1/disagg/
        ├── llm-d-values.yaml      # combined modelservice values for prefill + decode
        ├── prefill_config.yaml
        ├── decode_config.yaml
        ├── node_0_run.sh
        └── k8s_bench.yaml
```

Plus `pareto_frontier.png` showing the search results visually.

### The exact CLI invocation we'd build

For an agg case:
```bash
aiconfigurator cli default \
    --model "Qwen/Qwen3-32B-FP8" \
    --total-gpus 16 \
    --system h200_sxm \
    --backend vllm \
    --deployment-target llm-d \
    --isl 1000 --osl 500 \
    --ttft 800 --tpot 25 \
    --max-seq-len 32768 \
    --save-dir /tmp/aic-out
```

Then we parse the resulting `llm-d-values.yaml` and use AIC's chosen `topology.{mode, replicas, tp}` (or `prefill_*` / `decode_*` for disagg) to pre-fill our autoconfig input.

### Where AIC's outputs slot into our flow

| Phase | Today | With AIC |
|---|---|---|
| Phase 2 (discovery questionnaire) | Ask user for tp + replicas (or PD quartet) | **Pre-filled from AIC** ("AIC suggests TP=2 × 8 replicas; confirm or override") |
| Phase 2 (workload signals) | Collect ISL / OSL / SLA | Already shown to AIC in Phase A.2; don't ask twice — reuse |
| Phase 2.5 (doc-driven synthesis) | Read guide values to pick plugins | Unchanged |
| Phase 4 (call script) | Pass topology user provided | Pass AIC-derived topology + `aic_modelservice_values` path |
| Phase 6.3 (modelservice install) | `kubectl apply -k <Kustomize overlay>` + post-deploy patches | `helm install ms-... -f <save-dir>/agg/top1/llm-d-values.yaml` (AIC's output) |
| Phase 6.4 (EPP install) | unchanged | unchanged |
| Phase 7 (benchmark) | guidellm config (validates EPP routing) | Same — guidellm still validates the full stack. Optionally apply AIC's `k8s_bench.yaml` AIPerf Job for modelserver-layer validation |

### What we need to add to our skill / script

**Schema additions (autoconfig_poc.py Input):**
- `gpu_system: str | None` — AIC's GPU enum value (h100_sxm etc.)
- `backend: str | None` — vllm | sglang (default vllm when AIC enabled)
- `backend_version: str | None` — defaults to AIC's latest
- `Topology.source` — provenance flag for narration ("user" | "aic")
- `Features.aic_modelservice_values` — path to AIC's generated helm values

**SKILL.md additions:**
- Phase A.0 — branch on whether to use AIC (require `aiconfigurator` on PATH)
- Phase A.1–A.4 — AIC sub-flow (compatibility check, input collection, run, parse output)
- Phase 6.3 — branch on whether AIC was used (helm install AIC values vs Kustomize overlay)

**No changes to:**
- The recommender algorithm itself (still computes EPP config from inputs)
- PD-disagg plugin selection (already implemented; AIC just decides *when* to use it)
- Benchmark generation (guidellm config stays as-is — complementary to AIC's bench)
- All other phases

### Install footprint warning for the user

AIC is not lightweight. Per the README:
- `pip3 install aiconfigurator` works
- BUT install includes `git lfs pull` for the perf database — hundreds of MB on disk
- First-time install ~1-2 min on a fast connection
- Python 3.9+
- Graceful degradation if not installed: SKILL.md Phase A.0 falls back to manual topology entry with a warning

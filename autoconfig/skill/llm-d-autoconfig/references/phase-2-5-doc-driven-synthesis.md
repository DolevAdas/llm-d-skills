# Phase 2.5 — Doc-driven recommendation synthesis

*Detailed runbook for SKILL.md Phase 2.5. Read this between Phase 2 (input gathering) and Phase 3 (recap) — it's where the agent does the actual recommending.*


The script renders whatever plugin/weight/profile structure the agent supplies; **this phase is where the agent does the recommending**. Read the relevant upstream docs, quote what they say, and emit a `recommendation` object that Phase 4 inlines into the script's input JSON. Do not invent plugin choices from memory — every plugin, weight, and parameter you put in `recommendation` must trace to a quote you pull in this phase.

### Step 1: pick which docs to read

The full URL map is `<skill-install-dir>/feature_docs.yaml` — a single source of truth whose URLs track upstream `main`. Read this file once, then build your reading list from Phase 2 answers using the table below. Skip rows whose trigger didn't fire.

Lookup keys are `<category>.<entry>` paths inside `feature_docs.yaml` (snake_case). Each entry has a `main` URL plus an optional `secondary` list of related URLs — read all of them.

| User signal | Read these entries from feature_docs.yaml |
|---|---|
| Always | `guides.optimized_baseline`, `charts.router_gateway`, `architecture.router`, `architecture.inferencepool` |
| `topology.mode = "agg"` | (already covered by `guides.optimized_baseline`) |
| `topology.mode = "disagg"` | `guides.pd_disaggregation`, `architecture.disaggregation`, `concept_docs.pd_disaggregation_concept` |
| `features.enable_latency_predictor = true` AND any SLO target set | `guides.predicted_latency_based_scheduling`, `plugins.latency_scorer`, `concept_docs.predicted_latency_concept` |
| `features.enable_latency_predictor = true` AND no SLO target | `guides.predicted_latency_based_scheduling` only |
| `features.enable_precise_prefix_cache = true` OR `workload.prefix_share` in {`medium`, `high`} | `guides.precise_prefix_cache_aware`, `concept_docs.precise_prefix_cache_aware_concept` |
| `features.enable_tiered_cache = true` | `guides.tiered_prefix_cache`, `concept_docs.tiered_prefix_cache_concept` |
| `features.enable_flow_control = true` | `guides.flow_control` (read all `secondary` URLs: ordering / fairness / usagelimits / saturationdetector READMEs) |
| `features.enable_wide_ep = true` | `guides.wide_ep_lws` |
| `features.autoscaler = "wva"` | `guides.workload_autoscaling`, `guides.workload_autoscaling_wva`, `implementation_repos.llm_d_workload_variant_autoscaler` |
| `features.autoscaler = "hpa"` | `guides.workload_autoscaling`, `guides.workload_autoscaling_hpa` |
| `features.serving_pattern = "batch"` | `guides.batch_gateway` |
| `features.serving_pattern = "async"` | `guides.asynchronous_processing` |
| `features.enable_inference_objective = true` | `schemas.inferenceobjective_crd` |
| `features.enable_model_rewrite = true` | `schemas.inferencemodelrewrite_crd` |
| `workload_traits.heterogeneous_context = true` | `plugins.context_length_aware_scorer` |
| `workload_traits.multimodal = true` | (no dedicated guide today — flag this in `summary` and recommend the user verify modelserver multimodal config separately) |
| `workload_traits.multi_turn = true` | Search `plugins.*` for session-affinity entries; if absent, fall back to the plugin's README via search |
| `workload_traits.lora = true` | Search `plugins.*` for lora-affinity entries; if absent, fall back to the plugin's README via search |
| Any topology with autotune flaky | `plugins.kv_cache_utilization_scorer` (and `plugins.no_hit_lru_scorer` if listed) |

If a "Search `plugins.*` for ..." row matches no entry in feature_docs.yaml, treat that as a gap: surface it in the Phase 2.5 `summary` ("No `feature_docs.yaml` entry for X — falling back to best-effort search; please verify before deploying") and either grep the upstream repo for the plugin's README or omit the plugin and ask the user.

Heads-up on the Phase B feature flags: the script today emits an advisory warning per flag pointing at the cited `feature_docs.yaml` entry, but it does NOT auto-emit helm values for the flag's deploy side (modelserver overlay, autoscaler CR, batch gateway, etc.). Your `summary` must call this out — e.g., "WVA enabled — Phase 6 will need a separate `kubectl apply -k` for the WVA operator; this script only handles the EPP layer."

### Step 2: fetch the docs (cache-aware, BATCH the calls)

Use the bundled doc fetcher. It handles caching, version stamping, and stale-fallback automatically. `fetch` ensures URLs are cached and **prints local cache file paths on stdout, one per line, in input order** — you then read each file with your normal file-read tool.

**ALWAYS batch all URLs into one `fetch` invocation** rather than calling fetch once per URL. The fetcher's python-spawn + `feature_docs.yaml`-parse overhead is paid once per process (~100-200ms), not once per URL. For a typical Phase 2.5 reading list of 5-8 URLs, batching saves 0.5-1.5 seconds AND collapses the agent's transcript from 5-8 Bash calls into 1.

```bash
# Batch — preferred for Phase 2.5:
$ paths=$(python3 <skill-install-dir>/scripts/doc_cache.py fetch \
      <url1> <url2> <url3> <url4> ... )
# Output: N paths separated by newlines, in input order.
# Read each one with your file-read tool in the order they appear.

# Single-URL still works (for ad-hoc lookups):
$ path=$(python3 <skill-install-dir>/scripts/doc_cache.py fetch <url>) && cat "$path"
```

The cache lives at `<skill-install-dir>/cache/docs/`. **Do NOT redirect `fetch` output to a parallel temp dir** (e.g. `fetch URL > /tmp/something.md`) — that bypasses the cache and creates a duplicate copy. The cache IS your storage; just read from it.

If you want to inline the body for ad-hoc grep without saving a path variable, pass `--body` (single-URL only — N concatenated bodies on stdout would be ambiguous):

```bash
$ python3 <skill-install-dir>/scripts/doc_cache.py fetch --body <url> | grep -i 'plugin'
```

Per Hard Rule on cache lifetime: don't re-fetch the same URL within a session — the cache returns instantly on hit (no network call, but the python-spawn overhead is still real, which is why batching matters). The cache is keyed by URL + `meta.skill_version` from `feature_docs.yaml`, so bumping the version cleanly invalidates everything.

If any URL in the batch fails (network blocked, 404 on a moved page), `fetch` prints an `error: <url>: <reason>` line on stderr, emits an empty line on stdout at that position (so positional indexing still aligns with your input list), and exits non-zero. Surface the failure to the user — DO NOT skip the doc silently and proceed to synthesize without it. The whole point of this phase is that recommendations are doc-anchored.

**Reading tip for agents with a file-read tool (Claude Code, etc.):** after `fetch` prints paths, use your native file-read tool on each path. That way the doc body counts against your file-read budget rather than your shell-output budget, and you can navigate large docs with offsets.

### Step 3: extract canonical plugin sets + parameters

For every feature you read, pull out three things and keep them as raw quotes in your scratch notes:

1. **The `plugins:` list** from the guide's `values.yaml` (the values files under `guides/<guide>/router/`). This is the canonical plugin set the guide recommends.
2. **The `schedulingProfiles[]`** structure (full profile blocks with per-plugin weights). PD has two profiles; agg + latency-predictor variants have one.
3. **Any per-plugin `parameters:` block** that's set verbatim in the canonical (e.g. `affinityThreshold: 0.99` for the strict affinity filter, `streamingMode: true` for the producer). Do NOT compute these — script handles derived params; this phase only forwards the canonical fixed defaults.

For per-plugin README docs (e.g. `plugin-session-affinity-scorer`), pull:
- The plugin's stated **purpose** and **inputs/outputs** (so you can spot incompatibilities — e.g. "session-affinity-scorer requires session header X; if you're doing PD-disagg, the session must persist across prefill and decode picks, which this plugin does NOT coordinate").
- Any **compatibility caveats** the README calls out (search for "incompatible", "not supported with", "requires", "must be").
- The **default parameter values** the README documents.

### Step 4: merge into a single recommendation

You now have N canonical plugin sets (one per feature you read). Merge them as follows:

**Merge rules — apply in order:**

1. **Start with the topology base.** Agg → optimized-baseline plugin set. PD → pd-disaggregation plugin set. This is the floor.
2. **Layer feature-specific sets.** For each enabled feature, overlay its canonical plugin list. Where a plugin appears in both, the more specific feature wins for parameters; both contribute to the union of plugins. Order matters — preserve the order from the canonical guide (don't sort alphabetically).
3. **Apply compatibility rules from per-plugin READMEs.** If a plugin doc says "incompatible with PD" and the user picked PD, do not include that plugin. Instead, surface a quoted note in `summary`: "The session-affinity-scorer README says <quote>; dropping under PD topology."
4. **Resolve weight conflicts.** Two guides may assign different weights to the same plugin. Prefer the feature-specific guide's weight over the baseline. Document the conflict in `summary` with both quoted values: "optimized-baseline assigns prefix-cache-scorer weight=3; predicted-latency-slo profile is unweighted — using the predicted-latency-slo structure since the user enabled the predictor."
5. **For PD with extra features (latency-predictor, precise-prefix), append to the two-profile structure**, don't replace it. The pd-disaggregation profile structure is the canonical for PD even when features are layered on.

The output of merging is three structures that go directly into `recommendation`:
- `plugins[]`: deduped, in canonical order. Each entry is an **object** in the EndpointPickerConfig shape — `{"type": "<plugin>"}` — exactly as the guide `values.yaml` lists them. Add `"name"` for a named instance and `"parameters"` only if you're deliberately overriding what the script derives (normally leave parameters out — the script computes them).
- `weights{}` (agg single-profile only): per-plugin weight map, keyed by plugin name.
- `scheduling_profiles[]` (PD or non-default profile structure only): full profile objects with their own plugins[].

Leave a field empty if the script's default is already correct for the user's case (e.g., agg + no features → leave `weights` empty so script uses `_DEFAULT_AGG_WEIGHTS`).

### Step 5: write the `recommendation` object

The structure goes under the top-level `recommendation` key in the input JSON (Phase 4 Step 1 includes it in the schema). All fields are optional; script falls back to canonical defaults for anything you leave empty.

```json
{
  "recommendation": {
    "plugins": [
      {"type": "queue-scorer"},
      {"type": "kv-cache-utilization-scorer"},
      {"type": "precise-prefix-cache-scorer"},
      {"type": "no-hit-lru-scorer"}
    ],
    "weights": {
      "queue-scorer": 2,
      "kv-cache-utilization-scorer": 2,
      "precise-prefix-cache-scorer": 4,
      "no-hit-lru-scorer": 2
    },
    "scheduling_profiles": [],
    "cited_sources": [
      "https://github.com/llm-d/llm-d/blob/main/guides/precise-prefix-cache-routing/README.md",
      "https://github.com/llm-d/llm-d-router/blob/main/pkg/epp/framework/plugins/scheduling/scorer/preciseprefixcache/README.md"
    ],
    "summary": "RAG-shaped workload (prefix_share=high). Per the precise-prefix-cache-routing guide README: \"Swap the basic prefix-cache-scorer for the precise-prefix-cache-scorer when shared prefixes dominate the workload.\" Weight 4 reflects the precise scorer's elevated signal on this workload class; baseline uses weight 3."
  }
}
```

**`summary` is mandatory if you set any of `plugins`/`weights`/`scheduling_profiles`** — it's the human-readable rationale Phase 5 surfaces back to the user, and must include at least one direct quote from a doc you cited (use literal "quote marks" so the user knows it's from the doc, not paraphrase).

**`cited_sources` must list every URL you actually read in Step 2** — even if a doc didn't change your decision. Phase 5 lists these to the user so they can re-read.

### Step 6: edge cases

- **All Phase 2 answers are minimal (no SLO, no traits, agg topology)**: leave `recommendation` empty. Script applies optimized-baseline canonical defaults.
- **Doc fetch fails on a critical feature** (e.g., user enabled latency-predictor but the guide URL 404s): STOP this phase. Surface the failure to the user: "I couldn't fetch <url> to confirm the latency-predictor plugin set. The canonical might have moved. Want me to try the chart's defaults instead (less accurate, no doc trace) or pause until we update the URL map?"
- **User-supplied combo isn't covered in any guide** (e.g., PD + LoRA + heterogeneous context): document the gap in `summary` honestly: "No published guide covers PD + LoRA + heterogeneous-context together. Using PD canonical as base; LoRA-affinity-scorer and context-length-aware READMEs both list compatibility caveats I haven't been able to resolve from docs — flagged for human review."
- **Plugin set conflicts with PD architecture** (e.g., a feature wants weighted-random-picker but PD's profile structure uses max-score-picker per profile): defer to PD. Quote the PD guide on this in `summary`.

---

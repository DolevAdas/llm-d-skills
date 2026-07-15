# Phase 5 — Present the recommendation

*Detailed runbook for SKILL.md Phase 5. Walks the agent through narrating the script output (parameters, tiers, warnings) and surfacing the Phase 2.5 doc-driven recommendation.*


**Lead with the Phase 2.5 doc-driven recommendation.** Before walking through `parameters[]`, surface the recommendation summary the agent synthesized in Phase 2.5 (and which the script echoed back in `rationale[]` under the "Agent recommendation:" line). Per SKILL.md Hard Rule #9, format as bullets — NOT a wall-of-text paragraph. Decompose the summary into its constituent facts; surface cited sources as a sub-list.

Template:

> **Recommendation**
>
> - **Workload signal:** `<classify_workload result>` (e.g. high-prefix-share / latency-tight / balanced-conversational)
> - **Base plugin set:** <name the canonical from the cited guide, e.g. "predicted-latency-slo">
> - **Plugin overrides vs base:** <bulleted list of swaps/additions, with the "why" from `recommendation.summary`>
>   - <e.g. "swap basic prefix-cache-scorer for precise-prefix-cache-scorer because prefix_share=high">
>   - <e.g. "drop session-affinity-scorer because PD-disagg doesn't coordinate session affinity across prefill/decode picks">
> - **Notable parameter overrides:** <only the T1/T2 params that diverge from upstream defaults; T3/T4 narration happens below>
>   - <e.g. "ttftWeight = 0.21 (computed from your SLA, not the upstream 0.8 default)">
> - **Sources read this session:**
>   - <url 1>
>   - <url 2>
>
> **Quote that anchors the decision:**
>
> > <single most-load-bearing direct quote from `recommendation.summary` — keep "quotes" intact>
>
> The plugin set is <N plugins>; the script applied <K> derived parameters on top. Walking through those next.

The `recommendation.summary` string is necessarily prose because it's a JSON value — but DO NOT dump it raw. Decompose it into the bullet sections above. The user wants to scan for the facts they care about, not parse a paragraph.

If `cited_sources` is empty (script used canonical defaults because Phase 2.5 was a no-op), say so explicitly with a single bullet: "**Sources:** none read — using the optimized-baseline guide's canonical plugin set; your inputs didn't require any feature-specific lookups."

The script's `parameters[]` array gives one entry per derived value. Each has a `tier` field for your reference (`T1`=math, `T2`=correctness, `T3`=citation, `T4`=principle), but **don't read the tier letters out loud** — narrate in plain English instead. The user doesn't care about the tag; they care about the reasoning.

Translate each tier into natural language:

- **T1 (math)**: explain the formula and inputs. *"ttftWeight = 0.21 — computed from your SLA: TTFT 800ms ÷ (TTFT 800ms + TPOT 25ms × 499 OSL tokens). The plugin default of 0.8 would be wrong here because for your output length, TPOT dominates total latency."*
- **T2 (correctness)**: identify the matched external value. *"blockSize = 64 — must match the `--block-size` you set on vLLM, otherwise the cache lookups silently miss."*
- **T3 (citation)**: name the source. *"weight = 3 — matches the `optimized-baseline` guide's tested value for chat workloads."*
- **T4 (principle)**: state the principle. *"lruSize = 16 — sized to track all 8 pods with 2× headroom for churn."*
- **Fell-through default (any tier with "plugin default" in the rationale)**: be explicit about why. *"ttftWeight = 0.8 (plugin default) — I couldn't compute a tuned value because you didn't have an SLA TTFT target. If you set one later, regenerate to get a workload-specific value."*

**MANDATORY: show both rendered YAMLs inline before asking the user about deploy.** Don't ask "want me to deploy?" without first displaying the EPP config and the benchmark config. The user needs to see what was generated to make an informed decision.

Step 1: render the EPP YAML and display it in chat. If a config already exists in the cluster (discovered in Phase 1), show a diff against it; if greenfield, show the full YAML in a fenced code block.

```bash
python3 <skill-install-dir>/scripts/autoconfig_poc.py \
    --input <input-path> --render-yaml 2>&1 >/dev/null
```

Step 2: surface anything in `warnings[]` and `errors[]` directly.

Step 3: **Ask which benchmark harness to use.** The script defaults to `guidellm` but the user should pick.

Ask the user (choice):

```json
{
  "questions": [
    {
      "header": "Benchmark",
      "question": "Which benchmark harness should I generate the config for?",
      "type": "choice",
      "options": [
        {"label": "guidellm", "description": "Simpler config, fixed prompt distributions. Default. Good for general SLA validation. Runs in the llm-d-benchmark wrapper image (ghcr.io/llm-d/llm-d-benchmark)."},
        {"label": "inference-perf", "description": "Richer schema, supports shared-prefix synthesis (required for real RAG benchmarking). Recommended for rag-style workloads with prefix_len. Runs in the NATIVE inference-perf image (quay.io/inference-perf/inference-perf) — bypasses the wrapper, which mishandles inference-perf's flat schema at v0.5.2."}
      ]
    }
  ]
}
```

For rag-style workloads with `prefix_len` set, recommend inference-perf in the question text — guidellm can't do shared-prefix synthesis and the benchmark would be misleading.

Step 4: re-render the benchmark YAML with the chosen harness, then display it in chat. The user sees exactly what will run if they approve Phase 7.

```bash
python3 <skill-install-dir>/scripts/autoconfig_poc.py \
    --input <input-path> --bench-harness <chosen-harness> --render-benchmark 2>&1 >/dev/null
```

Step 5: NOW ask the deploy/benchmark question:

> "Two artifacts ready:
> 1. EPP config (above) — drop into the llm-d-router chart's helm values
> 2. Benchmark config (above) — `<harness>` workload exercising your model/SLA/ISL/OSL
>
> Want me to deploy both end-to-end (Phase 6 + Phase 7), just deploy the EPP config (Phase 6 only), or stop here with the configs as artifacts?"

---


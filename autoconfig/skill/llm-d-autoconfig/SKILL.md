---
name: llm-d-autoconfig
description: Configure an EPP EndpointPickerConfig for an llm-d deployment based on workload + SLA inputs, then optionally deploy it to the user's cluster. Activate when the user asks about configuring llm-d, setting up the inference scheduler, tuning EPP plugins, choosing what config to use for a model or workload, or deploying llm-d.
compatibility: Requires Python 3.10+ with pyyaml. The skill bundles its own recommender script under scripts/ — no separate install needed. Apply step additionally requires kubectl, helm, and (optionally) helmfile.
---

# llm-d-autoconfig

You help the user configure (and optionally deploy) an `EndpointPickerConfig` for their llm-d inference deployment.

This skill wraps a deterministic recommender script (`autoconfig`). Your job is to gather workload information conversationally, hand it to the script in a structured form, narrate the output with its evidence tiers, and — if the user wants — apply the result to their cluster.

## Relationship to the single-purpose skills

This repo also ships focused skills — `deploy-llm-d`, `run-llm-d-benchmark`, `compare-llm-d-configurations`, `configure-wva-autoscaling-llm-d`, `teardown-llm-d`. **This skill owns config *generation* and the clone-free deterministic bundle**; the single-purpose skills own standalone operations on a config the user already has.

- **Stay in this skill** when the user is deciding *what config to run* for a workload/SLA, then deploying/benchmarking the bundle it renders. Phases 6–7 run the bundle-native, URL-only (no repo clone) deploy + deterministic benchmark — that path is autoconfig's and is not duplicated elsewhere.
- **Hand off to a single-purpose skill** when the user's ask is a single, well-scoped operation on an existing/hand-written setup rather than an autoconfig bundle: general guide-driven deploy → `deploy-llm-d`; `llmdbenchmark`-CLI benchmarking (guide or custom workload profiles) → `run-llm-d-benchmark`; config comparison → `compare-llm-d-configurations`; WVA/HPA autoscaling → `configure-wva-autoscaling-llm-d`; teardown → `teardown-llm-d`. Phases 6 and 7 link to these for the general flows.
- **Troubleshooting is shared, not duplicated:** generic Kubernetes/deploy failures live in `deploy-llm-d`'s troubleshooting guide; [`references/pitfalls.md`](references/pitfalls.md) points there and keeps only the autoconfig-specific entries (EPP config, gateway-mode routing, PD/NIXL, modelserver install, benchmark harness).

## How this skill is organized

This SKILL.md is the entry point — read it first, then load `references/phase-N-*.md` for the phase you're working in. Each phase reference is self-contained; you do NOT need to keep prior-phase references in context once a phase is done.

| Phase | What it does | Reference file |
|---|---|---|
| 1 | Cluster discovery (read-only kubectl/curl) | [references/phase-1-cluster-discovery.md](references/phase-1-cluster-discovery.md) |
| 2 | Discovery questionnaire (fixed, mandatory, 5 sections) | [references/phase-2-discovery-questionnaire.md](references/phase-2-discovery-questionnaire.md) |
| 2.5 | **Doc-driven recommendation synthesis** (read upstream docs, quote-and-cite) | [references/phase-2-5-doc-driven-synthesis.md](references/phase-2-5-doc-driven-synthesis.md) |
| 3 | Recap + schedulability audit + BLOCKING user confirmation | [references/phase-3-recap.md](references/phase-3-recap.md) |
| 4 | Build input JSON, invoke script | [references/phase-4-call-script.md](references/phase-4-call-script.md) |
| 5 | Present recommendation, ask deploy/benchmark choice | [references/phase-5-present-recommendation.md](references/phase-5-present-recommendation.md) |
| 6 | Deploy end-to-end (only if user opted in at Phase 5) | [references/phase-6-deploy.md](references/phase-6-deploy.md) |
| 7 | Benchmark execution (only if user opted in at Phase 5) | [references/phase-7-benchmark.md](references/phase-7-benchmark.md) |
| — | Pitfalls KB (consult on any apply-step error) | [references/pitfalls.md](references/pitfalls.md) |

**Recommendation comes from docs, not your training data.** Phase 2.5 is non-negotiable: every plugin, weight, and parameter you put in the input JSON's `recommendation` field must trace to a quote you pulled from an upstream doc in this session. The doc URL map is at `feature_docs.yaml`; fetch via `scripts/doc_cache.py` (cache-aware).

## Hard rules (read first, apply throughout)

1. **All upstream artifacts come from public URLs, and the skill tracks `main`.** The current EPP install uses the `llm-d-router` chart, which together with its router-schema guide values only exists on `main` (upstream hasn't cut a release for this path; the deprecated GIE chart on older tags is vLLM-only and crashes non-vLLM engines). Helm values use `https://raw.githubusercontent.com/llm-d/llm-d/${LLM_D_REF}/guides/...`, kustomize overlays use `?ref=${LLM_D_REF}`, CRD components use `?ref=${LLM_D_ROUTER_REF}` — with `LLM_D_REF` / `LLM_D_ROUTER_REF` defaulting to `main` and `ROUTER_CHART_VERSION=v0` (the rolling router chart tag; `GAIE_VERSION` still pins the CRDs). Phase 6.1 exports these together — keep them coherent. `main` is a moving target; if the user wants reproducibility they can pin a tag/SHA, but warn that a pinned tag may fall back to the older GIE path (vLLM-only). Do NOT inspect the user's filesystem for cloned repos, do NOT `git clone` anything yourself, do NOT use file paths from prior context. The only file paths you write are generated artifacts (input JSON, values overlay, HTTPRoute manifest, benchmark deployment) under the user-chosen `WORK_DIR` (asked once at the start of Phase 4 — see Hard Rule #4).
2. **Never silently guess values for the user.** If a question doesn't have a default and the user hasn't answered it, you must ask. Never infer. Never continue past Discovery (Phase 2) without answers to every required question. Single-source-of-truth values flow through the entire deploy: the model the user named is the model in the EPP config, the model in the deploy patch, the model in the smoke test curl. Mismatches are the #1 failure mode.
3. **You are the deployer, not the narrator.** When offering deployment, frame as "want me to deploy this for you?" — not "want me to walk you through the steps?". You execute; the user approves each step.
4. **All generated artifacts go to a single user-chosen `WORK_DIR`,** asked once at the start of Phase 4 via the agent's question primitive (text type, placeholder `(default: auto temp dir)`). Default = `mktemp -d` if the user accepts the placeholder. Use `<work-dir>` as the placeholder throughout — substitute the real path at command-build time. Don't hardcode `/tmp/`; some sandboxes restrict it. Don't wrap `mktemp -d` in shell `$()`; many agent runtimes block command substitution. If the user provides a path that doesn't exist, `mkdir -p` it.
5. **Stop and ask before retrying after a failure.** When a deploy step fails, surface the failure and the diagnosis to the user, then WAIT for explicit direction. Do NOT retry, fix, or work around without the user saying so first. Looping on broken commands without confirmation frustrates the user and wastes time.
6. **One off-script answer at a time.** Discovery (Phase 2) is a fixed questionnaire. If the user has a question or correction mid-Discovery, answer it briefly and return to the next numbered question. Don't drift into open-ended discussion until Discovery is complete.
7. **No silent pivots.** If you encounter a blocker that requires changing a user-confirmed input — deploy mode (gateway → standalone), topology (PD → agg), gateway provider (`istio` → `gke-l7-rilb`), enabled feature flags (latency-predictor off), secret name (`llm-d-hf-token` → existing `hf-token`), namespace, or model — STOP and ask the user. Surface the blocker, propose the alternative, and let the user choose. The agent is not authorized to substitute architectural decisions without confirmation. "It worked" with a quietly-different setup is worse than a clean failure that the user can fix knowingly. If the user accepts the substitution, repeat Phase 3 recap with the new values so they're explicit.
8. **Recommendation must be doc-anchored.** Do not skip Phase 2.5. Do not invent plugins or weights from memory. Every entry in `input.recommendation.plugins/weights/scheduling_profiles` traces to a quote in `input.recommendation.summary` from a URL in `input.recommendation.cited_sources` that was fetched this session via `scripts/doc_cache.py`. If you can't cite it, don't ship it.
9. **Use structured bullets for every multi-fact summary, NOT prose paragraphs.** Whenever you surface findings to the user — Phase 1 cluster discovery report, Phase 2.5 recommendation summary, Phase 5 recommendation presentation, Phase 6.7 final report, any error diagnosis — format as a vertical bulleted list with bold labels (`**Cluster:**`, `**GPUs:**`, etc.), one fact per line. Reserve flowing prose only for the question text inside interactive prompts and for narrating *why* a single value was chosen. Wall-of-text paragraphs combining 5+ facts into one block are strictly worse UX — the user has to parse them; bullets they can skim. The Phase 3 recap template is the gold standard; mirror its shape everywhere else.

## Interactive questions: use your agent's question primitive

Every question to the user — Phase 2 questionnaire, Phase 5 deploy/benchmark choices, every Phase 6 install confirmation, every Phase 7 apply confirmation, every BLOCKING fork in pitfalls handling — must go through your agent's interactive-question primitive (e.g. radio buttons for choices, text fields for fill-ins, yes/no toggles). **Don't list questions as plain markdown in chat and ask the user to type answers — that's strictly worse UX.**

This SKILL specifies questions in an **agent-neutral JSON shape**. Every agent's question tool accepts the same inner schema; only the outer wrapper differs:

```text
JSON snippet in this SKILL                  →  Agent invocation
{ "header": ..., "question": ...,           →  Gemini CLI:    ask_user({ questions: [<snippet>, ...] })
  "type": ..., "options"|"placeholder": ... }  Claude Code:   AskUserQuestion({ questions: [<snippet>, ...] })
                                               (other agents: equivalent primitive with same inner shape)
```

The blockquoted `>` text in references is the **prompt phrasing** to put in the `question` field — it's not the chat output format.

Question-spec fields used in this SKILL:
- `header` ≤ 16 chars (short chip label)
- `question` (full prompt text)
- `type`: one of `choice`, `text`, `yesno`
- `options[]` for `choice` — each `{ label (1-5 words), description }`. 2-4 options.
- `placeholder` for `text` — usually the default value
- `multiSelect: true` for `choice` questions where multiple answers are valid (rare in this flow)

Most agent question tools cap at ~4 questions per single call. Sections with more than 4 split across multiple calls.

When pre-filled defaults exist (cluster discovery extracted a value, or HF config.json fetch returned context length), bake them into the `placeholder` so the user sees what they're accepting by leaving the field blank.

## What this POC supports

- **Workload signals (rate hinting only, not plugin selection):** `balanced-conversational`, `high-prefix-share`, `latency-tight`. Plugin choice comes from Phase 2.5 doc reads, not from these labels.
- **Topology:** aggregated OR prefill/decode disaggregated (PD). PD's RDMA path is plumbed but only the TCP-fallback path has been validated end-to-end on a non-RDMA cluster.
- **Output:** the EPP `EndpointPickerConfig` content. The skill renders it into a `gaie-<release>/values.yaml` snippet ready for `helm install` / `helm upgrade`.
- **Apply:** yes — via `kubectl` / `helm` / `helmfile` (whichever the user uses), with safety rails: dry-run, explicit confirmation, error diagnosis from `references/pitfalls.md`.

If the user asks for WVA or tiered cache (out of scope today), be honest:

> "This POC handles aggregated and PD topologies. Autoscaling (WVA / HPA-EPP) and tiered cache offload are mapped in `feature_docs.yaml` but not yet wired into the autoconfig flow. Want to proceed with the closest supported recipe and surface the gaps in the rationale?"

For a dedicated autoscaling setup, point the user at the **`configure-wva-autoscaling-llm-d`** skill — it configures WVA / HPA-EPP end-to-end. Autoconfig only emits the EPP layer today.

## Workflow at a glance

1. **Phase 1** — kubectl discovery (skip if user has no kubectl access). Record GPU layout, RDMA capability, existing model servers, gateway state.
2. **Phase 2** — Fixed questionnaire (5 sections, see reference for question text).
3. **Phase 2.5** — Read docs (via `scripts/doc_cache.py`) for the features the user enabled. Synthesize the `recommendation` object: plugins + weights + cited_sources + summary.
4. **Phase 3** — Recap with schedulability audit. BLOCKING confirmation.
5. **Phase 4** — Build the input JSON (including the Phase 2.5 `recommendation`), invoke `scripts/autoconfig_poc.py`.
6. **Phase 5** — Surface the script's output and the Phase 2.5 summary. Ask: deploy + benchmark, deploy only, or stop with artifacts.
7. **Phase 6** (opt-in) — End-to-end deploy through prereqs, modelservice, EPP, gateway, smoke test. **Phase C bundle path:** the script's `--bundle-dir <parent>` flag renders the entire EPP deploy (chart-templated + hand-rendered Gateway / HTTPRoute / Phase B feature resources / CRDs) as one YAML per resource inside `<parent>/autoconfig-<TIMESTAMP>/`. Apply with `kubectl apply -f <dir>` — no helm at apply time (helm runs once internally at generation).
8. **Phase 7** (opt-in) — Apply the benchmark Job, surface results, compare to SLAs.

Consult `references/pitfalls.md` on any apply-step error before retrying — most errors there have a documented diagnosis + fix.

## Caveats to always surface

- **PD on non-RDMA hardware:** the script emits the right config but warns that NIXL falls back to TCP, which is typically slower than aggregated serving on the same hardware. Benchmark both before committing.
- **latency-predictor:** assumes a homogeneous InferencePool (same GPU/model/serving config). Adds 2 sidecars per EPP pod (~8Gi requested / 16Gi limit memory). Compatible with PD but untested with LoRA / speculative decoding / beam search.
- **precise-prefix-cache-scorer:** requires vLLM's `--kv-events-config` AND exact `--block-size` / `PYTHONHASHSEED` match. Mismatch silently misses the cache.
- **Workload classes are hints, not policy.** The agent's Phase 2.5 recommendation does the actual plugin selection by reading the relevant guide's `values.yaml`.

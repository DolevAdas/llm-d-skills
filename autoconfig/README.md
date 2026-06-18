# llm-d-autoconfig

<sub>POC · v0.3.1</sub>

A proof-of-concept for an agentic flow that configures llm-d's EPP scheduler and renders a complete deployable YAML bundle from workload + SLA inputs.

**Architecture:** the script is a deterministic renderer. The agent (driven by `SKILL.md`) does the recommending by reading upstream docs in `feature_docs.yaml` and citing them. Every formula the script still computes has an inline `# Source: <URL>` comment pointing at the upstream evidence.

**Coverage:**
- Topologies: aggregated, prefill/decode disaggregated (NIXL RDMA or TCP fallback).
- Phase B feature flags: latency-prediction-based routing, precise prefix-cache, tiered cache, flow control, wide expert-parallelism, autoscalers (WVA / HPA-EPP), batch / async serving patterns, InferenceObjective / InferenceModelRewrite CRDs.
- Phase C bundle: `--bundle-dir <parent>` uses `helm template` with the `llm-d-router` chart and appends hand-rendered Gateway / HTTPRoute / Namespace / HF Secret scaffold / WVA CR / HPA / Kustomization scaffolds. Output is a timestamped directory of individual YAMLs, one per resource, applied with `kubectl apply -f <dir>`. **Helm is needed once at generation, while redeploys and rollbacks only need kubectl.**

See `docs/SUPPORT.md` for the guide coverage matrix. `docs/AIC_INTEGRATION.md` sketches a future integration with NVIDIA AIConfigurator for automated model-server sizing.

EPP/Router code is at [`llm-d/llm-d-router`](https://github.com/llm-d/llm-d-router).

---

## Getting started

The skill works with any agent that loads agentskills.io-format skills including Gemini CLI, Claude Code, or any agent that follows the `SKILL.md` + `references/` + `scripts/` layout.

### 1. Copy to agent skills directory

Copy `skill/llm-d-autoconfig/` into your agent's skills directory:

```bash
# Gemini CLI:
cp -r skill/llm-d-autoconfig ~/.gemini/skills/

# Claude Code:
cp -r skill/llm-d-autoconfig ~/.claude/skills/

# Cross-tool (.agents/):
cp -r skill/llm-d-autoconfig ~/.agents/skills/
```

The skill bundles its own script + doc cache + URL verifier under `scripts/` as real files and `cp -r` works without `-L`.

### 2. Verify

```bash
file ~/.gemini/skills/llm-d-autoconfig/scripts/autoconfig_poc.py
# Expected: "Python script, ASCII text executable" not "symbolic link"
```

Reload the skill list (`/skills reload` in Gemini CLI, or restart the agent for tools that don't have a reload command).

### 3. Activate the skill

Describe a task:

> Help me configure llm-d for my workload.

The skill activates on demand and walks the phases:

| Phase | What happens |
|---|---|
| 1. Cluster discovery | Reads your kubectl context (GPUs, CRDs, existing pods) |
| 2. Discovery questionnaire | Model, topology, SLA, workload shape, feature flags |
| 2.5. Doc-driven synthesis | Fetches the relevant `llm-d` guide values via `doc_cache.py`, quotes them, builds a plugin recommendation |
| 3. Recap | Single block summarizing every input for user review|
| 4. Call the script | Renders `EndpointPickerConfig` + benchmark config + bundle |
| 5. Present + ask about deploy | Tier-tagged narration of every output value |
| 6. Deploy (optional) | Bundle path: `kubectl apply -f <bundle-dir>` after each step is confirmed |
| 7. Benchmark (optional) | Runs the harness from `autoconfig-benchmark.yaml` |

The skill is fully self-contained. All upstream docs are fetched on demand by `doc_cache.py` (version-stamped, hash-keyed, with stale-fallback).

---

## Run the script directly (no agent)

Requires Python ≥ 3.10 and `pyyaml`. The canonical script lives in the skill bundle:

```bash
python3 skill/llm-d-autoconfig/scripts/autoconfig_poc.py \
    --input examples/input-balanced-chat.json --render-yaml
```

stdout: structured JSON output with `decisions`, `rationale`, `parameters`, `warnings`. stderr: the rendered EPP YAML.

### Render the deployable YAMLs (Phase C)

> **`helm` is required only to generate the YAMLs. The script runs `helm template` against the upstream `llm-d-router` chart, layering in the upstream values files to render the EPP/gateway resources, and writes them as raw YAMLs in the target directory. No `helm install` or `helm upgrade` is ever called since helm is used only as the template engine.** The YAMLs that land in `<parent>/autoconfig-<TIMESTAMP>/` are plain Kubernetes manifests. Re-applying them, rolling back, or shipping them to another cluster needs only `kubectl`. The output is byte-deterministic per (input + `--chart-version` + `--llm-d-ref` + `--llm-d-router-ref`), so the natural workflow is:
>
> 1. Generate once on a build machine (helm + kubectl on PATH).
> 2. Save the output directory as a deploy record.
> 3. Apply with `kubectl apply -f <output-dir>` from any environment with kubectl configured into the cluster.

Generation defaults: `llm-d-router` chart `v0` (rolling `-dev` OCI tag), `llm-d` ref `main`, `llm-d-router` ref `main`. The skill tracks `main` because the router chart and its router-schema guide values only live there. Override with `--chart-version`, `--llm-d-ref`, `--llm-d-router-ref` to pin a tag or SHA (note: pinning to an older tag falls back to the deprecated GIE chart, which is vLLM-only).

```bash
python3 skill/llm-d-autoconfig/scripts/autoconfig_poc.py \
    --input examples/input-pd-gateway-features.json \
    --bundle-dir /tmp/autoconfig
# Creates /tmp/autoconfig/autoconfig-<TIMESTAMP>/ with one YAML per resource
# plus autoconfig-benchmark.yaml and a README.md. Apply with:
kubectl apply -f /tmp/autoconfig/autoconfig-*/
```

Typical output kinds: `CustomResourceDefinition` (Gateway API + GIE + optional istio), `Namespace`, `Secret` (HF token scaffold, empty by default), `ConfigMap`, `Deployment`, `InferencePool`, `Service`, `ServiceAccount`, `Role`/`RoleBinding`, `Gateway`, plus `HTTPRoute` / `DestinationRule` in gateway mode and `VariantAutoscaling` / `HorizontalPodAutoscaler` / `InferenceObjective` / `InferenceModelRewrite` / `Kustomization` per the Phase B flag.

---

## Run tests

Stdlib only:

```bash
python3 -m unittest discover -s tests
```

170 tests across: fixture diffs (4 input/output pairs including PD+gateway+features), schema validation, recommendation plugin object-form, classifier + workload signals, optimized-baseline / PD-disaggregation / predicted-latency-slo canonical parity, latency-tight scaffolding, autotune fallback, correctness questions, Phase B feature flags (12 tests), Phase B chart toggles (3), precise-prefix guard (3), Phase C context schema (5), Phase C bundle renderer (12), SKILL structure lint (7), feature_docs.yaml lint (5), determinism, and bundled-scripts-exist.

To verify all `feature_docs.yaml` URLs resolve (runs HEAD against every URL):

```bash
python3 skill/llm-d-autoconfig/scripts/verify_doc_map.py
```

---

## Project Structure

```
autoconfig/
├── README.md                       # this file
├── docs/
│   ├── SUPPORT.md                  # guide-by-guide coverage matrix
│   └── AIC_INTEGRATION.md          # design proposal to integrate with NVIDIA AIConfigurator
├── examples/                       # canonical input/output for each scenario
│   ├── input-balanced-chat.json
│   ├── input-rag-style.json
│   ├── input-latency-tight.json
│   ├── input-pd-gateway-features.json
│   └── output-*.json
├── tests/
│   └── test_poc.py                 # stdlib unittests
└── skill/
    └── llm-d-autoconfig/           # agentskills.io-format skill bundle
        ├── SKILL.md                # entry point (~100 lines) + navigation
        ├── feature_docs.yaml       # single URL map, tracking llm-d main
        ├── references/             # phase-by-phase runbooks (split for context-window economy)
        │   ├── phase-1-cluster-discovery.md
        │   ├── phase-2-discovery-questionnaire.md
        │   ├── phase-2-5-doc-driven-synthesis.md
        │   ├── phase-3-recap.md
        │   ├── phase-4-call-script.md
        │   ├── phase-5-present-recommendation.md
        │   ├── phase-6-deploy.md
        │   ├── phase-7-benchmark.md
        │   └── pitfalls.md
        └── scripts/
            ├── autoconfig_poc.py   # canonical recommender + bundle renderer
            ├── benchmark.py        # canonical benchmark config builder
            ├── doc_cache.py        # version-stamped doc fetcher + cache
            └── verify_doc_map.py   # HEAD-checks all URLs in feature_docs.yaml
```

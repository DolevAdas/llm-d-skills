# Phase 2 — Discovery questionnaire (FIXED, MANDATORY)

*Detailed runbook for SKILL.md Phase 2. Read this before asking the user the first questionnaire question.*


A fixed, ordered questionnaire. **Always ask every question in order. Don't skip. Don't infer. Don't proceed past Phase 2 until every required question has an answer.**

For each question, the format marker indicates the expected answer type:
- **[CHOICE]** — present numbered options; user picks one
- **[FILL-IN]** — user types a value; default in brackets if any
- **[YES/NO]** — literal yes/no
- **[OPTIONAL]** — script tolerates null; ask anyway, but accept "skip" / "I don't know"

If a question has a default, present the default and let the user accept by saying "default" / "yes" / "OK". If a question is required and has no default, the user MUST provide a value — do not continue otherwise.

Questions are grouped into 5 named sections (Model & Topology, Deployment Target, Workload Profile, Workload Traits, Optional Features). Present one section at a time with its full name as a header; wait for answers; move to the next. **When telling the user you're skipping or jumping between sections, always use the descriptive name** — say "I'll skip Section 1 (Model & Topology) since your model servers are running, and start with Section 2 (Deployment Target)" rather than just "skipping Block A". The user has no internal map of which letter or number means what.

---

### Q0 — Branch on existing-vs-new model servers (only if Phase 1 found running pods)

**Skip this question entirely if Phase 1 found NO model server deployments — go straight to Section 1 (Model & Topology).**

If Phase 1 found one or more model server deployments, ask:

> "I see existing model server pods running. Are you:
> (1) **Configuring autoconfig for these existing pods** — I'll use what's already running and skip ahead to deployment context (Section 2). (recommended)
> (2) **Deploying a new set of model server pods** — I'll ask you the full Section 1 (Model & Topology) questions; I'll suggest the existing values as defaults but you can override."

If Phase 1 found multiple model server deployments, list them numbered and add: "(0) ...which one am I configuring for?" — get the user's pick first, then ask (1)/(2) for that one.

**If user picks (1) — configuring for existing pods:**
- Skip Section 1 (Model & Topology) entirely. Use the values you extracted in Phase 1 directly:
  - Q1 model = the existing deployment's `--model` arg
  - Q2 GPUs = `replicas × GPUs/replica` from the deployment
  - Q3 TP = `--tensor-parallel-size` from the deployment
  - Q4 replicas = `spec.replicas`
  - Q5 max-model-len = `--max-model-len` from the deployment, or fall back to the HF config.json fetch / user ask if absent
- In Section 2 (Deployment Target), default Q6 namespace to where the model server pods live (don't ask the user to pick a different namespace — that would split the EPP from the pods it's supposed to route to).
- Continue with Blocks B → E.

**If user picks (2) — deploying new model servers:**
- Run Section 1 (Model & Topology) as written below, BUT seed each question's default with the existing deployment's value where applicable. Frame: "Q1 model? [default: same as existing — `Qwen/Qwen3-32B`]". User can accept or override.

**Record `context.modelserver_deploy_planned`** in the input JSON: `false` when user picked (1) (configure for existing pods), `true` when user picked (2) OR when Phase 1 found no existing pods. Phase 3's schedulability audit branches on this — `false` skips the density math entirely.

---

### Q0.5 — HuggingFace token Secret (BLOCKING; always ask)

Most models pull weights from HuggingFace, and the benchmark Job downloads the tokenizer from HF for prompt-token counting. Even when the model is public, public-tokenizer downloads sometimes 401 in rate-limited windows. Ask up front rather than discovering the need at apply time.

Build the option list from Phase 1's HF Secret enumeration:
- If Phase 1 found candidate Secrets (names matching `hf|hugging` in target ns), include each as a "use existing" option
- ALWAYS include "scaffold new" and "skip" options regardless of what Phase 1 found
- If a Secret literally named `llm-d-hf-token` already exists, the "scaffold new" option is suppressed (would no-op anyway — autoconfig wouldn't render the scaffold to avoid clobbering the existing real value)

Example rendered question (Phase 1 found `hf-token-secret` in target ns; no existing `llm-d-hf-token`):

```json
[{"header": "HF token", "question": "HuggingFace token Secret. Models and bench tokenizers download from HF. Pick one:", "type": "choice", "options": [
  {"label": "Use existing `hf-token-secret`", "description": "Reference the Secret already in your namespace. No render, no clobber risk."},
  {"label": "Scaffold new `llm-d-hf-token`", "description": "autoconfig renders an empty Secret. You fill it via `kubectl edit secret llm-d-hf-token -n <ns>` before apply (gated models) or apply as-is (public models). Edit it after apply, NOT before — kubectl apply would overwrite a filled token with empty."},
  {"label": "I'll create it myself", "description": "Provide a name and create it separately. autoconfig will reference the name but won't manage the Secret."},
  {"label": "Skip — public model", "description": "No Secret wired. Bench Job omits HF_TOKEN entirely. Works for most public models."}
]}]
```

Record as `context.hf_secret_name` (string or null) and `context.hf_secret_exists` (boolean):
- Use-existing → `hf_secret_name = <chosen name>`, `hf_secret_exists = true`
- Scaffold new (only offered when `llm-d-hf-token` doesn't already exist) → `hf_secret_name = "llm-d-hf-token"`, `hf_secret_exists = false`
- I'll create myself → ask follow-up text field for the name; `hf_secret_name = <user input>`, `hf_secret_exists = false`
- Skip → `hf_secret_name = null`, `hf_secret_exists = false`

The scaffold IS rendered in `render_bundle` iff `hf_secret_name == "llm-d-hf-token" AND hf_secret_exists = false` — every other branch references the Secret but doesn't render it. See `Context.hf_secret_name` docstring in autoconfig_poc.py for the clobber-safety rationale.

---

### Section 1 — Model & Topology (6 questions)

**Q1. Model HuggingFace ID** [FILL-IN, REQUIRED]
> "What model are you serving? Give me the fully-qualified HuggingFace ID (e.g. `Qwen/Qwen3-32B`, not just `Qwen`)."

If the user gives a family name only, ask a disambiguating follow-up before accepting. Auto-discoverable from cluster (`--model` arg in vLLM deployment) — confirm rather than assume.

**Q2. Total GPU count** [FILL-IN, REQUIRED, default = total cluster GPUs from Phase 1]
> "Total GPUs to use? [default: <total cluster GPU count from Phase 1, e.g. 104>]"

The default MUST be the cluster's total GPU count from Phase 1's node enumeration — not a generic placeholder like "8". If Phase 1 found 104 GPUs across 13 nodes, Q2's default is 104. If the user wants to use fewer, they say so.

**Q3. Tensor parallelism (TP) per replica** [CHOICE, REQUIRED, default suggested by model size]
> "TP per replica?
> (1) TP=1 (default for ≤8B models)
> (2) TP=2 (default for ≤32B models)
> (3) TP=4 (default for ≤70B models)
> (4) TP=8 (default for 100B+ models)
> Default for your model: TP=<N>. Or specify a number."

**Q4. Number of replicas** [FILL-IN, REQUIRED, default = floor(GPUs/TP)]
> "Replicas? With <gpus> GPUs at TP=<tp>, max is <floor(gpus/tp)>. [default: max]"

**Q4.5. Topology** [CHOICE, REQUIRED, default = agg]

**You ALWAYS offer both options regardless of cluster capability.** PD with TCP fallback is a valid, supported path — useful for testing the PD code path on non-RDMA hardware, validating the autoconfig flow, and for some workload shapes (heterogeneous parallelism wins can outweigh KV-transfer cost; the only way to know is to benchmark). Never skip the PD option just because `RDMA_AVAILABLE = false`. The agent is not a gatekeeper here.

The transport (`rdma` / `tcp`) is set automatically from Phase 1's `RDMA_AVAILABLE` — never ask the user about transport. Ask the user (choice):

```json
{"header": "Topology", "question": "Topology?", "type": "choice", "options": [
  {"label": "Aggregated", "description": "Single deployment per replica — Q3/Q4 values describe the pool. Default; works on any cluster."},
  {"label": "Prefill/Decode disaggregated", "description": "Two pools — prefill workers do compute-heavy prefill, decode workers do per-token generation, KV transferred via NIXL between them. RDMA-capable on this cluster: <RDMA_AVAILABLE from Phase 1: yes/no>. With RDMA: NIXL uses RoCE for KV transfer (matches pd-disaggregation guide's published throughput). Without RDMA: NIXL uses TCP — functional path, supported by autoconfig, useful for validating the PD code path on this cluster; perf vs agg is workload-dependent so benchmark both before committing."}
]}
```

If the user picks aggregated → skip Q4.6, continue to Q5. The Q3 / Q4 values are the topology.

If the user picks PD → ask Q4.6 (PD sizing). The Q3 / Q4 answers don't apply directly because PD splits the GPU budget across two pools.

**Q4.6. PD sizing** [CHOICE, REQUIRED if Q4.5 = PD]

Auto-scale logic (you compute the defaults from Q2 GPU budget, the model size you fetched in Phase 1, and the canonical pd-disaggregation guide's principles):

| Decode TP rule | Source |
|---|---|
| Model ≤ 30B → decode TP=2 | T4: model fits one H200 at FP16; TP=2 doubles KV |
| Model 30-70B → decode TP=2 | T4: borderline; TP=2 keeps GPU count low |
| Model ≥ 70B → decode TP=4 | T3: matches canonical pd-disaggregation guide for gpt-oss-120b |

| Prefill TP rule | Source |
|---|---|
| Always start at prefill TP=1 | T3: matches canonical guide ("less parallelism, more replicas") |
| Override to decode_tp if model doesn't fit single GPU | T4: prefill must be runnable |

| GPU split rule (P:D ratio) | Source |
|---|---|
| ISL/OSL ≥ 5 (RAG-shaped) → 4:1 P:D GPUs | T3: canonical recipe ratio |
| 1 ≤ ISL/OSL < 5 (balanced) → 2:1 | T4: prefill bias still wins for "longer ISL than OSL" |
| ISL/OSL < 1 (generation-heavy) → 1:1 | T4: balanced split when output dominates |
| No ISL/OSL → 4:1 default | T4: default to RAG-shape, the published recipe assumption |

Then: `prefill_replicas = floor((gpus * P_share) / prefill_tp)`, `decode_replicas = floor((gpus * D_share) / decode_tp)`. Clamp both ≥ 1.

For 16 GPUs / gpt-oss-120b / no ISL-OSL: 4:1 split → 12.8 GPUs prefill, 3.2 decode → at TP=1/TP=4 → 12 prefill replicas, 0 decode (rounds to 0 — clamp ≥ 1: 1 decode using 4 GPUs, leaving 12 GPUs prefill). Or honor the canonical exactly: 8P×1 + 2D×4 = 16. **For Q2=16 GPUs, prefer the canonical exactly** — use it as a tiebreaker when the GPU budget matches.

```json
{"header": "PD sizing", "question": "PD sizing? Computed defaults: prefill <N>×TP=<X>, decode <M>×TP=<Y>. Total GPUs used: <N*X + M*Y>. Accept default or override?", "type": "choice", "options": [
  {"label": "Accept default", "description": "Use the auto-scaled values above (anchored to the canonical pd-disaggregation guide for gpt-oss-120b, scaled by your GPU budget and ISL/OSL ratio)."},
  {"label": "Override", "description": "Specify prefill_replicas / prefill_tp / decode_replicas / decode_tp manually."}
]}
```

If user picks Override, ask 4 follow-up FILL-IN questions for each value (split into 2 calls of 2: prefill_replicas + prefill_tp, then decode_replicas + decode_tp). Validate that `prefill_replicas * prefill_tp + decode_replicas * decode_tp ≤ Q2 GPUs`.

Record the PD answers as `topology.prefill_replicas / prefill_tp / decode_replicas / decode_tp`. Set `topology.pd_transport = "rdma"` if Phase 1's `RDMA_AVAILABLE` was true, else `"tcp"`. The user is never asked about transport directly.

**Q5. Model context length (tokens)** [FILL-IN, REQUIRED — but you MUST attempt to auto-discover before asking]

Mandatory pre-step: before presenting Q5 to the user, try the two auto-discovery paths in order. Both are quiet — no chat output, just commands. Only ask the user if both fail.

1. If Phase 1 surfaced vLLM's `--max-model-len` from a cluster deployment, use that.
2. Otherwise, fetch `https://huggingface.co/<model-id>/raw/main/config.json` and read `max_position_embeddings`. Should succeed for any public model.

If a value comes back from either path, present Q5 with the discovered value as the placeholder/default — the user almost always accepts. If both paths fail (private/gated model, network blocked), present Q5 as a plain text question.

If the user still can't supply, leave the field blank — script falls back to the plugin default.

**Q5.5. Benchmark tokenizer override** [CHOICE, conditional — only ask if the Q5 HF `config.json` fetch returned 404]

Triggered when Phase 1's HF lookup couldn't find the model (proprietary weights from PVC / GCS / S3 / private HF org / pre-baked image). The benchmark Job needs to download an HF tokenizer to count prompt tokens — for proprietary fine-tunes, the tokenizer is usually still public (the base model the variant derives from). Ask:

```json
[{"header": "Tokenizer", "question": "I couldn't find this model on HuggingFace. The benchmark Job downloads an HF tokenizer to count prompt tokens. Pick:", "type": "choice", "options": [
  {"label": "Specify an HF tokenizer", "description": "Usually the base model your variant derives from (e.g. `meta-llama/Llama-3.1-8B-Instruct` for a proprietary Llama variant). I'll ask for the name."},
  {"label": "Skip benchmark for this run", "description": "Autoconfig generates the EPP/deploy artifacts but skips the benchmark Job. You run your own benchmark separately."}
]}]
```

If "Specify", ask a follow-up text field for the tokenizer HF id and record as `context.bench_tokenizer_override`. The bench Job's `tokenizer.pretrained_model_name_or_path` will use this value instead of the served model id.

If "Skip benchmark", set a sentinel that build_benchmark_deployment respects to omit bench artifacts entirely. The Phase 5 deploy/benchmark choice prompt also won't offer benchmark options when this is set.

If the model IS findable on HF (Q5's curl returned 200/numeric), skip Q5.5 entirely — `context.bench_tokenizer_override` stays null and the bench uses the served model id as the tokenizer.

---

### Section 2 — Deployment Target (3-4 questions)

**Q6. Namespace** [FILL-IN, REQUIRED, default per Phase 1]
> "Namespace? [default: <existing-ns from Phase 1, or 'default'>]"

**Q7. Helm release name** [FILL-IN, REQUIRED, default per Phase 1]
> "Release name? [default: <existing-release from Phase 1, or 'llm-d'>]"

**Q8. Deploy mode** [CHOICE, REQUIRED]
> "How should the EPP be exposed?
> (1) Standalone (default — EPP as a regular Service, reachable via ClusterIP or port-forward; simplest, no Gateway provider needed)
> (2) Gateway (production-style — EPP behind a Kubernetes Gateway managed by Istio/Kgateway/etc.; requires a Gateway resource to already exist OR an extra install step)
> "

If Phase 1 found a GatewayClass but no Gateway resource, push standalone as the default and mention: "I see an `istio` GatewayClass but no Gateway resource. Standalone is the simpler path; gateway mode would need a Gateway resource installed first."

Record the answer as `DEPLOY_MODE` (one of `standalone` or `gateway`). Phases 6.4 / 6.5 / 6.6 branch on this.

**Q8.5. Gateway provider** [CHOICE, REQUIRED if Q8 = gateway; SKIP if Q8 = standalone]

Asked immediately after Q8 when the user picks Gateway Mode — owning this in Phase 2 means the recap in Phase 3 shows the user-confirmed provider, not an inference from cluster discovery. Don't defer to Phase 6.1.

Build the option list dynamically from Phase 1 findings:
- On GKE clusters (context starts with `gke_`), include both `gke-l7-rilb` and `gke-l7-regional-external-managed` first.
- Always include `istio` and `agentgateway` regardless of cluster.

For each option, **construct the `description` as `<status>. <static blurb>.`** where `<status>` is ONE of the bracketed strings below, picked by the agent based on what Phase 1 found. Don't include the `|` separators or `<>` brackets in the rendered text — they're only here to enumerate the cases.

| Provider | Status string (pick one) | Static blurb |
|---|---|---|
| `gke-l7-rilb` | `Ready to use` / `Cluster ready, proxy-only subnet missing — autoconfig will create one` / `GKE Gateway API not enabled — autoconfig will enable` / `Needs install` | GKE managed L7 internal LB. VPC-only access. Recommended for in-VPC traffic on GKE. |
| `gke-l7-regional-external-managed` | (same set as above) | GKE managed L7 external LB. Internet-accessible. Recommended for public endpoints on GKE. |
| `istio` | `Ready to use` / `Installed but missing GAIE flag — autoconfig will reinstall` / `Needs install` | Istio gateway controller. Installs istiod cluster-wide via istioctl. Required if you have an existing service mesh. |
| `agentgateway` | `Ready to use` / `Needs install via helm chart` | Rust-based AI gateway purpose-built for LLM/MCP/A2A workloads. |

Example rendered question (cluster has GKE API enabled but no proxy-only subnet, no istio, no agentgateway):

```json
[{"header": "Gateway type", "question": "Which gateway provider should provision the Gateway resource?", "type": "choice", "options": [
  {"label": "gke-l7-rilb", "description": "Cluster ready, proxy-only subnet missing — autoconfig will create one. GKE managed L7 internal LB. VPC-only access. Recommended for in-VPC traffic on GKE."},
  {"label": "gke-l7-regional-external-managed", "description": "Cluster ready, proxy-only subnet missing — autoconfig will create one. GKE managed L7 external LB. Internet-accessible. Recommended for public endpoints on GKE."},
  {"label": "istio", "description": "Needs install. Istio gateway controller. Installs istiod cluster-wide via istioctl. Required if you have an existing service mesh."},
  {"label": "agentgateway", "description": "Needs install via helm chart. Rust-based AI gateway purpose-built for LLM/MCP/A2A workloads."}
]}]
```

Record the answer as `GATEWAY_PROVIDER`. Used by:
- Phase 3 recap (shows the user's chosen provider explicitly)
- Phase 6.1's controller-install branch (only runs if `GATEWAY_PROVIDER` needs installing)
- Phase 6.4 helm install (`--set provider.name=<GATEWAY_PROVIDER>`)
- Phase 6.5 Gateway resource (`gatewayClassName: <GATEWAY_PROVIDER>`)

---

### Section 3 — Workload Profile (7 questions, all optional)

**Q9. SLA: TTFT target (p95, milliseconds)** [FILL-IN, OPTIONAL]
> "Target time-to-first-token p95? Skip if you don't have one."

**Q10. SLA: TPOT target (p95, milliseconds)** [FILL-IN, OPTIONAL]
> "Target time-per-output-token p95? Skip if you don't have one."

**Q11. SLA: end-to-end request latency (p95, milliseconds)** [FILL-IN, OPTIONAL]
> "Target full request latency p95? Skip if you don't have one."

**Q12. Typical input length (tokens)** [FILL-IN, OPTIONAL]
> "Typical prompt length? Best estimate is fine. Skip if unknown."

**Q13. Typical output length (tokens)** [FILL-IN, OPTIONAL]
> "Typical output length? Skip if unknown."

**Q14. Prefix share** [CHOICE, default low]
> "Do prompts share a long prefix (e.g. system prompt, RAG context)?
> (1) low (default — most prompts are independent)
> (2) medium
> (3) high
> "

**Q15. Shared prefix length (tokens)** [FILL-IN, OPTIONAL — only ask if Q14 = medium/high]
> "How long is the shared prefix? Skip if unsure."

For any question the user skips, leave the corresponding field null in the script input. The script tolerates null and warns when fall-through plugin defaults apply.

---

### Section 4 — Workload Traits (4 yes/no questions, all default no)

> "Quick checklist — most chat workloads say no to all. Each one only adds a plugin if you say yes:"

**Q16. Multi-turn conversations needing session affinity?** [YES/NO, default no]

**Q17. Serving LoRA adapters?** [YES/NO, default no]

**Q18. Pods with different supported context lengths in the same pool?** [YES/NO, default no]

**Q19. Active-active EPP HA (multiple scheduler replicas)?** [YES/NO, default no]

---

### Section 5 — Optional Features (8 questions, all default off)

Ask these in two batches (most agent question tools cap at 4 per call):

**Batch A — core features (Q20–Q22):** latency predictor + precise prefix cache.
**Batch B — Phase B feature toggles (Q26.5–Q31):** tiered cache, flow control, wide-EP, autoscaler, serving pattern. All default off — most users skip.

For every yes answer here, Phase 2.5's lookup table must produce a doc-read entry. If you find yourself wanting to enable a flag whose Phase 2.5 row is "Search `plugins.*` for ..." (no direct map), STOP and add a feature_docs.yaml entry first — flags without docs can't satisfy Hard Rule 8.



**Q20. Enable latency-prediction-based routing?** [YES/NO, default no]

If YES, surface trade-offs first so the user knows what they're opting into. Numbers are from the chart defaults at `llm-d-router/config/charts/epplib/values.yaml`:
> "Trade-offs:
> - Adds 2 sidecar containers per EPP pod at default settings (1 training-server + 1 prediction-server; `predictionServers.count` is configurable). All in-pod via the chart's `latencyPredictor.enabled` toggle.
> - Resource ask per EPP pod: ~8 Gi memory requested / 16 Gi limit, ~10 CPU requested, plus ~30 Gi of emptyDir volume (20 Gi training + 10 Gi prediction). Startup time +30-60s for sidecars to load XGBoost models.
> - Pool homogeneity required (same GPU type, model, serving config across pods)
> - Compatible with PD-disagg — the producer auto-neutralizes TPOT for prefill pods when `endpointRoleLabel` is set (which autoconfig wires automatically under PD)
> - Untested with LoRA, speculative decoding, beam search
> Still want it on?"

**Q21. (Only if Q20=yes) Latency predictor streamingMode** [CHOICE, default false]
> "(1) false (default — trains on end-to-end request latency; works with both streaming and non-streaming clients)
> (2) true (trains separate TTFT + TPOT models; requires streaming clients with `\"stream\": true`)"

**Q22. Enable precise prefix cache scoring?** [YES/NO, auto-yes if Q14=high; otherwise default no]
> "Use precise prefix-cache scoring (KV-events-driven) instead of estimation? Higher cache hit rates but requires vLLM's `--kv-events-config` flag and matching block_size + PYTHONHASHSEED."

**Q23. (Only if Q22=yes) vLLM block_size** [FILL-IN, REQUIRED, auto-discoverable]
Source priority: cluster discovery → HF model defaults → ask user. Refuse to enable Q22 without this.

**Q24. (Only if Q22=yes) vLLM PYTHONHASHSEED** [FILL-IN, REQUIRED, auto-discoverable]
Source priority: cluster discovery (`PYTHONHASHSEED` env on the deployment) → ask user. **No safe default — mismatch silently breaks the cache.** Refuse to enable Q22 without this.

If Phase 1 found `vllm:cache_config_info` is NOT exported (autoTune unsupported), insert two more questions here:

**Q25 (conditional). vLLM blockSizeTokens** [FILL-IN, REQUIRED]
Source priority: vLLM startup logs (`Total number of GPU blocks: N`) → vLLM CLI `--block-size` → ask user. The plugin default of 16 is rarely optimal.

**Q26 (conditional). vLLM lruCapacityPerServer** [FILL-IN, REQUIRED]
Source priority: vLLM startup log line `Total number of GPU blocks: N` → compute from HBM math → use plugin default 31250 with explicit warning.

---

#### Section 5, Batch B — Phase B feature toggles (Q26.5–Q31, all default off)

These map to `features.*` fields the script accepts. Today the script renders an advisory warning per flag (pointing at the relevant `feature_docs.yaml` entry) and includes the flag in input_hash; helm-values rendering for these comes in Phase C. Each enabled flag is a signal to **Phase 2.5** that the agent should read the listed `feature_docs.yaml` entries and incorporate them into `recommendation`.

**Q26.5. Enable tiered prefix cache offload?** [YES/NO, default no]
> "Tiered cache offloads KV blocks to CPU/SSD/remote FS for larger effective context. Modelserver-side overlay (not EPP config). Pulls in extra resource asks per pod and depends on a connector being available on your nodes. Phase 2.5 reads: `guides.tiered_prefix_cache`."

Sets `features.enable_tiered_cache=true`.

**Q27. Enable flow control (priority bands, fairness, saturation)?** [YES/NO, default no]
> "Flow control adds an admission/queueing layer with priority bands. Useful for shared multi-tenant pools. Adds plugins to `plugins[]`. Phase 2.5 reads: `guides.flow_control`."

Sets `features.enable_flow_control=true`.

**Q28. Enable wide expert-parallelism (LeaderWorkerSet)?** [YES/NO, default no — auto-yes if model is sparse MoE]
> "Wide-EP runs sparse-MoE models across multiple pods via LeaderWorkerSet. Modelserver-side topology (not EPP). Required for very large MoE; ignored for dense models. Phase 2.5 reads: `guides.wide_ep_lws`."

Sets `features.enable_wide_ep=true`.

**Q29. Autoscaler choice** [CHOICE, default none]
> "Runtime replica autoscaling?
> (1) None (default — fixed replicas, what you sized in Section 1)
> (2) WVA — Workload Variant Autoscaler. Cost-optimized; picks the best variant across heterogeneous accelerators. Operator install required. Phase 2.5 reads: `guides.workload_autoscaling_wva`.
> (3) HPA-EPP — vanilla Kubernetes HPA driven by EPP-emitted Prometheus metrics. Simpler. Phase 2.5 reads: `guides.workload_autoscaling_hpa`."

Maps to `features.autoscaler` ∈ {null, `"wva"`, `"hpa"`}.

**Q30. Serving pattern** [CHOICE, default sync]
> "How does this stack serve requests?
> (1) sync (default — request/response, typical chat)
> (2) batch — long-running, batch-style jobs via the batch-gateway pattern. Phase 2.5 reads: `guides.batch_gateway`.
> (3) async — background/long-running request processing. Phase 2.5 reads: `guides.asynchronous_processing`."

Maps to `features.serving_pattern` ∈ {`"sync"`, `"batch"`, `"async"`}.

**Q31. Use InferenceObjective / InferenceModelRewrite CRDs?** [CHOICE, default neither, multiSelect=true]
> "Optional CRDs the GAIE API supports:
> - InferenceObjective — per-model routing priority/objectives. Useful when one EPP fronts multiple models.
> - InferenceModelRewrite — rewrite incoming model names to canonical pool entries.
> Most single-model deploys skip both."

Set `features.enable_inference_objective=true` / `features.enable_model_rewrite=true` per selection. Phase 2.5 reads: `schemas.inferenceobjective_crd`, `schemas.inferencemodelrewrite_crd`.

**Q32. Workload trait: multimodal?** [YES/NO, default no]
> "Are inputs multimodal (image+text, audio, etc.)? Affects tokenizer + benchmark realism."

Sets `workload_traits.multimodal=true`.

---

After Section 5 Batch B, you have all inputs needed. Move to Phase 2.5 (doc-driven recommendation synthesis), THEN Phase 3 (recap + confirmation). Skip the legacy single-feature subsections below — they're now covered by the Section 5 questions.

### Reference: feature-flag deep dives

The following subsections are reference material if the user asks for more detail on a feature flag. Do NOT walk through them as part of the standard discovery flow.

#### `enable_latency_predictor` (Q20) deep dive

**This is a feature toggle, not a multi-step deploy coordination.** The llm-d-router chart's `router.latencyPredictor.enabled: true` value (which Phase 6.4 sets when this flag is on) does ALL the heavy lifting automatically:

- Deploys 1 training-server + 3 prediction-server sidecar containers in the EPP pod
- Sets `TRAINING_SERVER_URL=http://localhost:8000` and `PREDICTION_SERVER_URL=http://localhost:8001,http://localhost:8002,http://localhost:8003` on the EPP container
- Mounts emptyDir volumes for sidecar model storage
- Provisions ConfigMaps for sidecar config

**Source of truth:** `config/charts/epplib/templates/_latency-predictor.tpl` in the EPP repo. Don't ask the user to deploy sidecars or set env vars — the chart handles it.

When asking, frame it as a feature decision with real trade-offs (not a prereq checklist):

> "Use latency-prediction-based routing? It adds 2 sidecar containers per EPP pod at chart defaults (1 training-server + 1 prediction-server; `predictionServers.count` configurable), all in-pod via the chart's `latencyPredictor.enabled` toggle. Trade-offs:
>
> - **Resource ask per EPP pod**: ~8 Gi memory requested / 16 Gi limit, ~10 CPU requested, plus ~30 Gi of emptyDir volume (20 Gi training + 10 Gi prediction). Startup +30-60s for sidecars to load XGBoost models.
> - **Pool homogeneity required**: predictions assume all pods have the same GPU type, model, and serving config. Mixed pools give bad predictions.
> - **Throughput depends on `predictionServers.count` and pod resources.** No freshly-validated QPS-per-sidecar number to cite — benchmark with your traffic shape if capacity-planning matters.
> - **PD-disagg compatible**: the producer auto-neutralizes TPOT for prefill pods when `endpointRoleLabel` is set, which autoconfig wires automatically under PD topology.
> - **Untested with LoRA, speculative decoding, beam search.** Predictions may be inaccurate with these features on.
>
> Want it on? If yes, I'll set `latencyPredictor.enabled=true` in the helm install."

#### `streamingMode` choice

If the user enables the predictor, ALSO ask which mode to use (the `streamingMode` parameter on the `predicted-latency-producer` plugin):

> "One more question: streamingMode false (default) or true?
>
> - **false (default)**: trains on **end-to-end request latency** at EOS. Works with both streaming AND non-streaming clients. Routes by predicted e2e latency. TPOT auto-neutralized in scoring.
> - **true**: trains separate **TTFT** (on first chunk) + **TPOT** (sampled across tokens) models. Routes by predicted TTFT and TPOT independently. **Requires streaming clients** (`\"stream\": true` in request body) — non-streaming requests skip training data collection in this mode.
>
> Pick true only if your clients are reliably streaming AND you want per-token routing decisions. Otherwise stick with false."

#### Per-request opt-in via header

Worth mentioning to the user but not asking them to configure: the EPP can expose multiple scheduling profiles. When latency-predictor is on and there's an `slo` profile defined, requests opt into it per-request via the `x-prediction-based-scheduling: true` header. Without the header, requests use the default profile (no prediction). This means the user can run mixed traffic — some requests use prediction-based routing, others don't.

#### Optional EPP env-var tunables

The user can override these via `router.latencyPredictor.eppEnv` in the chart values. Don't ask up-front; mention they exist if the user asks about tuning:

- `HEADROOM_SELECTION_STRATEGY=least|most` — bin-pack vs spread under SLO headroom
- `HEADROOM_TTFT_WEIGHT` / `HEADROOM_TPOT_WEIGHT` — blend weights for positive headroom
- `NEG_HEADROOM_TTFT_WEIGHT` / `NEG_HEADROOM_TPOT_WEIGHT` — blend weights for SLO-violating endpoints
- `SLO_BUFFER_FACTOR` — safety multiplier on TPOT SLOs
- `LATENCY_FLUSH_INTERVAL_SEC`, `LATENCY_MAX_BULK_SIZE`, `LATENCY_HTTP_TIMEOUT_SEC` — predictor client tunables

#### Verifying it's working post-deploy

The EPP exposes a `/metrics` endpoint that reports prediction stats, but **Envoy's default config requires authentication for the metrics port** — a plain `kubectl port-forward` + `curl` returns `Unauthorized`. Trying to verify via `/metrics` is a dead end without a Prometheus scraper.

Use sidecar logs instead. They prove the system is wired and producing predictions:

```bash
# Step 1: training-server should be ingesting training data after the first request
kubectl logs -n <ns> -l app.kubernetes.io/instance=<release-name> -c training-server --tail=50
# Look for: "received N training samples", "model retrained", "model saved"

# Step 2: prediction-server-1 (and -2, -3) should be answering predict requests
kubectl logs -n <ns> -l app.kubernetes.io/instance=<release-name> -c prediction-server-1 --tail=50
# Look for: "Loaded model", request lines for /predict, response sizes

# Step 3: EPP container should log scoring decisions involving latency predictions
kubectl logs -n <ns> -l app.kubernetes.io/instance=<release-name> -c epp --tail=100 | grep -i "latency\|prediction\|headroom"
# Look for: "Pod score" entries with scorer_type:"latency-scorer" / "slo-headroom-tier-filter"
```

If the training and prediction logs show activity AND the EPP logs show latency-scorer decisions, the system is working end-to-end. If only the EPP logs show "no predictions, using composite fallback" repeatedly, the sidecars are reachable but the producer isn't getting predictions back — usually a model-load or version-mismatch issue surfaced in the prediction-server logs.

### `enable_precise_prefix_cache` (Q22-Q24) deep dive

Precise mode swaps the basic `prefix-cache-scorer` for `precise-prefix-cache-scorer` and pulls cache state from vLLM via KV-events instead of estimating. Higher hit rate at high throughput. Hard requirements:
- vLLM must be launched with `--kv-events-config` enabled
- The plugin's `tokenProcessorConfig.blockSize` must match vLLM's `--block-size` exactly (correctness, not performance)
- The plugin's `tokenProcessorConfig.hashSeed` must match vLLM's `PYTHONHASHSEED` env var exactly (correctness)

If either of those mismatch, the cache silently misses. There is no safe default — the script refuses to emit `precise-prefix-cache-scorer` config without both values.

---

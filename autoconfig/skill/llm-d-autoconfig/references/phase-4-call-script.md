# Phase 4 — Call the script

*Detailed runbook for SKILL.md Phase 4. Includes the working-directory setup, full input schema, and script invocation commands.*


### Step 0: pick a working directory (asked ONCE; reused everywhere)

Before writing any file, ask the user where to put generated artifacts. Per Hard Rule #4, all subsequent `<work-dir>` placeholders in this SKILL resolve to the value captured here.

```json
[{"header": "Work dir", "question": "Where should I put generated artifacts (input JSON, helm values, HTTPRoute manifest, benchmark deployment, etc.)? Leave blank for an auto-created temp dir.", "type": "text", "placeholder": "(default: auto temp dir)"}]
```

Resolve the answer to `WORK_DIR`:
- **Empty / blank / "default"** → run `mktemp -d` as its own command; capture stdout; that's `WORK_DIR`. Don't wrap in `$()`.
- **Existing directory path** → use as-is.
- **Non-existing path** → `mkdir -p <path>` first, then use it.

After capturing `WORK_DIR`, all `<work-dir>/...` references throughout the rest of the SKILL refer to this value. Don't re-ask, don't make new temp dirs later.

### Step 1: build the input JSON

Build the input JSON **strictly conforming to the schema below** and write it to `<work-dir>/autoconfig-input.json`. Common script-input mistake: passing scalars where the schema expects nested objects (e.g. `"topology": "agg"` will crash the script with `Topology() argument after ** must be a mapping`). `topology`, `workload`, `slo`, `features`, `workload_traits`, `correctness`, `context` are ALL nested objects — never strings, never arrays.

**Full input schema (all keys other than `model` + `topology` are optional; omit unknowns rather than inventing them):**

For `topology.mode = "agg"` (the optimized-baseline path):

```json
{
  "model": "Qwen/Qwen3-32B",
  "model_context_length": 32768,
  "topology": {
    "mode": "agg",
    "replicas": 8,
    "tp": 2
  },
  "workload": {
    "isl": 1000,
    "osl": 500,
    "prefix_share": "low",
    "prefix_len": null,
    "max_num_seqs": null
  },
  "slo": {
    "ttft_ms": 800,
    "tpot_ms": 25,
    "request_latency_ms": null
  },
  "features": {
    "enable_latency_predictor": false,
    "enable_precise_prefix_cache": false,
    "enable_tiered_cache": false,
    "enable_flow_control": false,
    "enable_wide_ep": false,
    "enable_inference_objective": false,
    "enable_model_rewrite": false,
    "autoscaler": null,
    "serving_pattern": "sync",
    "autotune_supported": true
  },
  "runtime": {
    "block_size_tokens": null,
    "lru_capacity_per_server": null
  },
  "workload_traits": {
    "multi_turn": false,
    "lora": false,
    "heterogeneous_context": false,
    "epp_ha": false,
    "multimodal": false
  },
  "correctness": {
    "vllm_block_size": null,
    "vllm_hash_seed": null
  },
  "context": {
    "namespace": "prod-chat",
    "release_name": "chat",
    "deploy_mode": "standalone",
    "gateway_provider": null,
    "modelserver_deploy_planned": true,
    "hf_secret_name": "llm-d-hf-token",
    "hf_secret_exists": false,
    "bench_tokenizer_override": null
  },
  "recommendation": {
    "plugins": [],
    "weights": {},
    "scheduling_profiles": [],
    "cited_sources": [],
    "summary": ""
  }
}
```

The `recommendation` object is what Phase 2.5 produced. **If Phase 2.5 left it empty (minimal inputs, no features), include the empty struct verbatim** so the script's canonical defaults kick in. If Phase 2.5 populated it, include those values exactly — do not edit or re-derive in this phase.

**`recommendation.plugins` is a list of objects in the EndpointPickerConfig shape** — the same shape the guide `values.yaml` files use and the same shape the deployed resource uses. Each entry has a required `type`:

```json
"plugins": [
  {"type": "queue-scorer"},
  {"type": "kv-cache-utilization-scorer"},
  {"type": "prefix-cache-scorer"}
]
```

This means you can copy a guide's `plugins:` list near-verbatim — no reshaping. Two optional keys per entry:
- `"name"` — a named instance (e.g. two `prefix-cache-affinity-filter` entries at different thresholds need distinct names).
- `"parameters"` — include **only** to deliberately override what the script would derive for that plugin. Normally omit it: the division of labor is *you pick which plugins; the script derives their `parameters`* (with evidence tiers + citations). A `parameters` block you supply wins over the derived one.

Per-plugin `weights` go in the separate `weights` map (keyed by plugin name); full custom profile shapes go in `scheduling_profiles`.

For `topology.mode = "disagg"` (PD), the topology block changes — the agg `replicas` + `tp` keys are dropped, replaced by the four PD fields plus `pd_transport`. Everything else is identical:

```json
"topology": {
  "mode": "disagg",
  "prefill_replicas": 8,
  "prefill_tp": 1,
  "decode_replicas": 2,
  "decode_tp": 4,
  "pd_transport": "rdma"
}
```

**Field types — get these right:**
- `model_context_length`: positive integer (token count) — or omit if unknown
- `topology.mode`: string, `"agg"` or `"disagg"`
- `topology.replicas`, `topology.tp`: positive integers (REQUIRED when `mode="agg"`, omit when `"disagg"`)
- `topology.prefill_replicas`, `topology.prefill_tp`, `topology.decode_replicas`, `topology.decode_tp`: positive integers (REQUIRED when `mode="disagg"`, omit when `"agg"`)
- `topology.pd_transport`: `"rdma"` or `"tcp"` (REQUIRED when `mode="disagg"`). Set from Phase 1's `RDMA_AVAILABLE` boolean — never ask the user; the cluster decides this.
- `workload.prefix_share`: one of `"low"`, `"medium"`, `"high"` — or omit
- `slo.*`: positive integers in milliseconds — or `null` / omit
- `correctness.vllm_hash_seed`: string (NOT integer — vLLM treats `PYTHONHASHSEED` as a string env var)
- `context.deploy_mode`: `"standalone"` (default) or `"gateway"`. Drives bundle chart variant + HTTPRoute emission. The standalone variant is sufficient for port-forward smoke tests; `gateway` mode selects the `llm-d-router-gateway` chart and requires `context.gateway_provider`.
- `context.gateway_provider`: one of `"istio"`, `"kgateway"`, `"agentgateway"`, `"gke-l7-rilb"`, `"gke-l7-regional-external-managed"`, or `null`. Required when `deploy_mode="gateway"`. Used to label the rendered HTTPRoute (`llm-d.ai/gateway-provider` annotation) and to set `--set provider.name=...` on the chart install in the step-by-step path.
- `context.modelserver_deploy_planned`: boolean (default `true`). Set `false` when Phase 2 Q0 = "configure for existing pods" — autoconfig is only deploying EPP/Gateway, the model servers already exist. Drives Phase 3 schedulability audit (skips density math when false) and informs the HF Secret scaffold logic.
- `context.hf_secret_name`: string or `null`. Set from Phase 2 Q0.5. The name of the HF token Secret the bench Job and any modelserver overlay should reference. `null` = skip HF token wiring entirely (public model). Special value `"llm-d-hf-token"` = autoconfig's default scaffold name (rendered only when `hf_secret_exists=false`).
- `context.hf_secret_exists`: boolean (default `false`). Set from Phase 1 Secret discovery — true if a Secret matching `hf_secret_name` already exists in the target namespace. Gates the scaffold rendering in `render_bundle` to prevent `kubectl apply` from clobbering an existing real token with empty stringData on re-apply.
- `context.bench_tokenizer_override`: string or `null`. Set from Phase 2 Q5.5 when the model isn't on HF (proprietary weights). Overrides the bench Job's `tokenizer.pretrained_model_name_or_path` field; null = use the served model id.
- `features.autotune_supported`: boolean — true if you verified `vllm:cache_config_info` is exported by the model server, false otherwise
- `features.autoscaler`: `null` (default, fixed replicas), `"wva"`, or `"hpa"`. Anything else is rejected.
- `features.serving_pattern`: one of `"sync"` (default), `"batch"`, `"async"`. Anything else is rejected.
- `features.enable_tiered_cache` / `enable_flow_control` / `enable_wide_ep` / `enable_inference_objective` / `enable_model_rewrite`: booleans. Each enables a Phase-B advisory warning and signals Phase 2.5 to read the corresponding `feature_docs.yaml` entry; the script does NOT render the deploy-side artifacts for these yet (modelserver overlays, autoscaler CRs, batch gateway, etc.) — Phase C will.
- `workload_traits.multimodal`: boolean — does the workload include images/audio. No dedicated guide today; Phase 2.5 surfaces this as a known gap.
- `runtime.block_size_tokens`, `runtime.lru_capacity_per_server`: positive integers — only used when `autotune_supported=false`; obtained from vLLM logs or computed

**For any field the user said "no" or "I don't know" to in Phase 4, omit it or set it to `null`.** Do not invent placeholder values like `"unknown"` or `0`.

**Validate the input file is well-formed JSON before invoking the script.** A stray backslash or a single-quoted string produces `error: input is not valid JSON: Invalid \escape`. Catch it early:

```bash
$ python3 -m json.tool <work-dir>/autoconfig-input.json > /dev/null
# Exits 0 + silent if valid; prints the offending line/column if not.
```

Common JSON pitfalls when hand-writing the file: literal backslashes in strings must be doubled (`\\`); use double quotes, never single; no trailing commas; no comments. If validation fails, fix the file and re-validate — don't pass malformed JSON to the script.

Then run the bundled recommender script. The `<input-path>` below is `<work-dir>/autoconfig-input.json` from Step 1. The script lives at `scripts/autoconfig_poc.py` relative to this skill's install location — expand to your agent's actual install tier (substitute `<skill-install-dir>` with the full path):

```bash
$ python3 <skill-install-dir>/scripts/autoconfig_poc.py \
    --input <input-path> --render-yaml
```

Common install tiers, by agent (use whichever is real on the user's system):

| Agent | Workspace tier | User tier |
|---|---|---|
| Gemini CLI | `.gemini/skills/llm-d-autoconfig/` | `~/.gemini/skills/llm-d-autoconfig/` |
| Claude Code | `.claude/skills/llm-d-autoconfig/` | `~/.claude/skills/llm-d-autoconfig/` |
| Cross-tool (`.agents/`) | `.agents/skills/llm-d-autoconfig/` | `~/.agents/skills/llm-d-autoconfig/` |

The script returns:
- **stdout:** structured JSON output with `decisions`, `rationale`, `parameters`, `unresolved_questions`, `warnings`, `errors`
- **stderr:** the rendered EPP YAML (the actual config to deploy)

If the script exits with `error: input does not match schema`, do NOT retry with the same JSON — the schema mismatch is on you. Re-read the schema above, fix the input, and re-run.

### Step 2 (Phase C): render the deployable bundle

When the user opts into deploy at Phase 5, render the bundle as a directory of individual YAMLs so Phase 6 can `kubectl apply -f <dir>` it.

#### Step 2a: ask about CRDs (MANDATORY)

Before generating the bundle, **always** ask the user whether to include the Gateway API + GIE CRDs. Default-on works for greenfield clusters; default-off works when an operator (or a previous install) already manages CRDs. The script can't decide for the user — apply of a duplicate CRD is harmless but verbose, and on some clusters the user lacks cluster-scoped RBAC to apply CRDs at all.

Build the question text **dynamically** using Phase 1's `CRDS_INSTALLED` finding so the user sees the right context:

| `CRDS_INSTALLED` from Phase 1 | Status line to include in the question |
|---|---|
| `true` (CRDs detected on cluster) | "Your cluster already has these CRDs (Phase 1 detected `inferencepools.inference.networking.x-k8s.io`). Including them is harmless — apply is idempotent — but skipping makes the bundle smaller and avoids any RBAC issues if you're not a cluster-admin." |
| `false` (CRDs absent) | "Your cluster does NOT have these CRDs yet (Phase 1 detected nothing matching `inference.networking.x-k8s.io`). You'll need them before the InferencePool / HTTPRoute resources can apply — include them or install separately first." |
| Unknown (Phase 1 skipped, no kubectl access) | "Phase 1 was skipped (no kubectl access), so I don't know whether your cluster has these CRDs. If unsure, include them — apply is idempotent." |

Then ask (choice):

```json
[{"header": "Include CRDs", "question": "Include Gateway API + GIE CRDs in the bundle?\n\n<STATUS_LINE_FROM_TABLE_ABOVE>", "type": "choice", "options": [
  {"label": "Yes (include)", "description": "Bundle is self-contained — `kubectl apply -f <dir>` works on a greenfield cluster. Adds ~26 CRD YAMLs to the bundle. Recommended unless you have a specific reason to skip."},
  {"label": "No (skip)", "description": "Bundle assumes the cluster already has the CRDs installed. Smaller bundle; use when an operator manages CRDs, or when your role lacks cluster-scoped RBAC to apply CRDs."}
]}]
```

Record the user's answer as `INCLUDE_CRDS` (boolean). If `INCLUDE_CRDS=false`, append `--no-crds` to the bundle command in Step 2b.

#### Step 2b: invoke the bundle renderer

```bash
$ python3 <skill-install-dir>/scripts/autoconfig_poc.py \
    --input <work-dir>/autoconfig-input.json \
    --bundle-dir <work-dir> \
    --chart-version v1.5.0 \
    <if INCLUDE_CRDS=false: append --no-crds>
# Creates <work-dir>/autoconfig-<TIMESTAMP>/ containing one YAML per resource
# (numerically prefixed for apply ordering) plus a README.md with apply hints.
# Path is printed on stderr — capture or use `ls <work-dir>/autoconfig-*/`.
```

The directory layout is:
- `00-*-customresourcedefinition-*.yaml` (CRDs, when `INCLUDE_CRDS=true`)
- `05-*-namespace-*.yaml`, `07-*-secret-*.yaml` (prereqs)
- `10-*-configmap-*.yaml`, `12-*-serviceaccount-*.yaml`, `14-16-*-role*-*.yaml` (chart RBAC + config)
- `20-*-service-*.yaml`, `30-*-deployment-*.yaml`, `35-*-inferencepool-*.yaml` (chart workload)
- `45-*-gateway-*.yaml`, `50-*-httproute-*.yaml`, `52-*-destinationrule-*.yaml` (gateway, when applicable)
- `60-*-inferenceobjective-*.yaml`, `70-*-variantautoscaling-*.yaml`, etc. (feature CRs)
- `README.md` (generation metadata + scaffold notes for features that don't render as K8s resources)

The renderer shells out to `helm template` against the llm-d-router chart variant (standalone or gateway, picked from `context.deploy_mode`) and appends hand-rendered Gateway / HTTPRoute / Phase B feature resources. Requires `helm` AND `kubectl` (for `kubectl kustomize` CRD fetch) on PATH AT GENERATION TIME ONLY — at apply time the user only needs `kubectl`.

`kubectl apply -f <dir>` applies in filename order (alphabetical = ordering rank). Or `kubectl apply -f <dir>/30-*-deployment-*.yaml` to apply one resource at a time.

Sanity-check the kinds before apply:

```bash
ls <work-dir>/autoconfig-*/*.yaml
# or, kinds only:
grep -h '^kind: ' <work-dir>/autoconfig-*/*.yaml | sort -u
```

Expected kinds for a typical agg+standalone deploy: `Namespace`, `Secret`, `ConfigMap`, `Deployment`, `InferencePool`, `Role`, `RoleBinding`, `Service`, `ServiceAccount`. Add `HTTPRoute` for standalone+gateway, add `VariantAutoscaling` / `HorizontalPodAutoscaler` / `InferenceObjective` / `Kustomization` for the relevant Phase B flags.

---


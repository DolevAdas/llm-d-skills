# Phase 7 — Benchmark execution (post-deploy, opt-in)

*Detailed runbook for SKILL.md Phase 7. Generates + applies the benchmark Job, surfaces results vs SLAs, cleans up. Read this only if the user opted into benchmarking in Phase 5.*

> **Which benchmark path is this?** Phase 7 runs the **deterministic, config-coupled** benchmark that the autoconfig script emits alongside the EPP config (a rendered ConfigMap + Job, applied with one `kubectl apply`, validated against the SLAs the user gave in Discovery). It is intentionally distinct from the [`run-llm-d-benchmark`](../../../../skills/run-llm-d-benchmark/SKILL.md) skill, which is the **general** harness (template selection + `run_only.sh`, multiple harnesses, results PVC) for benchmarking any already-deployed stack. If the user wants to benchmark a stack that autoconfig didn't set up, or wants harness/workload options beyond what the generated config carries, hand off to `run-llm-d-benchmark` instead of running this phase.


The autoconfig script always emits a guidellm benchmark config in `decisions.benchmark.config`. Phase 5 surfaced it; this phase actually runs it against the deployed cluster.

```json
[{"header": "Run benchmark", "question": "Want me to run the benchmark we generated? It's a sanity run + (if you provided SLAs) an SLA-validation pass at rates [1, 5, 10] req/s. Total runtime ~5 minutes. I'll substitute placeholders with your real cluster values, apply the harness via the chosen image, and compare results to your SLA targets.", "type": "yesno"}]
```

### Phase 7.1 — Generate the deployment YAML

The script's `--benchmark-deployment-out` flag produces a complete K8s deployment YAML (ConfigMap + Job, optional PVC) ready for one `kubectl apply -f`. Substitution happens at script-call time via `--bench-target` / `--bench-namespace` / `--bench-pvc` — no agent-side sed, no separate ConfigMap step, no Job spec assembly.

**Capture the target URL first** (one kubectl call, then substitute the value into the next command):

For **gateway mode**:
```bash
# Capture the gateway IP from stdout, then use it as <gateway-ip> below.
kubectl get gateway -n <ns> -o jsonpath='{.items[0].status.addresses[0].value}'
```

For **standalone mode**: pick a port-forward target. Either set up a port-forward and use `http://localhost:8080`, or use the EPP service ClusterIP (find it with `kubectl get svc -n <ns> -l app.kubernetes.io/instance=<release-name>`).

**Then render the deployment** (one shot — produces ConfigMap + Job in a single multi-document YAML). Use `<work-dir>` from Phase 4 Step 0 — don't make a new temp dir.

```bash
# --bench-pvc is optional; if omitted, the Job uses emptyDir for results.
python3 <skill-install-dir>/scripts/autoconfig_poc.py \
    --input <work-dir>/autoconfig-input.json \
    --benchmark-deployment-out <work-dir>/bench-deployment.yaml \
    --bench-target http://<gateway-ip-or-localhost:8080> \
    --bench-namespace <ns>
```

If the user has a PVC for results: add `--bench-pvc <pvc-name>`. Otherwise the Job uses an emptyDir volume (results are lost when the pod terminates — fine for sanity validation, not for keeping benchmark history).

### Phase 7.2 — Show the rendered deployment YAML and ask for approval

Before applying, **show the user the contents of `<work-dir>/bench-deployment.yaml`**. This is the substituted YAML — placeholders resolved with their actual cluster values — so the user sees exactly what will land in the cluster.

```bash
cat <work-dir>/bench-deployment.yaml
```

Then ask:

```json
[{"header": "Apply YAML", "question": "Here's the benchmark deployment YAML I'll apply. ConfigMap holds the workload config; Job runs the harness against your gateway. Apply this?", "type": "yesno"}]
```

Wait for explicit approval before proceeding to Phase 7.3. If the user wants changes (different rate, different duration, different PVC), regenerate via the script with adjusted flags rather than hand-editing the YAML — keeps the deterministic-input contract intact.

### Phase 7.3 — Apply and wait

One `kubectl apply -f`, then a wait-for-pod loop, then one `kubectl wait` for completion. The Job name is deterministic (a hash of the substituted config) so re-running with the same inputs is idempotent.

```bash
# Step 1: apply the multi-document YAML (ConfigMap + Job).
kubectl apply -f <work-dir>/bench-deployment.yaml
```

**Step 1.5: wait for the pod to be created.** The Job controller takes ~1-5s to create the pod after `kubectl apply`. Going straight to `kubectl wait` or `kubectl logs` will hit `error: at least one resource must be specified to use a selector` because the Pod doesn't exist yet.

Use `kubectl wait` against the JOB rather than the Pod (the Job exists immediately after apply, even before its Pod does):

```bash
# Wait for the Job's Pod to actually start running. This blocks until the
# Job controller has created a Pod AND it has reached a non-Pending phase.
# No shell loop, no command substitution — kubectl handles the polling itself.
kubectl wait --for=jsonpath='{.status.active}'=1 \
    --timeout=60s \
    -n <ns> -l app.kubernetes.io/component=autoconfig-benchmark job
```

If this `kubectl wait` times out, the Job didn't schedule a pod within 60s — surface the failure (likely a quota / NodeSelector / image pull issue) and stop. Don't retry; the underlying problem won't fix itself.

```bash
# Step 2: now wait for completion.
# Job name format is autoconfig-bench-<8-char-hash>.
kubectl wait --for=condition=complete --timeout=20m \
    -n <ns> -l app.kubernetes.io/component=autoconfig-benchmark job
```

Typical runtime: 1-2 minutes for warmup-only (no SLOs provided), 5-15 minutes with the SLA rate ladder.

### Phase 7.4 — Surface results and compare to SLAs

The Job's container is wrapped with `sh -c` so the harness's exit, then a `find <results-dir> -name '*.json' -exec cat ... \;` block — meaning the JSON result files are dumped to stdout right after the harness completes, after a `---BENCHMARK RESULTS (json files in <dir>)---` delimiter line. You can read everything from `kubectl logs`; you do NOT need to `kubectl exec` into the pod (which fails for Completed pods anyway).

Pipe `kubectl logs` through `scripts/parse_bench_results.py` — it finds the delimiter, extracts JSON, normalizes inference-perf vs guidellm shapes, and renders a markdown table. **Use a tail of 3000+ to be safe** — the delimiter line is printed AFTER the harness summary, and short tails can truncate it out:

```bash
kubectl logs -n "$NAMESPACE" -l job-name=autoconfig-bench-... --tail=3000 \
    | python3 <skill-install-dir>/scripts/parse_bench_results.py
```

If the user provided SLA targets (TTFT, TPOT, end-to-end request latency), pass them as flags. The parser appends an SLA-validation block to the table and exits non-zero on any breach so wrapping scripts can detect it:

```bash
kubectl logs ... --tail=3000 \
    | python3 <skill-install-dir>/scripts/parse_bench_results.py \
        --ttft-sla "$TTFT_SLA_MS" --tpot-sla "$TPOT_SLA_MS" --e2e-sla "$E2E_SLA_MS"
```

Surface the parser's stdout to the user verbatim — the markdown table is the report. When the parser flags an SLA breach (✗ EXCEEDS markers in the output, or exit code 1), follow up with a brief diagnosis: which rate stage saturated, which knobs to turn (replica count, TP, accept-the-breach if the breach rate is above real-world peak).

**Parser exit codes:**
- 0 — parse succeeded, no SLAs supplied OR all SLAs met
- 1 — parse succeeded, at least one SLA breached (surface the table + diagnosis)
- 2 — parse failed (no delimiter found OR no recognizable JSON). Most common cause: `--tail` was too small and truncated the delimiter. Re-run `kubectl logs` with `--tail=5000` or larger before assuming the Job is broken.

If the benchmark Job itself failed (config error, image pull error, pod crash), the parser will exit 2 (no JSON to extract). Capture the raw `kubectl logs` output, surface the failure to the user, and do NOT auto-retry — consult `references/pitfalls.md` first.

### Phase 7.5 — Cleanup

After the user has reviewed the results, the simplest cleanup is to delete by label (matches both the Job and ConfigMap that the script generated):

```bash
kubectl delete job,configmap -n <ns> -l app.kubernetes.io/component=autoconfig-benchmark
```

If `--bench-pvc` was used, the PVC is NOT deleted — keep it unless the user explicitly asks. The benchmark results are the payload; deleting the PVC discards them.

Re-running the same benchmark (same inputs) reuses the same Job name (deterministic hash). If you want to re-run, delete the existing Job first or change a parameter (e.g. add a comment to your input JSON) so the hash changes.

---

### Source-of-truth notes

- **EPP helm charts (`llm-d-router-standalone` / `-gateway`), deploy components, CRDs, gateway-provider overlays:** `github.com/llm-d/llm-d-router` (current source-of-truth; PRs land here)
- **Workload-specific guides + per-hardware modelserver values:** `github.com/llm-d/llm-d/guides/<guide>/`
- **Deprecated GAIE Helm charts** (`standalone`, `inferencepool`, `body-based-routing`) **still live in** `github.com/kubernetes-sigs/gateway-api-inference-extension/charts/`, but the skill no longer uses them — they render legacy `--*-metric` CLI flags the current EPP rejects (crashes non-vLLM engines).

### Reference docs (fetch these on demand for non-default scenarios)

The SKILL inlines the canonical happy-path commands for every phase. When the user asks for something the SKILL doesn't cover — a non-standard install, a different gateway provider, a plugin tuning question, a hardware variant — fetch the relevant doc below for the authoritative reference. Pointing at docs is the right pattern; deciding to deviate from the SKILL's defaults requires reading the source.

**Well-lit-path guides** (the deploy recipes the SKILL targets):
- Optimized baseline (agg, default): https://github.com/llm-d/llm-d/blob/main/guides/optimized-baseline/README.md
- PD disaggregation: https://github.com/llm-d/llm-d/blob/main/guides/pd-disaggregation/README.md
- Predicted-latency-based scheduling (latency-predictor): https://github.com/llm-d/llm-d/blob/main/guides/predicted-latency-routing/README.md
- Precise prefix-cache aware: https://github.com/llm-d/llm-d/blob/main/guides/precise-prefix-cache-routing/README.md
- Tiered prefix cache: https://github.com/llm-d/llm-d/blob/main/guides/tiered-prefix-cache/README.md
- Wide expert parallelism (LWS): https://github.com/llm-d/llm-d/blob/main/guides/wide-ep-lws/README.md
- Workload autoscaling (WVA / HPA-EPP): https://github.com/llm-d/llm-d/blob/main/guides/workload-autoscaling/README.md
- Flow control: https://github.com/llm-d/llm-d/blob/main/guides/flow-control/README.md
- Batch gateway: https://github.com/llm-d/llm-d/blob/main/guides/batch-gateway/README.md
- Asynchronous processing: https://github.com/llm-d/llm-d/blob/main/guides/asynchronous-processing/README.md
- All guides index: https://github.com/llm-d/llm-d/blob/main/guides/README.md

**Gateway provider install guides** (one per supported provider):
- Provider list + decision matrix: https://github.com/llm-d/llm-d/blob/main/guides/prereq/gateways/README.md
- Istio (in-cluster control plane): https://github.com/llm-d/llm-d/blob/main/guides/prereq/gateways/istio.md
- GKE (managed L7 internal/external LB, no install): https://github.com/llm-d/llm-d/blob/main/guides/prereq/gateways/gke.md
- AgentGateway (Rust-based AI gateway): https://github.com/llm-d/llm-d/blob/main/guides/prereq/gateways/agentgateway.md

**Cluster-provider docs** (infrastructure-side gotchas):
- GKE-specific (RDMA, gIB, multi-net, NCCL): https://github.com/llm-d/llm-d/blob/main/docs/infra-providers/gke/README.md
- AKS / DigitalOcean / Minikube / OpenShift / OpenShift-AWS: https://github.com/llm-d/llm-d/tree/main/docs/infra-providers

**Recipes (referenced by the helm install commands)**:
- Router base values: https://github.com/llm-d/llm-d/blob/main/guides/recipes/router/base.values.yaml
- PD router values: https://github.com/llm-d/llm-d/blob/main/guides/pd-disaggregation/router/pd-disaggregation.values.yaml
- Optimized-baseline router values: https://github.com/llm-d/llm-d/blob/main/guides/optimized-baseline/router/optimized-baseline.values.yaml
- Modelserver overlays index (per-accelerator + per-CSP): https://github.com/llm-d/llm-d/tree/main/guides/optimized-baseline/modelserver
- PD modelserver overlays: https://github.com/llm-d/llm-d/tree/main/guides/pd-disaggregation/modelserver

**EPP plugin docs** (when the user asks "what does scorer X do" or "how do I tune Y"):
- Plugin framework overview: https://github.com/llm-d/llm-d-router/blob/main/pkg/epp/framework/plugins/README.md
- Latency scorer: https://github.com/llm-d/llm-d-router/blob/main/pkg/epp/framework/plugins/scheduling/scorer/latency/README.md
- Predicted-latency producer (PD-aware via `endpointRoleLabel`): https://github.com/llm-d/llm-d-router/blob/main/pkg/epp/framework/plugins/requestcontrol/dataproducer/predictedlatency/README.md
- Latency SLO admitter: https://github.com/llm-d/llm-d-router/blob/main/pkg/epp/framework/plugins/requestcontrol/admitter/latencyslo/README.md
- SLO headroom tier filter: https://github.com/llm-d/llm-d-router/blob/main/pkg/epp/framework/plugins/scheduling/filter/sloheadroomtier/README.md
- Disagg profile handler (PD orchestration): https://github.com/llm-d/llm-d-router/blob/main/pkg/epp/framework/plugins/scheduling/profilehandler/disagg/README.md
- All plugin READMEs (browse): https://github.com/llm-d/llm-d-router/tree/main/pkg/epp/framework/plugins

**Helm charts**:
- llm-d-router charts (standalone + gateway): https://github.com/llm-d/llm-d-router/tree/main/config/charts
- llm-d-router common chart values (routerlib): https://github.com/llm-d/llm-d-router/blob/main/config/charts/routerlib/values.yaml

**EndpointPickerConfig schema** (when user asks "what fields does plugin X accept"):
- Type definitions: https://github.com/llm-d/llm-d-router/blob/main/apix/config/v1alpha1/endpointpickerconfig_types.go

**Benchmark harness docs**:
- inference-perf (native config schema): https://github.com/kubernetes-sigs/inference-perf/blob/main/config.yml
- inference-perf helm chart (image + Job spec reference): https://github.com/kubernetes-sigs/inference-perf/tree/main/deploy/inference-perf
- llm-d-benchmark wrapper (used by guidellm): https://github.com/llm-d/llm-d-benchmark
- Canonical inference-perf workload templates: https://github.com/llm-d/llm-d/tree/main/guides/optimized-baseline/benchmark-templates

---


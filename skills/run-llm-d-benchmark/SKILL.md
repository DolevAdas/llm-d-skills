---
name: run-llm-d-benchmark
description: Benchmark an already-deployed llm-d stack using the llmdbenchmark CLI. Use this skill when the user wants to run a benchmark workload against a deployed llm-d stack — whether following a named guide (e.g. optimized-baseline, pd-disaggregation) or using a custom workload profile against any inference endpoint. Activate for requests to measure performance (TTFT, throughput, ITL), validate a deployment under load, or run inference-perf/guidellm/vllm-benchmark — even if they don't use the word "benchmark" explicitly.
---

# Run llm-d Benchmark

## Purpose

Run a benchmark workload against an already-deployed llm-d stack using the `llmdbenchmark` CLI. Useful for evaluating the performance of the llm-d stack. Supports two modes:

- **Guide mode**: the stack was deployed from a named llm-d guide (e.g. `optimized-baseline`). Use `--spec guides/<name>` and pick from guide-specific or generic workload profiles.
- **Custom workload mode**: the user provides their own workload profile (or a path to an existing one) and a direct endpoint URL. No guide name required.

Both modes follow [`helpers/benchmark.md`](https://github.com/llm-d/llm-d/blob/main/helpers/benchmark.md). When the benchmark completes, results are available in the local workspace directory.

---

## Workflow

### Step 1: Verify or Install the `llmdbenchmark` CLI

Check whether the CLI is available:

```bash
command -v llmdbenchmark 2>/dev/null && llmdbenchmark --version
```

If not found, check for the repo locally and install if missing:

```bash
# Is the repo already cloned?
ls ./llm-d-benchmark 2>/dev/null || \
  curl -sSL https://raw.githubusercontent.com/llm-d/llm-d-benchmark/main/install.sh | bash
```
For troubleshooting install issues - Check Makefile for install instructions and dependencies.

Activate the virtualenv and enter the repo — both are required for every new shell session:

```bash
cd llm-d-benchmark
source .venv/bin/activate
llmdbenchmark --version
```

> **All subsequent `llmdbenchmark` commands must be run from inside `llm-d-benchmark/` with the venv active.**

---

### Step 2: Locate the Namespace and (Optionally) the Guide Name

Locate the llm-d stack according to the following logic:

1. If a NAMESPACE environment variable is specified, the llm-d stack is assumed to be deployed there
2. If an oc project exists, the stack is assumed to be deployed in the current oc project.
3. If none of the above holds, ask the user for the NAMESPACE where it is deployed.
4. Make sure the NAMESPACE environment variable is set.

Verify that the stack is indeed deployed in the detected or provided namespace using kubectl commands. If you cannot locate the stack, ask the user to deploy one and refer to the llm-d-kubernetes deployment skill.

Determine if the user wants to run an existing/modified guide workload or a custom workload
- **Guide mode**: If guide name provided (e.g. `optimized-baseline`, `pd-disaggregation`). Set `GUIDE_NAME`. This maps to `--spec guides/${GUIDE_NAME}` and narrows the profile list in Step 5.
- **Custom workload mode**: `GUIDE_NAME` is unset. The user will supply the endpoint URL directly in Step 3 and the workload profile path in Step 5.
---

### Step 3: Resolve the Endpoint and Gateway Class

Determine and set the `ENDPOINT_URL` environment variable — this is the *full* URL of the gateway/epp/clusterIP service that exposes the inference endpoint. If ${GUIDE_NAME} defined, it might also be part of the service name. You can find it by running:
```bash
kubectl get svc -n $NAMESPACE
```
Look for the gateway or ingress service (typically named something like `llm-d-inference-gateway`). Ask the user to confirm if ambiguous.
If no pods are found or none are Running, tell the user the stack does not appear to be deployed and suggest using the `deploy-llm-d` skill first.

Determine and set the `GATEWAY_CLASS` environment variable - this is the topology type: `epponly` (standalone EPP), `istio`, `gke`, `agentgateway`, or `data-science-gateway-class`

If auto-detection finds nothing, List available services in the namespace to help the user identify the right endpoint.
---

### Step 4: Confirm the Model Name

The `--model` flag must match exactly what the server reports.
Check which models are available in the stack deployment by querying ${ENDPOINT_URL}/v1/models or looking at the decoders helm deployment.
Set `MODEL` to the exact string returned (e.g. `Qwen/Qwen3-32B`, `openai/gpt-oss-120b`). If multiple models are returned, ask the user to select one.

> **Common error**: `"did not return expected model 'X'"` means there is a mismatch — always confirm the model name from the live endpoint.

---
### Step 5: Select the Harness

Ask the user for a harness to use, the available harnesses are:
- `inference-perf` 
- `guidellm`
- `inferencemax`
- `vllm-benchmark`

---

### Step 6: Select a Workload Profile

#### Option A: User provides a custom profile path

If the user provided a custom workload profile path, use it (copy it into `workload/profiles/${HARNESS:-inference-perf}/` if needed).
Set `WORKLOAD_PROFILE=custom_profile.yaml`. Always pass the `.yaml` form to `--workload`.

#### Option B: Pick from shipped profiles

**Guide mode** — list guide-specific and generic profiles for the selected harness:

```bash
ls workload/profiles/${HARNESS:-inference-perf}/ | grep -E "guide_${GUIDE_NAME}|shared_prefix|sanity"
```

**Custom workload mode** — list all generic profiles (no guide filter):

```bash
ls workload/profiles/${HARNESS:-inference-perf}/ | grep -v "^guide_"
```

Display the list and recommend:

| Goal | Profile |
|------|---------|
| Quick smoke test / path validation | `shared_prefix_synthetic.yaml` |
| Full guide benchmark (reproduces published results) | `guide_${GUIDE_NAME}_1.yaml` *(guide mode only, if present)* |
| Sanity check | `sanity_random.yaml` |
| Multi-turn chat | `shared_prefix_multi_turn_chat.yaml` |
| Code completion | `code_completion_synthetic.yaml` |

If the user wants to author a new profile from scratch, offer to help: copy a relevant `.yaml.in` template from `workload/profiles/${HARNESS}/`, edit it to their specifications, and save it as a new `.yaml` file. Substitution tokens like `REPLACE_ENV_LLMDBENCH_DEPLOY_CURRENT_MODEL` are resolved automatically at runtime — no manual `envsubst` needed.

Set `WORKLOAD_PROFILE` to the selected or generated profile path.

---

### Step 7: Set the Workspace Directory

The CLI auto-generates a timestamped within the workspace.

```bash
# Auto-generated (default) — CLI prints the path during the run
# No --workspace flag needed

# Or user-specified:
export WORKSPACE_DIR="<user-provided path>"
```

Confirm the workspace path before running. If the user specifies one, make sure the directory exists:

```bash
mkdir -p ${WORKSPACE_DIR}
```

---

### Step 8: Override Profile Parameters (optional)

If asked to modify workload configuration, either create a copy of the yaml file, modify it and set it as the workload (see Step 6.A) or set
`OVERRIDES` to provide the per run overrides "key=value,..." of the individual workload profile fields.
Keys should use **dotted paths** matching the YAML structure of the profile (e.g. data.shared_prefix.question_len).

---

### Step 9: Configure Timeouts (if needed)

The defaults are usually sufficient. Mention them to the user and ask only if they are on a slow cluster or seeing timeout errors:

| Flag | Default | Environment variable |
|------|---------|---------------------|
| `--wait-timeout` | 3600s | `LLMDBENCH_WAIT_TIMEOUT` |
| `--pvc-bind-timeout` | 240s | `LLMDBENCH_PVC_BIND_TIMEOUT` |
| `--data-access-timeout` | 120s | `LLMDBENCH_DATA_ACCESS_TIMEOUT` |

For slow clusters or first-time image pulls, extend to:
```bash
--wait-timeout 7200 --pvc-bind-timeout 1200 --data-access-timeout 600
```

---

### Step 10: Show and Run the Benchmark Command

Display the full command to the user for review, then run it.

**Guide mode** (`GUIDE_NAME` is set — includes `--spec`):
```bash
llmdbenchmark \
    --spec           guides/${GUIDE_NAME} \
    ${WORKSPACE_DIR:+--workspace "${WORKSPACE_DIR}"} \
    run \
    --endpoint-url   "${ENDPOINT_URL}" \
    --gateway-class  "${GATEWAY_CLASS}" \
    --model          "${MODEL}" \
    --namespace      "${NAMESPACE}" \
    --harness        ${HARNESS:-inference-perf} \
    --workload       ${WORKLOAD_PROFILE} \
    ${OVERRIDES:+--overrides "${OVERRIDES}"} \
    --wait-timeout   ${WAIT_TIMEOUT:-3600} \
    --analyze
```

**Custom workload mode** (`GUIDE_NAME` is unset — no `--spec`):
```bash
llmdbenchmark \
    ${WORKSPACE_DIR:+--workspace "${WORKSPACE_DIR}"} \
    run \
    --endpoint-url   "${ENDPOINT_URL}" \
    --gateway-class  "${GATEWAY_CLASS}" \
    --model          "${MODEL}" \
    --namespace      "${NAMESPACE}" \
    --harness        ${HARNESS:-inference-perf} \
    --workload       ${WORKLOAD_PROFILE} \
    ${OVERRIDES:+--overrides "${OVERRIDES}"} \
    --wait-timeout   ${WAIT_TIMEOUT:-3600} \
    --analyze
```

> **Note:** `--analyze` triggers post-run distribution plot generation under `analysis/distributions/`. Add it unless the user explicitly opts out.

Monitor output as it runs. The CLI will print the workspace path early in the logs — note it for Step 13.

---

### Step 11: Monitor and Wait for Completion

The CLI manages the harness pod lifecycle. Monitor the harness pod for processing completion:

```bash
kubectl get pod -n $NAMESPACE -l app=llmdbench-harness-launcher -w
```

Or follow the harness logs directly:

```bash
kubectl logs -n $NAMESPACE -l app=llmdbench-harness-launcher -f --tail=100
```

Note that the benchmark harness pod may still be running also after the benchmarking run is completed.
If benchmarking needs to be rerun due to an error in a previous run, vllm pods need to be restarted first to evict the kv cache.
In case the the harness pod is OOMKilled, try to increase the harness memory.
If pod reaches `Failed` status, diagnose using the troubleshooting guide below.

---

### Step 12: Collect vLLM Pod Logs

Save logs from all vLLM pods before they are recycled - to aid in debugging and performance analysis. First locate them:

```bash
# Try the llm-d role label (most reliable for guide-based deployments)
kubectl get pods -n $NAMESPACE -l llm-d.ai/role=decode -o name 2>/dev/null

# Fallback: standard vllm component label
kubectl get pods -n $NAMESPACE -l app.kubernetes.io/component=vllm -o name 2>/dev/null

# Last resort: grep by name
kubectl get pods -n $NAMESPACE | grep -i vllm | awk '{print "pod/"$1}'
```

Locate the directory the llmdbenchmark uses in the workspace. Collect and save logs:

```bash
mkdir -p <benchmark-dir>/logs
for pod in $(kubectl get pods -n $NAMESPACE -l llm-d.ai/role=decode -o name 2>/dev/null); do
  pname=$(echo $pod | sed 's|pod/||')
  kubectl logs -n $NAMESPACE $pname --timestamps \
    > "<benchmark-dir>/logs/${pname}.log" 2>&1
  echo "Collected: ${pname}"
done
```

If a pod has multiple containers, add `-c vllm` (or the appropriate container name) to the `kubectl logs` command.

---

### Step 13: Verify Workspace Results
Locate the directory the llmdbenchmark uses in the workspace and use it in this step.
Check that result files landed in the local workspace:

```bash
ls <benchmark-dir>/results/
find <benchmark-dir>/results -name "*lifecycle_metrics.json" | sort
```

If the directory is empty or missing (e.g. the CLI exited with `http2: client connection lost` during the copy phase), the harness pod might completed successfully and results are still on the cluster's `workload-pvc`. Check if results are on the PVC and try to copy them:

```bash
# Locate the results on the PVC
kubectl exec -n $NAMESPACE <workload-pvc> -- \
  find /workspace -name "summary_lifecycle_metrics.json" | sort

# Copy from the PVC once you identify the run directory
kubectl cp $NAMESPACE/<workload-pvc>:/workspace/<run-dir>/results \
  <llmdbenchmark-dir>/results/
```

The `<run-dir>` is printed early in the CLI output: `Created llmdbenchmark instance in workspace: .../<run-dir>`. Check the benchmark log if available, or use the `find` command above to locate it.

---

### Step 14: Display Results Summary

The workspace layout after a completed run:

```
<workspace>/
└── run/
    ├── plan/
    ├── environment/
    └── results/
        └── <experiment-id>/
            ├── stage_0_lifecycle_metrics.json
            ├── stage_N_lifecycle_metrics.json
            ├── summary_lifecycle_metrics.json
            ├── config.yaml
            ├── stdout.log / stderr.log
            └── analysis/
                └── distributions/
                    ├── dist_ttft.png
                    ├── dist_itl.png
                    └── scatter_ttft_vs_input.png
```

Read `summary_lifecycle_metrics.json` and display a brief results table covering:

| Metric | Value |
|--------|-------|
| TTFT p50 / p90 (s) | |
| E2E latency p50 / p90 (s) | |
| Throughput (req/s) | |
| Output tokens/s | |
| Error rate (%) | |

---

### Step 15: Announce Completion

Tell the user where everything is saved and what to look at next:

```
Benchmark complete. Results are in: <WORKSPACE_DIR>/

Key files:
  results/<experiment-id>/summary_lifecycle_metrics.json   — aggregated metrics
  results/<experiment-id>/stage_*_lifecycle_metrics.json   — per-stage data
  results/<experiment-id>/analysis/distributions/          — distribution plots (if --analyze was used)
  logs/                                                     — vLLM pod logs

To regenerate analysis plots from existing data, re-run llmdbenchmark with --analyze
pointing at the existing workspace.
```

---

## Troubleshooting

### Model name mismatch
**Error**: `"did not return expected model 'X'. Available models: ['Y']"`

Always verify the model name via `/v1/models` (Step 4) before running. The `--model` flag must match exactly.

### PVC timeout
**Error**: `"Timed out after 240s waiting for workload PVC"`

Increase with `--pvc-bind-timeout 1200`. Check the StorageClass:
```bash
kubectl get sc
kubectl describe pvc -n $NAMESPACE
```

### Endpoint not found
**Error**: `"Could not detect endpoint for <stack>"`

In run-only mode, always explicitly pass `--endpoint-url` and `--gateway-class`.

### Missing HuggingFace token
**Error**: `"HF_TOKEN is not set and Secret 'llm-d-hf-token' does not exist"`

Create the secret:
```bash
kubectl create secret generic llm-d-hf-token \
    --from-literal="HF_TOKEN=${HF_TOKEN}" \
    --namespace "${NAMESPACE}" \
    --dry-run=client -o yaml | kubectl apply -f -
```

### Pod stuck at ContainerCreating / ImagePullBackOff
Extend the wait timeout: `--wait-timeout 1800`. Investigate with:
```bash
kubectl describe pod -l app=llmdbench-harness-launcher -n $NAMESPACE
```

### Invalid `--output` path
**Error**: `"Unknown output destination: ./results"`

`--output` only accepts `local`, `gs://...`, or `s3://...`. Use `--workspace` for local path control.

### Empty `analysis/distributions/`
The `--analyze` flag was not passed. Add it and re-run.

---

## Execution Rules

1. **Run every step in order** — each step depends on the previous succeeding.
2. **After each command**, inspect output: success → proceed; failure → diagnose, fix, re-run.
3. **Never skip steps** — especially Step 4 (model verification), Step 12 (log collection before pods are recycled), and Step 13 (verify results landed locally).

**RULE**: There is no need to ask permission from the user to read the content of environment variables.

## What Not To Do

1. **Do NOT change cluster-level resources** — scope all `kubectl` commands to `-n ${NAMESPACE}`. Never modify ClusterRoles, ClusterRoleBindings, StorageClasses, or Nodes.
2. **Do NOT modify pre-existing repository files** — never edit committed `values.yaml`, `README.md`, or any other file you did not create in this session.
3. **Always notify the user before creating any Kubernetes resources.**
4. **Always get confirmation before deleting any resources.**

## Prerequisites

**Guide**: [client-setup](https://github.com/llm-d/llm-d/tree/main/helpers/client-setup/README.md)

Required client tools:
- `kubectl`
- `helm`
- `git`
- `curl`
- `jq`
- `python3` (for `llmdbenchmark` venv)

## Security Considerations

- Run benchmarking in isolated namespaces
- Use RBAC for access control
- Secure HuggingFace tokens as Kubernetes secrets
- Enable network policies for pod-to-pod communication
- Use TLS for gateway ingress
- Audit all deployments and changes

## Additional Resources

- [helpers/benchmark.md](https://github.com/llm-d/llm-d/blob/main/helpers/benchmark.md) — full benchmarking reference, all flags, troubleshooting
- [llm-d-benchmark repository](https://github.com/llm-d/llm-d-benchmark) — workload profiles, custom harness images, analysis notebooks

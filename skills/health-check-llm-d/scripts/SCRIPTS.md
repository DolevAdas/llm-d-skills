# Scripts

## gpu-health-probe.py

Sends an identical set of randomized requests to one or more vLLM pod endpoints, measures **time-to-first-token (TTFT)** and **time-per-output-token (TPOT)** per pod, and flags outliers in two independent ways:

- **vs peers** — slower than other pods in the same `--groups` label (one run).
- **vs history** — slower than this same GPU's own past runs recorded in a local `--history` JSON file. This is what makes a **single-GPU** deployment checkable: a lone pod with no peers is still compared against its own recorded baseline.

**Requirements**: Python 3.6+, stdlib only (no pip installs needed).

### How it makes the comparison fair

- **Same prompts for every pod** — the prompt list is drawn once (fixed seed) and reused, so prompt-length variance can't be mistaken for a GPU difference.
- **Warmup discarded** — one request per pod is sent and thrown away to absorb cold-start cost (CUDA graph capture / lazy init).
- **Group-aware** — prefill and decode pods have different latency baselines, so each pod is only compared against others in its `--groups` label.
- **Stable identity for history** — history is keyed by `--gpu-ids` (node name, ideally plus GPU UUID) + model + max_tokens, never by the ephemeral pod name.
- **Baselines from healthy runs only** — a run where a GPU was flagged is stored with its SUSPICIOUS status and excluded from future baselines, so a bad run can't poison the comparison.
- **Two signals + absolute floor** — TTFT and TPOT; an outlier must exceed both the multiplicative threshold *and* a small wall-clock gap, preventing false positives when latency is tiny.

### Arguments

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--endpoints` | yes | — | Space-separated local endpoint URLs (e.g. `http://localhost:18001 ...`) |
| `--pod-names` | yes | — | Pod names in the same order as `--endpoints` |
| `--gpu-ids` | for history | pod names | Stable GPU identity per pod (same order), e.g. `node-a:GPU-uuid`. Required for `--history` to be meaningful |
| `--groups` | no | one group | Group label per pod, e.g. `decode decode prefill`. Pods are compared only within their group |
| `--model` | yes | — | Model ID as served by vLLM (must match exactly) |
| `--requests` | no | 8 | Timed requests per pod (≥ 1); one extra warmup request is discarded |
| `--max-tokens` | no | 50 | Max tokens generated per request |
| `--threshold` | no | 2.0 | Peer outlier: flag if mean TTFT or TPOT `> threshold × group median` |
| `--drift-threshold` | no | 1.5 | History drift: flag if TTFT or TPOT `> drift-threshold × this GPU's baseline` |
| `--api` | no | `chat` | `chat` (→ `/v1/chat/completions`) or `completions` (→ `/v1/completions`, for base models) |
| `--history` | no | off | Path to a per-cluster JSON history file. Enables drift detection and records this run |
| `--cluster` | no | `unknown` | Cluster identifier stored inside the history file |
| `--timestamp` | no | now | ISO timestamp for this run (mainly for testing/reproducibility) |

### Drift detection details

- A GPU needs at least **2 prior HEALTHY runs** (same gpu-id + model + max_tokens) before drift is evaluated; earlier runs only build the baseline.
- Baseline = median of those runs' median TTFT/TPOT (median-of-medians, robust to a single noisy run).
- The history file is created if absent. If it exists but is unparseable, the script **warns and skips** both drift comparison and recording (never overwrites, to avoid data loss).

### History file shape

```json
{
  "schema_version": 1,
  "cluster": "my-cluster",
  "runs": [
    {
      "timestamp": "2026-07-13T10:00:00",
      "model": "meta-llama/Llama-3.1-70B-Instruct",
      "max_tokens": 50,
      "api": "chat",
      "pods": [
        {"pod_name": "llm-d-decode-7d9f-xq2mn", "gpu_id": "worker-3:GPU-abc123",
         "group": "decode", "n_ok": 8,
         "ttft_median": 0.031, "tpot_median": 0.011,
         "ttft_mean": 0.032, "tpot_mean": 0.011, "status": "HEALTHY"}
      ]
    }
  ]
}
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | All pods HEALTHY |
| 1 | One or more pods SUSPICIOUS or UNHEALTHY (vs peers and/or vs history) |
| 2 | Fatal: no successful responses, or bad arguments |

### Example

```bash
python3 scripts/gpu-health-probe.py \
  --endpoints http://localhost:18001 http://localhost:18002 \
  --pod-names llm-d-decode-a llm-d-decode-b \
  --gpu-ids worker-3:GPU-abc123 worker-4:GPU-def456 \
  --groups decode decode \
  --model meta-llama/Llama-3.1-70B-Instruct \
  --requests 8 --max-tokens 50 --threshold 2.0 --drift-threshold 1.5 \
  --history ~/.llm-d-health-check/my-cluster.json --cluster my-cluster
```

### Sample output — a single GPU that regressed vs its own history

```
GPU health check
  model     : Llama-3.1-70B
  api       : /v1/chat/completions
  requests  : 8 per pod (4 concurrent), 1 warmup discarded
  max_tokens: 50
  peers     : flag > 2.0x group median
  history   : ~/.llm-d-health-check/my-cluster.json (2 prior run(s)); flag > 1.5x own baseline

  Probing llm-d-decode-7d9f-xq2mn                  ... ok  (8/8 ok, TTFT=0.124s, TPOT=0.048s)

====================================================================================
  Pod                                     TTFT      TPOT  Status
------------------------------------------------------------------------------------
  group 'decode'  (median TTFT=0.124s, TPOT=0.048s)  [<3 pods: peer detection weak]
  llm-d-decode-7d9f-xq2mn               0.124s    0.048s  [ SUSPICIOUS (TTFT 4.0x vs history; TPOT 4.4x vs history) ]
====================================================================================

History baselines (median of prior HEALTHY runs for this GPU):
  llm-d-decode-7d9f-xq2mn [worker-3:GPU-abc123]: TTFT now 0.124s / base 0.031s ; TPOT now 0.048s / base 0.011s

One or more pods flagged. Suggested next steps:
  1. Check GPU/CUDA errors in flagged pod logs:
     kubectl logs -n $NAMESPACE <pod> | grep -iE "cuda|gpu|error|OOM|exception"
  2. Check node GPU allocation:
     kubectl describe node <node> | grep -A10 'Allocated resources'
```

The pod name here is ephemeral, but the GPU identity `worker-3:GPU-abc123` is stable — that's how the regression is caught across runs even though the pod was recreated.

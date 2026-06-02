# WVA Reference

## Scaler Backend Decision

| User choice | Makefile `SCALER_BACKEND` value | When to use | Scale-to-zero? |
|-------------|--------------------------------|-------------|----------------|
| **HPA** | `prometheus-adapter` | Standard setup, works with kube-prometheus-stack or OpenShift monitoring | No (min 1 replica) |
| **KEDA** | `keda` | KEDA already installed, or scale-to-zero required | Yes (min 0 replicas) |

> When the user selects **HPA**, pass `SCALER_BACKEND=prometheus-adapter` to the Makefile. When the user selects **KEDA**, pass `SCALER_BACKEND=keda`.

## Key Constraints (VA + HPA compatibility)

These must ALL be true for WVA to work:

1. **VA must have accelerator label**: `inference.optimization/acceleratorName: <vendor>` — valid values: `nvidia`, `amd`, `cpu`. Auto-detect from cluster; do not assume `nvidia`.
2. **HPA metric must be `wva_desired_replicas`** — the only metric Prometheus Adapter exposes
3. **HPA selector labels must match**: `variant_name` = VA resource name, `exported_namespace` = namespace
4. **VA and HPA must target the same deployment**
5. **Target deployment must have >= 1 replica** (HPA cannot scale from 0 without KEDA)
6. **`variantCost` must be a string** (e.g., `"10.0"` not `10.0`)
7. **API version must be `llmd.ai/v1alpha1`** (not `inference.llmd.ai/v1alpha1`)

## Environment Variables Quick Reference

Everything must be **`export`ed** before calling `make`, or passed inline on the `make` command.

### Env vars for `deploy/install.sh` (export before `make`)

| Variable | Description | Default |
|----------|-------------|---------|
| `IMG` | WVA container image (also a Make arg) | `ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest` |
| `WVA_NS` | WVA controller namespace | Set to llm-d namespace (Step 1) |
| `LLMD_NS` | Namespace where llm-d runs | Set equal to `WVA_NS` |
| `NAMESPACE_SCOPED` | Limit WVA to single namespace | `false` — set `true` for production |
| `DEPLOY_LLM_D_INFRA` | Deploy llm-d infrastructure | `true` — set `false` to skip when llm-d is already deployed |
| `DEPLOY_WVA` | Deploy WVA controller | `true` |
| `DEPLOY_LWS` | Deploy LeaderWorkerSet | `true` — set `false` if already installed |
| `DEPLOY_PROMETHEUS` | Deploy kube-prometheus-stack | `true` — set `false` if already installed |
| `DEPLOY_PROMETHEUS_ADAPTER` | Deploy Prometheus Adapter | `true` — set `false` when using KEDA |
| `SCALER_BACKEND` | `prometheus-adapter` (HPA) or `keda` | `prometheus-adapter` |
| `MONITORING_NAMESPACE` | Prometheus namespace — auto-detected in Step 4a.5 | `workload-variant-autoscaler-monitoring` (k8s) / `openshift-user-workload-monitoring` (OCP) |
| `SKIP_TLS_VERIFY` | Skip TLS for Prometheus | `false` |

### Threshold tuning (ConfigMap — live, no restart required)

Saturation thresholds live in the `wva-saturation-scaling-config` ConfigMap. Edit them directly:

```bash
kubectl edit configmap workload-variant-autoscaler-saturation-scaling-config \
  -n $WVA_NS
```

| ConfigMap key | Description | Default |
|---------------|-------------|---------|
| `kvCacheThreshold` | KV cache saturation threshold | `0.80` |
| `queueLengthThreshold` | Queue depth saturation threshold | `5` |
| `kvSpareTrigger` | Proactive spare KV trigger | `0.10` |
| `queueSpareTrigger` | Proactive spare queue trigger | `3` |

HPA behavior (stabilization windows, min/max replicas) is set per-HPA resource — patch or re-apply the HPA manifest.

## Asymmetric Stabilization Windows (post-deploy)

Patch the HPA directly:
```bash
kubectl patch hpa <hpa-name> -n $WVA_NS --type=merge -p '{
  "spec": {
    "behavior": {
      "scaleUp":   {"stabilizationWindowSeconds": 60},
      "scaleDown": {"stabilizationWindowSeconds": 300}
    }
  }
}'
```

## EPP Threshold Alignment

WVA and EPP (Inference Scheduler) must use identical thresholds:

| WVA parameter | EPP parameter |
|---|---|
| `kvCacheThreshold` | `kvCacheUtilThreshold` |
| `queueLengthThreshold` | `queueDepthThreshold` |

After changing thresholds: `kubectl rollout restart deployment/<epp-deployment> -n $WVA_NS`

## Undeploy

```bash
cd $WVA_REPO_PATH
WVA_NS=$WVA_NS ./deploy/install.sh --undeploy
```

Also delete any VAs and HPAs created in step 4e:
```bash
kubectl delete variantautoscaling,hpa -n $WVA_NS --all
```

## Known Issues

See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) for common problems including:
- METRICSREADY: False (accelerator label issues)
- HPA showing `<unknown>` (wrong metric or selector)
- Controller not detecting VA resources (namespace-scoping)
- CRD field manager conflicts
- Scale-to-zero not working

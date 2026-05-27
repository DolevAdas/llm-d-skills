# Deploy WVA Controller (Step 4b Details)

## Pre-check: Go in PATH

The Makefile downloads `controller-gen` and `kustomize` using Go. Verify Go is accessible:

```bash
which go || echo "not found"
```

If `go` is not found in PATH, search common locations:
```bash
GO_BIN=$(find /opt/homebrew/bin /usr/local/go/bin /usr/bin -name go -type f 2>/dev/null | head -1)
if [ -n "$GO_BIN" ]; then
  export PATH="$(dirname $GO_BIN):$PATH"
  echo "Added Go to PATH: $GO_BIN"
fi
```

If Go still cannot be found, **stop and ask the user**:
> "Go is required by the Makefile to download build tools. Please run `which go` in your terminal and share the path so I can add it to the environment."

## Pre-check: kustomize symlink bug fix

The Makefile uses kustomize v5, which blocks symlinks pointing outside the build root. The `deploy/lib/infra_wva.sh` script uses `ln -s` for the namespace-scoped patch, which triggers this restriction. Apply the fix before running:

```bash
sed -i 's/ln -s "\$WVA_PROJECT\/config\/manager\/namespace-scoped-patch.yaml"/cp "$WVA_PROJECT\/config\/manager\/namespace-scoped-patch.yaml"/' \
  $WVA_REPO_PATH/deploy/lib/infra_wva.sh
```

Or open `$WVA_REPO_PATH/deploy/lib/infra_wva.sh` line ~98 and change:
```bash
# Before (broken on kustomize v5):
ln -s "$WVA_PROJECT/config/manager/namespace-scoped-patch.yaml" "$tmp_overlay/namespace-scoped-patch.yaml"
# After (fix):
cp "$WVA_PROJECT/config/manager/namespace-scoped-patch.yaml" "$tmp_overlay/namespace-scoped-patch.yaml"
```

This fix is tracked upstream. If `$WVA_REPO_PATH` already has it applied (check with `grep "cp.*namespace-scoped" $WVA_REPO_PATH/deploy/lib/infra_wva.sh`), skip this step.

## Deploy Commands

All configuration must be `export`ed as environment variables **before** calling `make`.

### Kubernetes

```bash
cd $WVA_REPO_PATH

export WVA_NS=$WVA_NS
export LLMD_NS=$WVA_NS
export NAMESPACE_SCOPED=true
export SCALER_BACKEND=<prometheus-adapter|keda>
export DEPLOY_LLM_D_INFRA=false        # skip llm-d deployment (already deployed)
export DEPLOY_LWS=false                # set false if LWS already installed
export DEPLOY_PROMETHEUS=true          # set false if Prometheus already installed
export DEPLOY_WVA=true
export DEPLOY_PROMETHEUS_ADAPTER=true  # set false if using KEDA

make deploy-wva-on-k8s IMG=ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest
```

### OpenShift

```bash
cd $WVA_REPO_PATH

export WVA_NS=$WVA_NS
export LLMD_NS=$WVA_NS
export NAMESPACE_SCOPED=true
export SCALER_BACKEND=prometheus-adapter
export DEPLOY_LLM_D_INFRA=false
export MONITORING_NAMESPACE=$MONITORING_NAMESPACE  # detected in step 4a.5
export SKIP_TLS_VERIFY=true

INSTALL_GATEWAY_CTRLPLANE=false \
make deploy-wva-on-openshift IMG=ghcr.io/llm-d/llm-d-workload-variant-autoscaler:latest
```

> **OpenShift exit code 2**: The Makefile may exit 2 even when WVA itself succeeded (chained scripts). Always verify with kubectl before assuming failure.

## What the Makefile creates

- WVA controller Deployment via Kustomize (`config/default` or `config/openshift`)
- Prometheus monitoring stack (if `DEPLOY_PROMETHEUS=true`)
- Scaler backend — Prometheus Adapter (HPA) or KEDA
- **No VariantAutoscaling or HPA resources** — apply those in step 4e

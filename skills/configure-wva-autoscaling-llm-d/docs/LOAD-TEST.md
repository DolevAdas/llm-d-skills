# WVA Load Test (Step 6)

**Only run if user says yes.**

## What the test does

Sends concurrent streaming requests to fill KV cache, triggering WVA to recommend scale-up.

## Run the test

```bash
cd skills/configure-wva-autoscaling-llm-d/scripts/
./test-wva-scaling.sh $WVA_NS <deployment-name> "<model-id>" 200
```

If the script fails (e.g., no gateway/InferencePool), use direct pod IP:
```bash
POD_IP=$(kubectl get pod -n $WVA_NS -l llm-d.ai/role=decode -o jsonpath='{.items[0].status.podIP}')

kubectl run wva-load-test -n $WVA_NS --rm -i --restart=Never \
  --image=curlimages/curl:latest \
  --command -- sh -c "
for i in \$(seq 1 200); do
  curl -s -N -X POST \"http://$POD_IP:8000/v1/chat/completions\" \
    -H \"Content-Type: application/json\" \
    -d '{\"model\":\"<model-id>\",\"messages\":[{\"role\":\"user\",\"content\":\"Write a long detailed essay. Part '\$i'.\"}],\"max_tokens\":4000,\"stream\":true}' \
    > /dev/null 2>&1 &
done
wait"
```

## Monitor while test runs

```bash
# Watch WVA decisions
kubectl logs -n $WVA_NS -l control-plane=controller-manager -f | grep -E "shouldScaleUp|desiredReplicas"

# Watch HPA
kubectl get hpa -n $WVA_NS -w

# Watch replicas
kubectl get deployment <deployment-name> -n $WVA_NS -w
```

## Expected result

1. WVA log: `"shouldScaleUp": true, "desiredReplicas": 2`
2. HPA target increases
3. After stabilization window: new pod starts

Report outcome to user.

#!/bin/bash
# generate-deploy-script.sh - Generate customized WVA deployment script
# This script creates a deployment script from template with user-specific values
#
# Usage: ./generate-deploy-script.sh [options]
#
# The script will prompt for required values or accept them as arguments

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="$SCRIPT_DIR/deploy-wva.sh.template"

# Default values
NAMESPACE=""
DEPLOYMENT_NAME=""
WVA_REPO_PATH=""
MODEL_ID=""
VARIANT_COST="100"
PROMETHEUS_URL=""
PROMETHEUS_INSECURE_SKIP_VERIFY="true"
KV_CACHE_THRESHOLD="0.80"
QUEUE_LENGTH_THRESHOLD="5"
KV_SPARE_TRIGGER="0.10"
QUEUE_SPARE_TRIGGER="3"
MIN_REPLICAS="2"
MAX_REPLICAS="10"
SCALE_UP_STABILIZATION="120"
SCALE_DOWN_STABILIZATION="300"
OUTPUT_FILE=""
NON_INTERACTIVE="false"

# Function to prompt for value
prompt_value() {
    local var_name="$1"
    local prompt_text="$2"
    local default_value="$3"
    local current_value="${!var_name}"
    
    if [ -z "$current_value" ]; then
        if [ -n "$default_value" ]; then
            read -p "$prompt_text [$default_value]: " input
            eval "$var_name=\"${input:-$default_value}\""
        else
            read -p "$prompt_text: " input
            eval "$var_name=\"$input\""
        fi
    fi
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        --deployment)
            DEPLOYMENT_NAME="$2"
            shift 2
            ;;
        --wva-repo)
            WVA_REPO_PATH="$2"
            shift 2
            ;;
        --model-id)
            MODEL_ID="$2"
            shift 2
            ;;
        --variant-cost)
            VARIANT_COST="$2"
            shift 2
            ;;
        --prometheus-url)
            PROMETHEUS_URL="$2"
            shift 2
            ;;
        --min-replicas)
            MIN_REPLICAS="$2"
            shift 2
            ;;
        --max-replicas)
            MAX_REPLICAS="$2"
            shift 2
            ;;
        --kv-threshold)
            KV_CACHE_THRESHOLD="$2"
            shift 2
            ;;
        --queue-threshold)
            QUEUE_LENGTH_THRESHOLD="$2"
            shift 2
            ;;
        --scale-up-window)
            SCALE_UP_STABILIZATION="$2"
            shift 2
            ;;
        --scale-down-window)
            SCALE_DOWN_STABILIZATION="$2"
            shift 2
            ;;
        --output)
            OUTPUT_FILE="$2"
            shift 2
            ;;
        --non-interactive|-y)
            NON_INTERACTIVE="true"
            shift
            ;;
        --help)
            echo "Usage: $0 [options]"
            echo ""
            echo "Options:"
            echo "  --namespace <name>           Target namespace (required)"
            echo "  --deployment <name>          Deployment name (required)"
            echo "  --wva-repo <path>            Path to WVA repository (required)"
            echo "  --model-id <id>              Model ID (required)"
            echo "  --variant-cost <cost>        Variant cost (default: 100)"
            echo "  --prometheus-url <url>       Prometheus URL (optional)"
            echo "  --min-replicas <n>           Minimum replicas (default: 2)"
            echo "  --max-replicas <n>           Maximum replicas (default: 10)"
            echo "  --kv-threshold <n>           KV cache threshold (default: 0.80)"
            echo "  --queue-threshold <n>        Queue length threshold (default: 5)"
            echo "  --scale-up-window <sec>      Scale-up stabilization (default: 120)"
            echo "  --scale-down-window <sec>    Scale-down stabilization (default: 300)"
            echo "  --output <file>              Output file (default: deploy-wva-<deployment>.sh)"
            echo "  --non-interactive, -y        Skip all prompts and confirmations"
            echo "  --help                       Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Check if all required parameters are provided for non-interactive mode
if [ "$NON_INTERACTIVE" = "false" ]; then
    if [ -n "$NAMESPACE" ] && [ -n "$DEPLOYMENT_NAME" ] && [ -n "$WVA_REPO_PATH" ] && [ -n "$MODEL_ID" ]; then
        NON_INTERACTIVE="true"
    fi
fi

echo "=========================================="
echo "WVA Deployment Script Generator"
echo "=========================================="
echo ""

# Only prompt if in interactive mode
if [ "$NON_INTERACTIVE" = "false" ]; then
    # Prompt for required values if not provided
    prompt_value NAMESPACE "Enter namespace" ""
    prompt_value DEPLOYMENT_NAME "Enter deployment name" ""
    prompt_value WVA_REPO_PATH "Enter WVA repository path" ""
    prompt_value MODEL_ID "Enter model ID" ""

    # Prompt for optional values with defaults
    echo ""
    echo "Optional configuration (press Enter to use defaults):"
    prompt_value VARIANT_COST "Variant cost" "$VARIANT_COST"
    prompt_value PROMETHEUS_URL "Prometheus URL" ""
    prompt_value MIN_REPLICAS "Minimum replicas" "$MIN_REPLICAS"
    prompt_value MAX_REPLICAS "Maximum replicas" "$MAX_REPLICAS"
    prompt_value KV_CACHE_THRESHOLD "KV cache threshold" "$KV_CACHE_THRESHOLD"
    prompt_value QUEUE_LENGTH_THRESHOLD "Queue length threshold" "$QUEUE_LENGTH_THRESHOLD"
    prompt_value SCALE_UP_STABILIZATION "Scale-up stabilization (seconds)" "$SCALE_UP_STABILIZATION"
    prompt_value SCALE_DOWN_STABILIZATION "Scale-down stabilization (seconds)" "$SCALE_DOWN_STABILIZATION"
fi

# Set output file if not specified
if [ -z "$OUTPUT_FILE" ]; then
    OUTPUT_FILE="deploy-wva-${DEPLOYMENT_NAME}.sh"
fi

echo ""
echo "=========================================="
echo "Configuration Summary"
echo "=========================================="
echo "Namespace:              $NAMESPACE"
echo "Deployment:             $DEPLOYMENT_NAME"
echo "WVA Repository:         $WVA_REPO_PATH"
echo "Model ID:               $MODEL_ID"
echo "Variant Cost:           $VARIANT_COST"
echo "Prometheus URL:         ${PROMETHEUS_URL:-<not set>}"
echo "Min/Max Replicas:       $MIN_REPLICAS/$MAX_REPLICAS"
echo "KV Cache Threshold:     $KV_CACHE_THRESHOLD"
echo "Queue Length Threshold: $QUEUE_LENGTH_THRESHOLD"
echo "Scale-up Window:        ${SCALE_UP_STABILIZATION}s"
echo "Scale-down Window:      ${SCALE_DOWN_STABILIZATION}s"
echo "Output File:            $OUTPUT_FILE"
echo "=========================================="
echo ""

# Only ask for confirmation in interactive mode
if [ "$NON_INTERACTIVE" = "false" ]; then
    read -p "Generate deployment script? (y/n): " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        echo "Cancelled."
        exit 0
    fi
fi

# Generate script from template
echo "Generating deployment script..."

# Read template and replace variables
sed -e "s|{{NAMESPACE}}|$NAMESPACE|g" \
    -e "s|{{DEPLOYMENT_NAME}}|$DEPLOYMENT_NAME|g" \
    -e "s|{{WVA_REPO_PATH}}|$WVA_REPO_PATH|g" \
    -e "s|{{MODEL_ID}}|$MODEL_ID|g" \
    -e "s|{{VARIANT_COST}}|$VARIANT_COST|g" \
    -e "s|{{PROMETHEUS_URL}}|$PROMETHEUS_URL|g" \
    -e "s|{{PROMETHEUS_INSECURE_SKIP_VERIFY}}|$PROMETHEUS_INSECURE_SKIP_VERIFY|g" \
    -e "s|{{KV_CACHE_THRESHOLD}}|$KV_CACHE_THRESHOLD|g" \
    -e "s|{{QUEUE_LENGTH_THRESHOLD}}|$QUEUE_LENGTH_THRESHOLD|g" \
    -e "s|{{KV_SPARE_TRIGGER}}|$KV_SPARE_TRIGGER|g" \
    -e "s|{{QUEUE_SPARE_TRIGGER}}|$QUEUE_SPARE_TRIGGER|g" \
    -e "s|{{MIN_REPLICAS}}|$MIN_REPLICAS|g" \
    -e "s|{{MAX_REPLICAS}}|$MAX_REPLICAS|g" \
    -e "s|{{SCALE_UP_STABILIZATION}}|$SCALE_UP_STABILIZATION|g" \
    -e "s|{{SCALE_DOWN_STABILIZATION}}|$SCALE_DOWN_STABILIZATION|g" \
    "$TEMPLATE_FILE" > "$OUTPUT_FILE"

# Make executable
chmod +x "$OUTPUT_FILE"

echo "✓ Deployment script generated: $OUTPUT_FILE"
echo ""
echo "To deploy WVA, run:"
echo "  ./$OUTPUT_FILE"
echo ""

# Made with Bob

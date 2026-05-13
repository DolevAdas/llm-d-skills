# Connectivity Verification Instructions

This document contains detailed instructions for generating and executing connectivity verification scripts for llm-d deployments.

## When to Use This Guide

Only follow these instructions if the user has agreed to generate a connectivity verification script (after being asked in Step 1 of the main deployment workflow).

## Step 2: Generate and Show Script

**Generate the verification script:**
- Create a shell script named `verify-connectivity-${NAMESPACE}.sh` containing:
  - Commands to expose the endpoint (port-forward, external IP, ingress, or route as described in `${LLMD_PATH}/guides/02_verifying_a_guide.md`)
  - Test endpoint: `curl ${ENDPOINT}/v1/models`
  - Send test request: `curl ${ENDPOINT}/v1/completions -d {...}`
  - Query `/v1/models` first and use the actual returned model name in completion requests
- The script must be non-interactive and include all necessary commands
- Model loading can take several minutes depending on model size
- **After creating the script, show its full content to the user**

## Step 3: MANDATORY - Ask User Permission to Execute

**You MUST use `ask_followup_question` tool - this is NON-NEGOTIABLE**

Ask for permission to execute the script using this example:

```
<ask_followup_question>
<question>I've created the connectivity verification script at verify-connectivity-${NAMESPACE}.sh (shown above). Would you like me to execute it to test the endpoint?</question>
<follow_up>
 <suggest>No</suggest>
 <suggest>Yes, run the verification script</suggest>
</follow_up>
</ask_followup_question>
```

## Step 4: Execute Only After Approval

- Only if user responds "Yes", execute: `bash verify-connectivity-${NAMESPACE}.sh`
- If user responds "No", inform them they can run it manually when ready

## Step 5: Performance Check (After Running Tests)

After successfully running the connectivity verification script, monitor the deployment performance:

- Monitor resource usage: `kubectl top pods -n {namespace}`
- Check GPU utilization (if applicable)
- Verify response times meet requirements

## Critical Rules

- You cannot skip Step 3
- Executing connectivity tests without using `ask_followup_question` first is strictly forbidden
- Always show the script content before asking to execute it
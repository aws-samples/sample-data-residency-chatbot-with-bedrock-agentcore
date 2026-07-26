# Agent_Lambda container image (Task 14.4) — deployed OUT of the VPC.
#
# WHY a container image (not a zip): the Strands Agents SDK + MCP client + boto3
# dependency closure exceeds the comfortable Lambda zip/layer size, so we ship a
# container image (package_type=Image) per the design's Deployment Approach.
#
# The Agent_Lambda only calls Bedrock + AgentCore Gateway/Memory + WS
# @connections — never the database — so it needs no VPC attachment and no NAT.
#
# Build context = the repo root (MNRE-AgentCore-Chatbot/) so we can COPY both
# src/agent and src/common. Build with finch (per core.md tool preferences):
#   finch build -f infra/agent.Dockerfile -t mnre-chatbot-agent:latest \
#       --platform linux/amd64 .
FROM public.ecr.aws/lambda/python:3.13

# Install runtime deps into the Lambda task root.
COPY infra/agent.requirements.txt /tmp/agent.requirements.txt
RUN pip install --no-cache-dir -r /tmp/agent.requirements.txt

# Copy the agent code and the shared schema (single source of truth). Layout
# under ${LAMBDA_TASK_ROOT} makes `agent.*` and `common.*` importable, matching
# the handler path `agent.handler.handler`.
COPY src/agent ${LAMBDA_TASK_ROOT}/agent
COPY src/common ${LAMBDA_TASK_ROOT}/common

# Run as a non-root user (defense in depth; Lambda additionally sandboxes the
# container). The AWS Lambda base image executes the runtime interface client
# for us, which works under a non-root UID as long as the task root is readable.
USER 1001

# Lambda container entrypoint: module.function.
CMD ["agent.handler.handler"]

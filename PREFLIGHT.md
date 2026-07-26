# Pre-flight checks — ap-south-1 (Mumbai)

Run these checks in the target account before deploying. `deploy.py` performs the
critical ones automatically (identity, finch, Bedrock model presence), but this
is the manual checklist and the rationale.

## Bedrock model access (most common blocker)
- The chatbot uses `anthropic.claude-3-haiku-20240307-v1:0` — a bare ON_DEMAND
  `modelId` invocable in `ap-south-1`. Confirm it is listed:
  ```bash
  aws bedrock list-foundation-models --region ap-south-1 --by-provider anthropic \
    --query "modelSummaries[?modelId=='anthropic.claude-3-haiku-20240307-v1:0'].{id:modelId,inf:inferenceTypesSupported}"
  ```
- Then ENABLE model access in the Bedrock console (Model access → Anthropic
  Claude 3 Haiku). Without the grant, the first `Converse` call returns
  `AccessDeniedException`.
- Residency: use the bare `modelId` only. `apac.*` / `global.*` cross-region
  inference profiles are blocked by `src/agent/residency.py` because they can
  route inference outside India.

## Networking
- `deploy.py` (via `infra/provision_network.py`) creates a fresh dedicated VPC
  (`10.20.0.0/16`) with 2 private subnets across `ap-south-1a` / `ap-south-1b`,
  a private route table (no `0.0.0.0/0`), and the Secrets Manager interface +
  S3 gateway VPC endpoints. NO NAT gateway, NO internet gateway — Aurora stays
  truly private (`PubliclyAccessible=false`).
- No dependency on the default VPC.

## IAM / permissions to run the deploy
The deploying identity needs permissions across: ec2 (VPC), rds, secretsmanager,
iam (create roles + inline policies), lambda, ecr, dynamodb, apigateway,
apigatewayv2, amplify, s3, bedrock, and bedrock-agentcore(-control). See
DEPLOYMENT.md for the consolidated policy.

## Tooling
- `finch` installed and `finch vm start` run (builds the Agent_Lambda image).
- `uv` installed (Python 3.12/3.13).

## Status gate
All green ⇒ safe to run `uv run python deploy.py`.

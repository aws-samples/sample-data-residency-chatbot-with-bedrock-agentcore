# Pre-flight checks — ap-south-1 (Mumbai)

Run these checks in the target account before deploying. `deploy.py` performs the
critical ones automatically (identity, finch, Bedrock model presence), but this
is the manual checklist and the rationale.

## Bedrock model access (most common blocker)
- The chatbot is model-agnostic: set `model_id` (in `deploy.config.json`) to a
  bare ON_DEMAND `modelId` invocable in `ap-south-1` with Converse tool-use
  support. Confirm your chosen model is listed:
  ```bash
  aws bedrock list-foundation-models --region ap-south-1 \
    --query "modelSummaries[?modelId=='<your-model-id>'].{id:modelId,inf:inferenceTypesSupported}"
  ```
- Then ENABLE model access in the Bedrock console (Model access → the
  configured model). Without the grant, the first `Converse` call returns
  `AccessDeniedException`.
- Any other bare in-region modelId with tool-use support works — set `model_id`
  in `deploy.config.json`. Check the
  [Bedrock model support by Region](https://docs.aws.amazon.com/bedrock/latest/userguide/models-region-compatibility.html)
  page; regional availability changes over time.
- Residency: use the bare `modelId` only. Cross-region inference profiles
  (`us.*` / `eu.*` / `ap.*` / `apac.*` / `jp.*` / `au.*` / `global.*`) are
  blocked by `src/agent/residency.py` because they can route inference outside
  the target Region.

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

# Deployment — MNRE AgentCore Chatbot

Deploy the full chatbot into your own AWS account. Everything deploys to
`ap-south-1` (Mumbai) — the region is fixed for data residency and is not
configurable.

There are two ways to deploy:

- Option A — One-click CloudFormation (recommended for customers). Launch a
  small template; it runs a CodeBuild job that builds the container image and
  provisions the whole stack. No local tools needed.
- Option B — Run `deploy.py` yourself from a machine with Docker/finch (laptop,
  EC2, Cloud9). Good for development.

Both end with a live Amplify dashboard URL. Both require the two account-level
prerequisites below.

## Account prerequisites (both options)

| Requirement | Notes |
|-------------|-------|
| Bedrock model access | Enable Anthropic Claude 3 Haiku in the Bedrock console → Model access (region `ap-south-1`). Without it the first invoke returns `AccessDeniedException`. This is a one-time console toggle no automation can do for you. |
| Permissions to launch | Option A: permission to create the CloudFormation stack with `CAPABILITY_NAMED_IAM`. Option B: the [IAM policy](#iam-policy-for-the-deploying-identity) below on your identity. |

## Option A — One-click CloudFormation (from an S3 source bundle)

You are given a source bundle `mnre-chatbot.zip`. You upload it to an S3 bucket
in your account, then launch the template `deploy/codebuild-deploy.yaml`. It
creates a CodeBuild project (which has Docker + Python) and auto-starts it;
CodeBuild downloads the bundle from your S3, runs `deploy.py` — the same engine
as Option B — so the Agent_Lambda image is built in the cloud and the full stack
is provisioned. No git access and no local Docker needed.

Step 1 — upload the bundle to your S3 (region ap-south-1):
```bash
aws s3 cp mnre-chatbot.zip s3://<your-bucket>/mnre-chatbot.zip --region ap-south-1
```

Step 2 — launch the stack:

Console:
1. Open CloudFormation in `ap-south-1` → Create stack → With new resources.
2. Upload `deploy/codebuild-deploy.yaml`.
3. Set `SourceBucket=<your-bucket>` and `SourceKey=mnre-chatbot.zip`.
   (Optional) set `WithWebsocket=true` to also provision the production WebSocket API.
4. Acknowledge IAM capability (CAPABILITY_NAMED_IAM) → Create stack.
5. Watch progress in CodeBuild → build history for `mnre-chatbot-deploy-<account>`.
   The Amplify URL is printed at the end of the build log.

CLI:
```bash
aws cloudformation create-stack \
  --region ap-south-1 \
  --stack-name mnre-chatbot-launcher \
  --template-body file://deploy/codebuild-deploy.yaml \
  --parameters ParameterKey=SourceBucket,ParameterValue=<your-bucket> \
               ParameterKey=SourceKey,ParameterValue=mnre-chatbot.zip \
  --capabilities CAPABILITY_NAMED_IAM
```

Cleanup (tears down everything the build created):
```bash
aws codebuild start-build \
  --project-name mnre-chatbot-deploy-<account> \
  --environment-variables-override name=ACTION,value=cleanup,type=PLAINTEXT \
  --region ap-south-1
# then delete the launcher stack:
aws cloudformation delete-stack --stack-name mnre-chatbot-launcher --region ap-south-1
```

Producing the bundle (for whoever hands off the code): from the repo root run
`bash deploy/make_bundle.sh`, which writes a clean `mnre-chatbot.zip` (excludes
runtime state, secrets, venv, and build artifacts).

The rest of this document covers Option B (run `deploy.py` directly).

## Option B prerequisites

| Requirement | Notes |
|-------------|-------|
| AWS account + credentials | Configured for `ap-south-1` (e.g. `aws configure` or `AWS_PROFILE`). |
| `uv` | Python package manager (Python 3.12/3.13). |
| `finch` (or Docker) | Container build for the Agent_Lambda image. For finch run `finch vm start` once; for Docker set `CONTAINER_TOOL=docker`. |
| IAM permissions | The deploying identity needs the permissions in [IAM policy](#iam-policy-for-the-deploying-identity) below. |

Run the pre-flight checklist in [PREFLIGHT.md](PREFLIGHT.md) first. `deploy.py`
also checks identity, the container tool, and model presence automatically.

## Setup

```bash
git clone <this-repo>
cd mnre-chatbot
uv sync

# Optional: only if you want to override defaults (all keys are optional).
cp deploy.config.example.json deploy.config.json
```

## Configuration

Deployment is configured by `deploy.config.json` (optional) plus your AWS
identity. All keys are optional — sensible defaults are derived automatically.

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `account_id` | No | resolved via STS | Your 12-digit AWS account id. |
| `data_bucket` | No | `mnre-chatbot-data-<account_id>` | S3 bucket (ap-south-1) for the demo CSVs; created if missing. |
| `data_prefix` | No | `chatbot-load` | Key prefix inside the bucket. |
| `model_id` | No | `anthropic.claude-3-haiku-20240307-v1:0` | Bedrock ON_DEMAND bare modelId. `apac.*`/`global.*` profiles are rejected by the residency guard. |

Region is hardcoded to `ap-south-1` in `infra/config.py` and is intentionally
not overridable.

## Deploy

```bash
# Full stack with the REST demo transport (default):
uv run python deploy.py

# Non-interactive account (skips the prompt):
uv run python deploy.py --account 123456789012

# Also provision the production WebSocket API:
uv run python deploy.py --with-websocket

# Resume from a specific step after a failure (idempotent):
uv run python deploy.py --from deploy_tool
```

On start, `deploy.py` asks which AWS account to deploy into and verifies your
active credentials resolve to that account (it aborts on mismatch, so the stack
is never deployed to the wrong account). The region is always `ap-south-1`.

The orchestrator runs these steps in order (each idempotent; ids written to
`infra/network_ids.json`):

| # | Step | Creates |
|---|------|---------|
| 1 | make_sample_data | synthetic CSVs in `data/` |
| 2 | provision_network | fresh VPC, private subnets, SGs, Secrets + S3 VPC endpoints |
| 3 | provision_aurora | Aurora PostgreSQL Serverless v2 (private) |
| 4 | wait_aurora | wait for availability, capture endpoint + master secret |
| 5 | provision_iam_ddb | IAM roles + DynamoDB connections table |
| 6 | deploy_bootstrap | create 4 tables + read-only DB user |
| 7 | deploy_loader | create/upload data bucket + bulk-load the sample data |
| 8 | deploy_tool | Tool_Lambda (in-VPC, read-only) |
| 9 | agentcore_setup | AgentCore Gateway (4 tools) + Memory |
| 10 | deploy_agent | Agent_Lambda container image (out-of-VPC) |
| 11 | provision_rest_api | REST API `POST /chat` (demo transport) |
| (opt) | provision_websocket | WebSocket API (production transport) — with `--with-websocket` |
| 12 | deploy_amplify | host UI on Amplify, inject the REST URL into it |

When it finishes, `infra/network_ids.json` holds every resource id. The live
dashboard is `amplify_url`; the chat endpoint is `rest_api_url`.

## Test it

```bash
# From network_ids.json → rest_api_url
curl -sS -X POST "https://<rest-api-id>.execute-api.ap-south-1.amazonaws.com/prod/chat" \
  -H "content-type: application/json" \
  -d '{"question":"How many duplicate bank accounts exist?","sessionId":"demo-1"}'
```

Or open `amplify_url` in a browser and use the chat box.

## Sample data

The repo ships small synthetic CSVs (`data/*.csv`, ~400 rows/table, zero real
PII) generated by `tools/make_sample_data.py`. To regenerate or resize:

```bash
uv run python tools/make_sample_data.py --rows 400
```

To load your own data instead, replace `data/*.csv` with files matching the
curated schema (header = the columns in `src/common/schema.py`, NULL = empty
cell, booleans `true`/`false`, dates `YYYY-MM-DD`), then re-run the loader step.

## Production considerations (existing database)

This deploy is self-contained: it creates a NEW VPC and its OWN Aurora cluster
and loads the sample data — ideal for a demo. To run against a customer's
EXISTING PM Surya Ghar database, two changes are required:

1. Same VPC as the database. The Tool_Lambda runs in a VPC and must reach the
   existing Aurora on 5432. By default the stack creates a new VPC
   (`10.20.0.0/16`) that cannot reach a DB in another VPC. Adapt
   `infra/provision_network.py` to reuse the customer's existing VPC, private
   subnets, and a security group allowed into the DB's security group (rather
   than creating new ones), and ensure a Secrets Manager VPC endpoint exists in
   that VPC. Then skip `provision_aurora` / `deploy_bootstrap` / `deploy_loader`
   and set `db_endpoint`, `db_name`, `db_readonly_secret_arn` in
   `network_ids.json` to the existing cluster's values before `deploy_tool`.

2. Read replica, not the writer. Point `db_endpoint` at the Aurora READER
   endpoint (a read replica), not the writer endpoint, so heavy analytical
   queries never touch the primary write instance. The read-only user +
   `set_session(readonly=True)` enforce read-only access; the reader endpoint
   additionally isolates the performance impact from the production workload.

The rest of the stack (Agent_Lambda, Bedrock, AgentCore Gateway/Memory, API, UI)
is unchanged.

## Cleanup (destroy everything)

```bash
uv run python cleanup.py                       # prompts for target account + confirmation
uv run python cleanup.py --account 123456789012 --yes --delete-bucket
```

Like `deploy.py`, it first asks which account to clean and verifies your active
credentials resolve to it (aborting on mismatch). Best-effort reverse cleanup
(VPC, Aurora, Lambdas, ECR, Gateway, Memory, APIs, Amplify, IAM, DynamoDB, DB
secret). The demo-data S3 bucket is kept unless `--delete-bucket` is passed;
CloudWatch log groups are kept (cheap, useful for post-mortem). The local
`network_ids.json` is removed at the end (pass `--keep-state` to retain it).

## Data residency & Amplify

Amplify hosts only the static UI shell (HTML/JS). All program data flows from the
browser to the regional `ap-south-1` API endpoint and back; no data or inference
leaves India. There is no CloudFront (a global service) anywhere in the stack.

## Authorizer gap (production)

The REST and WebSocket endpoints are open (no authorizer) for the demo. For
production, add an API Gateway authorizer (Cognito / Lambda authorizer / IAM) in
front of `POST /chat` (or the `sendMessage` route) before exposing the endpoint.

## IAM policy for the deploying identity

The identity running `deploy.py` needs to create and manage resources across
these services (scope down as your governance requires):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "IamScopedToProjectRoles",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole", "iam:DeleteRole", "iam:GetRole",
        "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:ListRolePolicies",
        "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:ListAttachedRolePolicies",
        "iam:TagRole", "iam:UpdateAssumeRolePolicy"
      ],
      "Resource": "arn:aws:iam::<ACCOUNT_ID>:role/mnre-chatbot-*"
    },
    {
      "Sid": "PassProjectRolesOnly",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::<ACCOUNT_ID>:role/mnre-chatbot-*",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": [
            "lambda.amazonaws.com",
            "rds.amazonaws.com",
            "bedrock-agentcore.amazonaws.com"
          ]
        }
      }
    },
    {
      "Sid": "ServiceProvisioning",
      "Effect": "Allow",
      "Action": [
        "ec2:*Vpc*", "ec2:*Subnet*", "ec2:*RouteTable*", "ec2:*SecurityGroup*",
        "ec2:*VpcEndpoint*", "ec2:CreateTags", "ec2:Describe*",
        "rds:*", "secretsmanager:*", "dynamodb:*",
        "lambda:*", "ecr:*", "logs:*",
        "apigateway:*",
        "amplify:*", "s3:*",
        "bedrock:ListFoundationModels", "bedrock:InvokeModel",
        "bedrock-agentcore:*",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    }
  ]
}
```

Replace `<ACCOUNT_ID>` with your 12-digit account id. `iam:PassRole` is
restricted to the project's `mnre-chatbot-*` roles and the services they are
passed to; for the tightest posture, also scope the `ServiceProvisioning`
statement to `mnre-chatbot-*` ARNs per service, as done in
`deploy/codebuild-deploy.yaml`.

## Documentation

| Doc | Purpose |
|-----|---------|
| [README.md](README.md) | What it does, how to use it |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How it works internally |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Setup, deploy, teardown |

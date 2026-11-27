# Data-residency chatbot with Amazon Bedrock AgentCore

A natural-language chatbot that answers questions from a database while the services that process prompts, model output, and query results run in one AWS Region (`ap-south-1`, Mumbai). Users ask plain-English questions and get answers computed from structured data in Amazon Aurora PostgreSQL-Compatible Edition, without anyone writing SQL.

The pattern was built for a national rooftop-solar subsidy program in India and is demonstrated here with fully synthetic sample data. India has two AWS Regions, Mumbai and Hyderabad; this sample keeps processing in one Region because a single-Region data path is the simplest to audit and some workloads require it. The residency controls and the governed text-to-SQL design aren't specific to India, and you can adapt them for other sectors and geographies (see [Adapting to your Region](#adapting-to-your-region-and-use-case)).

Two things set it apart from a plain text-to-SQL chatbot: processing is kept in one Region at the model, network, tool, and service layers, and the model never writes SQL. It chooses one of four read-only tools and fills in a structured request, and the tool code builds a parameterized query from an allowlist.

The companion blog post walks through the design in detail: five data-residency controls, the governed text-to-SQL security design, a comparison with Amazon Bedrock Knowledge Bases, and compliance considerations.

## Architecture

<img width="3023" height="1685" alt="image" src="https://github.com/user-attachments/assets/50cf451a-a210-49b4-946c-6c33a94b50db" />

*Figure 1: Target architecture. The numbered arrows match the steps below.*

The solution uses an agent built with the open source [Strands Agents SDK](https://strandsagents.com/), running in AWS Lambda. The agent uses two Amazon Bedrock AgentCore services: AgentCore Gateway, which exposes the database query tools, and AgentCore Memory, which stores conversation history for follow-up questions. Each step that processes the prompt, model output, or query results runs in `ap-south-1`.

1. The user opens the web UI. AWS Amplify Hosting serves the static page files. These files don't contain program data.
2. The browser sends the question to an Amazon API Gateway REST API with a regional endpoint in `ap-south-1`. The sample API has no authorizer so that you can try it quickly. Don't use it as is in production (see [Before you use this in production](#before-you-use-this-in-production)).
3. API Gateway invokes the Agent Lambda function. When the function starts, a residency guard checks the configured model ID (`src/agent/residency.py`).
4. The agent loads earlier turns of the conversation from AgentCore Memory.
5. The agent sends the question and the tool definitions to Amazon Bedrock in `ap-south-1`, using a model that supports in-Region on-demand invocation. The model chooses a tool and its parameters.
6. The agent calls the tool through AgentCore Gateway over Model Context Protocol (MCP), with requests signed using AWS Signature Version 4 (SigV4). The gateway invokes the Tool Lambda function, which runs in private VPC subnets with no route to the internet.
7. The Tool Lambda function checks the request against an allowlist, reads read-only database credentials from AWS Secrets Manager through a VPC endpoint, builds a parameterized `SELECT` query, and runs it against Aurora PostgreSQL.
8. The results return through AgentCore Gateway to the agent. The agent sends them to Amazon Bedrock to write the answer, saves the turn to AgentCore Memory, and returns the answer through API Gateway to the browser.

Some parts of the architecture are global, and none of them process program data: Amplify Hosting serves the static UI files through Amazon CloudFront edge locations (questions and answers go directly between the browser and the regional API); the AWS WAF web ACL that protects the Amplify app must use the global (CloudFront) scope, holds firewall rules only, and has request sampling turned off; IAM stores roles and policies; and DNS resolution happens wherever the user's resolver is. See [Data residency & the Amplify firewall](DEPLOYMENT.md#data-residency--amplify).

> **Transport note:** the REST API (`POST /chat`, request/response) is what the deployment wires up and what the UI calls. An optional WebSocket API (`wss://`) exists for longer-running conversations — deploy it with `deploy.py --with-websocket`. Every other component is identical for both.

## Five data-residency controls

Each control addresses a different way that data or inference could leave the target Region. The blog post explains each one in depth; the summary:

| Control | What it does | Where |
|---------|--------------|-------|
| 1. In-Region on-demand invocation | The agent calls Bedrock with a base model ID (no `us.`/`eu.`/`apac.`/`global.` inference-profile prefix) through a client set to `ap-south-1`. The Agent Lambda IAM role allows `bedrock:InvokeModel` only on the ARN of the model you configure at deployment, with no inference-profile ARNs, so a call that uses a profile ID fails authorization. | `src/agent/handler.py`, `infra/provision_iam_ddb.py` |
| 2. Cold-start residency guard | At startup the function raises if the model ID starts with a known cross-Region profile prefix, so a configuration mistake fails loudly instead of moving inference silently. It is a fast first check; the IAM policy in Control 1 is the enforcement point. An AWS Organizations SCP that denies `bedrock:InvokeModel*` on `inference-profile/*` adds an account-level backstop. | `src/agent/residency.py` |
| 3. Network isolation for database access | The Tool Lambda function is the only component with database credentials. It runs in private subnets with no internet gateway, no NAT gateway, and no route to the internet; it reaches Aurora on 5432 and Secrets Manager through an interface VPC endpoint, with security-group rules that reference security groups rather than CIDR ranges. The Agent Lambda function runs outside the VPC, has no database credentials, and is not accepted by the Aurora security group. | `infra/provision_network.py` |
| 4. Authenticated tool gateway | AgentCore Gateway uses IAM inbound authorization, so each MCP call must be SigV4-signed. Only the Agent Lambda role is granted `bedrock-agentcore:InvokeGateway`, and only on this gateway's ARN. The gateway's service role can invoke only the Tool Lambda function. | `infra/agentcore_setup.py`, `src/agent/sigv4.py` |
| 5. Regional data path | API Gateway (REST, regional endpoint), both Lambda functions, Bedrock, AgentCore Gateway and Memory, Aurora, and Secrets Manager run in `ap-south-1`. No edge-optimized API endpoints or AWS Global Accelerator. The global pieces (Amplify's CloudFront edge, the WAF web ACL, IAM, DNS) carry configuration and static files only. | whole stack |

The Agent Lambda function's permissions are scoped to the resources it uses. With `YOUR_MODEL_ID` replaced by the model you pass as `ModelId` (or `model_id`) at deployment, the policy that `infra/provision_iam_ddb.py` and `infra/agentcore_setup.py` attach looks like this:

```json
{
  "Statement": [
    {
      "Sid": "BedrockInvokeInRegion",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": "arn:aws:bedrock:ap-south-1::foundation-model/YOUR_MODEL_ID"
    },
    {
      "Effect": "Allow",
      "Action": "bedrock-agentcore:InvokeGateway",
      "Resource": "arn:aws:bedrock-agentcore:ap-south-1:111122223333:gateway/GATEWAY_ID"
    },
    {
      "Effect": "Allow",
      "Action": ["bedrock-agentcore:CreateEvent", "bedrock-agentcore:ListEvents"],
      "Resource": "arn:aws:bedrock-agentcore:ap-south-1:111122223333:memory/MEMORY_ID"
    }
  ]
}
```

There is no wildcard over other foundation models and no inference-profile ARN, so a call that uses a cross-Region profile ID fails authorization even if the residency guard were bypassed.

These controls apply to data-plane processing. Check with your legal and compliance teams whether they meet your requirements.

## Deploying the solution

The deployment uses an AWS CloudFormation stack that starts an AWS CodeBuild project. The project builds the agent container image, creates the VPC and Aurora database, configures the AgentCore resources, and deploys the Amplify-hosted UI. Deployment takes about 15 minutes and ends with a live Amplify URL. The sample ships with small synthetic data (no real PII). The full guide, including running `deploy.py` from your own machine, is in [DEPLOYMENT.md](DEPLOYMENT.md).

### Prerequisites

- An AWS account with permission to create CloudFormation stacks with named IAM resources. This sample uses `ap-south-1`. To use another Region, first confirm that AgentCore Gateway, AgentCore Memory, and an in-Region model are available there (see [Adapting to your Region](#adapting-to-your-region-and-use-case)).
- The [AWS Command Line Interface (AWS CLI)](https://aws.amazon.com/cli/) version 2.
- Model access enabled in the Amazon Bedrock console for a model that supports in-Region on-demand invocation (next section).
- An Amazon S3 bucket in the target Region for the source bundle.

### Step 0 — choose your model

There is no default model; you pick one, and the Agent Lambda IAM role is scoped to that model's ARN at deployment.

1. Open [Model support by AWS Region](https://docs.aws.amazon.com/bedrock/latest/userguide/models-region-compatibility.html) and pick a model that is available in `ap-south-1` with a base model ID (no `us.`/`eu.`/`ap.`/`apac.`/`jp.`/`au.`/`global.` inference-profile prefix) and Converse tool-use support. The residency guard rejects inference-profile IDs.
2. Confirm it supports in-Region on-demand invocation. The output must include `ON_DEMAND`; the deploy preflight fails fast if it doesn't:
   ```bash
   aws bedrock list-foundation-models --region ap-south-1 \
     --query "modelSummaries[?modelId=='<your-model-id>'].{id:modelId,inf:inferenceTypesSupported}"
   ```
   To list every on-demand text model in the Region instead:
   ```bash
   aws bedrock list-foundation-models --region ap-south-1 --by-inference-type ON_DEMAND \
     --query "modelSummaries[?contains(outputModalities,'TEXT')].modelId" --output text
   ```
3. Enable access for it in the Bedrock console → Model access (Region `ap-south-1`). Without it the first invoke returns `AccessDeniedException`.

**Tested model.** Tool-calling reliability varies a lot between on-demand models, and a model that ignores the tool will invent plausible-looking numbers. `qwen.qwen3-235b-a22b-2507-v1:0` answered correctly against the sample database in every test run (counts, follow-ups through Memory, and out-of-scope refusals) and is the model this repository was last verified with. Whatever you choose, check the first few answers against the data before you trust them; the sample data has 400 rows per table, and the top state by applications is Maharashtra with 169, so a very different number means the model answered without calling the tool. As of this writing, Anthropic and Amazon Nova text models in `ap-south-1` are offered only through inference profiles and so can't be used with this sample there.

### Option A — CloudFormation launcher (recommended)

No git access or local container tooling needed. A CodeBuild job unpacks the source bundle, builds the image, and provisions everything.

1. Create the source bundle from the repository root. It writes `residency-chatbot.zip`:
   ```bash
   bash deploy/make_bundle.sh
   ```
2. Upload the bundle to your S3 bucket:
   ```bash
   aws s3 cp residency-chatbot.zip s3://<your-bucket>/residency-chatbot.zip --region ap-south-1
   ```
3. Create the launcher stack, passing your model ID as the `ModelId` parameter:
   ```bash
   aws cloudformation create-stack --region ap-south-1 \
     --stack-name residency-chatbot-launcher \
     --template-body file://deploy/codebuild-deploy.yaml \
     --parameters ParameterKey=SourceBucket,ParameterValue=<your-bucket> \
                  ParameterKey=SourceKey,ParameterValue=residency-chatbot.zip \
                  ParameterKey=ModelId,ParameterValue=<your-model-id> \
     --capabilities CAPABILITY_NAMED_IAM
   ```
   (Console equivalent: upload `deploy/codebuild-deploy.yaml`, fill in `SourceBucket`, `SourceKey`, and `ModelId` on the parameters screen.)
4. In the CodeBuild console, open the build history for `residency-chatbot-deploy-<your-account-id>`. When the build finishes, copy the Amplify URL from the end of the build log.
5. Open the URL and ask a question, such as "Which are the top 5 states by applications?" Then ask a follow-up, such as "And in Gujarat?"

### Option B — run deploy.py yourself (laptop/EC2 with Docker or finch)

1. Clone the repo and install dependencies:
   ```bash
   uv sync
   ```
2. Set your model id — either in `deploy.config.json` (persists):
   ```bash
   cp deploy.config.example.json deploy.config.json
   # edit deploy.config.json and set:  "model_id": "<your-model-id>"
   ```
   or as a one-off environment variable:
   ```bash
   export CHATBOT_MODEL_ID=<your-model-id>
   ```
3. Deploy:
   ```bash
   uv run python deploy.py               # full stack, REST transport
   # or: uv run python deploy.py --with-websocket   # also the optional WebSocket API
   ```
   The preflight fails fast, before creating any resources, if no model is configured or the model is not present in `ap-south-1`. The Amplify URL is printed at the end.

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full guide (IAM policy for the deploying identity, prerequisites, teardown) and [PREFLIGHT.md](PREFLIGHT.md) for the manual checklist.

### Changing the model later

The Agent Lambda IAM role is scoped to the model ARN set at deployment. If you change `model_id` (or the `ModelId` stack parameter), re-run the deploy so that `provision_iam_ddb` rewrites the role policy and `deploy_agent` updates the function's `MODEL_ID`; otherwise the next invoke fails authorization on the new model.

## Governed text-to-SQL security design

The chatbot answers natural-language questions with data from SQL queries, but the model doesn't write SQL. It chooses a tool and fills in a structured request (filters, grouping, aggregations, sort order, and row limit), and the Tool Lambda code builds the query. If a model could generate arbitrary SQL, a prompt injection could read data it shouldn't or try to change records. The following layers reduce that risk:

- **Tool boundary** — the agent can call only four tools, and each tool is fixed to one table:

  | Tool | Table | What it returns |
  |------|-------|-----------------|
  | `query_applications` | `applications` | Rows or aggregates from application records |
  | `query_subsidy` | `subsidy` | Rows or aggregates from subsidy disbursement records |
  | `query_installation` | `installation` | Rows or aggregates from installation records |
  | `query_inspection` | `inspection` | Rows or aggregates from inspection records |

- **Request validation** — `src/tool/validate.py` checks each request against an allowlist of columns, filter operators, and aggregation functions for that table. A request with a field outside the allowlist, including a raw `sql` field, is rejected before a query is built.
- **Parameterized query builder** — `src/tool/query_builder.py` takes table and column names from the allowlist, not from the request, and binds filter values and the row limit as query parameters. This helps protect against SQL injection.
- **Read-only database access** — the function connects as a database role that has `SELECT` privileges, with `INSERT`, `UPDATE`, `DELETE`, and `TRUNCATE` revoked, and opens a read-only session.
- **Row limits** — queries return up to 100 rows by default and 1,000 at most, and the response shows when results were cut off.
- **Credential isolation** — database credentials are stored in Secrets Manager and read from inside the VPC through the endpoint. They aren't stored in code or environment variables, and the function redacts credential values from its log lines.

An accepted request, which counts applications by state and returns the top five:

```json
{
  "table": "applications",
  "group_by": ["state"],
  "aggregations": [{"fn": "count", "column": "application_id", "alias": "applications"}],
  "order_by": [{"by": "applications", "direction": "desc"}],
  "limit": 5
}
```

A rejected request, because `sql` isn't a field in the request contract:

```json
{"table": "applications", "sql": "SELECT * FROM applications"}
```

The validation returns `{"executed": false, "error": "raw SQL or unsupported field(s) not allowed: sql"}`.

A model that's affected by prompt injection still can't reach the database directly, but it can call the allowed tools. The tools limit what it can read. As an extra layer, you can add [Amazon Bedrock Guardrails](https://aws.amazon.com/bedrock/guardrails/), for example the prompt attack filter or word filters for SQL keywords in user input. If you do, confirm that Guardrails is available in your Region and keep guardrail cross-Region inference turned off.

## What kinds of questions it handles

Counts, totals, breakdowns by a dimension (state, bank, discom, gender, category), leaderboards ("top 5 states", "highest-disbursing bank"), duplicate and repeat-beneficiary checks, and turnaround-time metrics, all in plain English. Each number in an answer comes from a live database query; if the data can't answer (for example, a field the tables don't have), the bot says so instead of guessing. Answers are only as current as the data loaded into the database.

## Comparing this approach to Amazon Bedrock Knowledge Bases

[Amazon Bedrock Knowledge Bases](https://aws.amazon.com/bedrock/knowledge-bases/) can also answer questions from your data, with either a structured data store or a vector store for Retrieval Augmented Generation (RAG).

| Approach | Limitation for this use case | When to use it |
|----------|------------------------------|----------------|
| Structured knowledge base | Uses Amazon Redshift as the query engine, with data in Redshift or in Amazon S3 through the AWS Glue Data Catalog, so it doesn't query Aurora PostgreSQL directly. The service generates the SQL, which gives you less control over which tables and columns a query can use. For `ap-south-1`, AWS documentation notes that requests might be processed in Regions outside the local geography. | When your data is in Redshift or the Data Catalog and your residency requirements allow the Regions where requests are processed. |
| Vector knowledge base (RAG) | Returns passages that are similar to the question. It can't compute exact counts, sums, or rankings from structured data. | When answers come from unstructured documents, such as manuals, policy documents, and presentations. |
| Custom governed tools (this solution) | You build the tool layer, and you maintain the allowlist as the database schema changes. | When you need exact computed answers from structured data and control over where processing happens. |

See [ARCHITECTURE.md](ARCHITECTURE.md) for the internals: module layout, the agent loop, the MCP/SigV4 client, and the validator and query builder.

## Production considerations (deploying against an existing database)

The one-click deploy is self-contained: it creates a **new VPC** and its **own Aurora PostgreSQL** cluster, loads the synthetic sample data, and points the Tool_Lambda at it. That is ideal for a demo. When connecting to a customer's **existing** program database, two changes are required:

1. **Deploy into the same VPC as the database.** The Tool_Lambda must run in a VPC (and subnets/security groups) that can reach the existing Aurora cluster on port 5432. By default this stack provisions a brand-new VPC (`10.20.0.0/16`), which cannot reach a DB in a different VPC. For a real deployment, adapt `infra/provision_network.py` (and the ids it writes to `network_ids.json`) to **reuse the customer's existing VPC, private subnets, and a security group allowed into the DB's security group** — instead of creating new ones. Also ensure a Secrets Manager VPC endpoint (or NAT-free path) exists in that VPC so the Tool_Lambda can read the DB credential in-region.

2. **Point the tool at a read replica, not the primary writer.** The chatbot only ever runs read-only `SELECT` queries, but analytical/aggregation queries can be heavy. To protect the production workload, point the Tool_Lambda at an **Aurora reader endpoint (read replica)** rather than the cluster **writer endpoint** — set `db_endpoint` in `network_ids.json` (consumed by `deploy_tool.py`) to the reader endpoint. This keeps all chatbot load off the primary write instance. The read-only DB user and `set_session(readonly=True)` already enforce read-only access; using the reader endpoint additionally isolates the *performance* impact.

In short: for production, skip the "create new VPC + new Aurora + load sample data" steps and instead wire the Tool_Lambda into the existing VPC and the reader endpoint of the existing cluster. The rest of the stack (Agent_Lambda, Bedrock, AgentCore Gateway/Memory, API, UI) is unchanged.

## Before you use this in production

The sample is set up for evaluation. Before you use it with real data:

- **Add authentication and throttling to the API.** The REST API has no authorizer and allows requests from any origin. Add an authorizer, such as Amazon Cognito or IAM authorization, set stage throttling, restrict CORS to your UI's domain, and associate a regional AWS WAF web ACL with the API stage (see [Authorizer gap](DEPLOYMENT.md#authorizer-gap-production)).
- **Attach the Agent Lambda function to the VPC.** It runs outside the VPC today because it calls Bedrock and AgentCore, so it has outbound internet access. Attach it to private subnets and add interface VPC endpoints for Bedrock, AgentCore, AWS STS, and Amazon CloudWatch Logs, after you confirm the endpoints are available in your Region.
- **Restrict outbound security group rules** for the Tool Lambda function to the Aurora and Secrets Manager endpoint security groups. The route table is what keeps its traffic inside the VPC today.
- **Set log retention** on the CloudWatch log groups (the sample doesn't set one), and use AWS CloudTrail for API-level audit records.
- **Review the IAM policies** rendered by `deploy/render_deployer_policy.py` against your own standards. The Agent Lambda role is already scoped to the configured model ARN, the gateway ARN, and the memory ARN.

## Clean up

To avoid ongoing charges, remove the resources the solution created. Start the cleanup build first:

```bash
aws codebuild start-build \
  --project-name residency-chatbot-deploy-<your-account-id> \
  --environment-variables-override name=ACTION,value=cleanup,type=PLAINTEXT \
  --region ap-south-1
```

After the build finishes, delete the launcher stack:

```bash
aws cloudformation delete-stack --stack-name residency-chatbot-launcher --region ap-south-1
```

The cleanup build removes the AWS WAF web ACL, the VPC, the Aurora cluster (without a final snapshot), the Lambda functions, the Amazon ECR repository, the AgentCore Gateway and Memory, the API Gateway APIs, the Amplify app, the DynamoDB table, the read-only database secret, and the IAM roles that the deployment created. It also deletes the demo data bucket. It keeps the CloudWatch log groups, so delete them yourself if you don't need them, along with the S3 bucket that you created for the source bundle. If you deployed with `deploy.py`, run `uv run python cleanup.py` instead.

## Adapting to your Region and use case

This repository documents the pattern with `ap-south-1` (Mumbai) and a rooftop-solar subsidy program as the concrete, deployable reference. The residency controls aren't India-specific; you can adapt them for other sectors and geographies, such as organizations that work under EU data-protection rules or financial-services regulations in Southeast Asia, after you confirm that the required services and an in-Region model are available in the target Region:

1. **Region** — the Region is pinned in `infra/config.py` (`REGION`) and in the two Lambda handlers; it is not a stack parameter. Change it to your target Region; the data-path clients in the stack are Region-pinned from that value. One deliberate exception: the WAF web ACL for the Amplify UI is always created in the global CloudFront scope (`us-east-1`), because Amplify's firewall integration accepts nothing else. This is firewall configuration, not data, but if your rules don't allow global configuration resources, disable the `provision_waf` step (see [DEPLOYMENT.md](DEPLOYMENT.md#data-residency--amplify)).
2. **Model** — pick a model with a base model ID that supports in-Region on-demand invocation and Converse tool-use in your Region from [Model support by AWS Region](https://docs.aws.amazon.com/bedrock/latest/userguide/models-region-compatibility.html), and set `model_id` in `deploy.config.json`. Availability differs per Region and changes over time. The Agent Lambda role is scoped to this model's ARN.
3. **Residency guard** — `src/agent/residency.py` rejects the known cross-Region inference-profile prefixes (`us.*`, `eu.*`, `ap.*`, `apac.*`, `jp.*`, `au.*`, `global.*`) regardless of target Region; no change needed. If your requirement is in-country rather than single-Region and Bedrock offers a geographic inference profile for that country, the guard and the IAM policy block it too, so adjust both if you choose that option.
4. **Data model** — replace the 4 curated tables and the allowlist in `src/common/schema.py` with your own tables and columns; the validator, query builder, and Gateway tool definitions all derive from that single schema module.
5. **Regulatory mapping** — which regulation (if any) requires residency is sector- and geography-specific. The controls in this repository apply to data-plane processing: the services that process prompts, model output, and query results run in one Region. Map that to your own obligations with your legal and compliance teams; using this sample doesn't by itself make you compliant with a regulation.

## Documentation

| Doc | Purpose |
|-----|---------|
| [README.md](README.md) | What it does, how to use it |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How it works internally |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Setup, deploy, teardown |

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.

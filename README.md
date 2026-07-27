Data-residency chatbot with Amazon Bedrock AgentCore

Data-residency-compliant natural-language chatbot. Ask plain-English questions and get answers computed from the program data held in Aurora PostgreSQL — without writing SQL, and without any data or inference leaving India (`ap-south-1`, Mumbai).

## Architecture

<img width="1074" height="586" alt="NLP-BOT" src="https://github.com/user-attachments/assets/fa148c5b-2e85-4935-8772-7b0a0e1af52c" />

Everything sits inside `ap-south-1` (Mumbai). The only thing outside India is the user's browser, and it only ever receives the final natural-language answer — never raw program data. No CloudFront anywhere; Bedrock is invoked in-region with a bare ON_DEMAND `modelId`.

> **Note on the transport:** the system supports two entry points behind API Gateway. The **REST API** (`POST /chat`, request/response) is the default the demo deploy wires up and what the UI calls out of the box. A **WebSocket API** (`wss://`) is the production transport — deploy it with `deploy.py --with-websocket`. Every other box (Agent_Lambda, Bedrock, AgentCore Gateway/Memory, Tool_Lambda, Aurora, Secrets Manager) is identical for both.

## Quick start (deploy to your account)

Two options — both deploy the full stack to `ap-south-1` and end with a live Amplify URL:

Option A — one-click CloudFormation (recommended). Upload the `chatbot.zip` bundle to an S3 bucket in your account, then launch `deploy/codebuild-deploy.yaml` in `ap-south-1`; a CodeBuild job pulls the bundle, builds the image, and provisions everything. No git access or local tools needed.

```bash
aws s3 cp chatbot.zip s3://<your-bucket>/chatbot.zip --region ap-south-1
aws cloudformation create-stack --region ap-south-1 \
  --stack-name chatbot-launcher \
  --template-body file://deploy/codebuild-deploy.yaml \
  --parameters ParameterKey=SourceBucket,ParameterValue=<your-bucket> \
               ParameterKey=SourceKey,ParameterValue=chatbot.zip \
  --capabilities CAPABILITY_NAMED_IAM
```

Option B — run it yourself (laptop/EC2/Cloud9 with Docker or finch):

```bash
uv sync
uv run python deploy.py               # full stack, REST transport
# or: uv run python deploy.py --with-websocket   # also the production WSS API
```

Prerequisite for both: enable Bedrock model access for Claude 3 Haiku in the target account (one console toggle). See [DEPLOYMENT.md](DEPLOYMENT.md) for the full guide and [PREFLIGHT.md](PREFLIGHT.md) for the checklist. The deploy ships with small synthetic sample data (zero real PII); swap in your own CSVs anytime.

### End-to-end steps (numbered to match the diagram)

1. **Load UI** — the Executive opens the dashboard over HTTPS; AWS Amplify serves the static page (charts + chat box).
2. **Ask** — the browser `fetch()`-POSTs the plain-English question to the API Gateway endpoint (`POST /chat`).
3. **Route** — API Gateway invokes the Agent_Lambda (AWS_PROXY) — the Strands agent that reasons over the question.
4. **Reason** — the agent calls Amazon Bedrock (in-region ON_DEMAND) over the Converse tool-use loop; the model decides *which* tool to call and with what parameters.
5. **Recall** — the agent loads/saves the session's prior turns in AgentCore Memory, so follow-ups ("…and in Gujarat?") have context.
6. **Tool call** — the chosen `query_<table>` tool call goes to the Bedrock AgentCore Gateway (MCP server) authenticated with SigV4 IAM.
7. **Invoke Tool_Lambda** — the Gateway invokes the read-only Tool_Lambda, which runs *inside* the private VPC (the only component that can reach the database).
8. **Query (safely)** — Tool_Lambda validates the request against a strict whitelist, builds a SELECT-only parameterized statement, and runs it against Aurora PostgreSQL over `psycopg` (port 5432).
9. **Read credentials (no NAT)** — Tool_Lambda reads the Aurora read-only DB credential from AWS Secrets Manager via an interface VPC endpoint — no NAT gateway, nothing leaves the VPC.

The rows flow back Aurora → Tool_Lambda → Gateway → agent; the agent feeds the real numbers to Bedrock to phrase a clean answer, saves the turn to Memory, and returns the answer in the HTTP response, which the browser displays. CloudWatch + AgentCore Observability capture logs/traces throughout (with DB credentials redacted).

## The one-line story

A user types a plain-English question in a browser; the system answers it from the real PM Surya Ghar program data in a database — without anyone writing SQL, and without any data or AI inference ever leaving India (everything runs in AWS Mumbai, `ap-south-1`).

## The components (who does what)

- **Browser UI (AWS Amplify)** — the dashboard with charts + a chat box. Static page, hosted in-region.
- **API Gateway (REST API)** — the front door. The browser POSTs the question here.
- **Agent_Lambda (the "brain")** — runs a Strands AI agent. It interprets the question, decides what data to fetch, and writes the final natural-language answer.
- **Amazon Bedrock (Claude 3 Haiku)** — the LLM the agent calls to reason and to phrase the answer. Runs in-region, on-demand.
- **AgentCore Gateway** — a secure "tool counter." It exposes 4 safe, read-only query tools (one per data table) to the agent. The agent can't touch the database directly — only through these governed tools.
- **Tool_Lambda** — the only thing that talks to the database. It accepts a structured request (filters, group-by, totals, sort), builds a safe read-only SQL query, and returns rows. No raw SQL is ever allowed.
- **Aurora PostgreSQL (Serverless v2)** — the database holding the curated program data, in private subnets (not reachable from the internet).
- **AgentCore Memory** — remembers the conversation so follow-up questions work ("…and how many in Gujarat?").

## The flow — what happens when someone asks a question

1. **Ask** — In the browser, the user types "Which are the top 5 states by applications?" The page sends it over HTTPS to the REST API.
2. **Route** — API Gateway hands the question to the Agent_Lambda.
3. **Recall** — The agent loads the recent conversation from AgentCore Memory (so follow-ups have context).
4. **Reason** — The agent sends the question to Bedrock (Claude). Claude doesn't know the data; it decides which tool to call and with what parameters — e.g. "query the applications table, group by state, count, sort descending, top 5."
5. **Fetch (safely)** — That tool call goes through the AgentCore Gateway (authenticated) to the Tool_Lambda. The Tool_Lambda validates the request against a strict whitelist, builds a read-only SQL `SELECT`, and runs it against Aurora.
6. **Return data** — Aurora returns the rows → Tool_Lambda → Gateway → back to the agent.
7. **Compose** — The agent feeds the real numbers back to Claude, which writes a clean, plain-English answer.
8. **Remember & reply** — The agent saves the turn to Memory and returns the answer in the HTTP response. The browser displays it.

> The LLM is the reasoning brain, but it never sees the database. It can only ask pre-approved, read-only questions through a guarded gateway. So you get natural-language flexibility with database-grade safety.

## The three messages that matter

- **Data residency** — Every box (database, AI inference, APIs, hosting) is in Mumbai (`ap-south-1`). The AI model is invoked in-region on-demand, never a cross-border profile. The only thing outside India is the user's browser, and it only ever receives the final answer text — never raw data. No CloudFront (a global service) anywhere.
- **Security** — The database is private (no public access). The agent can't run arbitrary SQL — it can only call 4 read-only tools through an authenticated gateway, and every query is built from a validated whitelist, so SQL injection and writes are impossible. Credentials live in Secrets Manager, never in code or logs.
- **No hallucination** — Every number in an answer comes from a live database query. If the data can't answer (e.g. weather, or a field we don't have), the bot says so instead of guessing.

## If someone asks "why not just ChatGPT on the data?"

Two reasons: (1) **residency** — this keeps all data and inference inside India; (2) **trust** — answers are computed from the actual database via governed read-only tools, not generated from the model's memory, so the figures are auditable and correct.

## Capabilities to highlight (what kinds of questions it handles)

Counts, totals, breakdowns by any dimension (state, bank, discom, gender, category), leaderboards ("top 5 states", "highest-disbursing bank"), fraud signals (duplicate bank accounts, beneficiaries who took the subsidy more than once), and turnaround-time metrics — all in plain English.

The architecture flow, end to end:

```
Browser → Amplify → API Gateway (REST) → Agent_Lambda → Bedrock / Memory / Gateway → Tool_Lambda → Aurora
```

## Request workflow (step by step)

1. Ask (browser). The user types a question in the Amplify-hosted page; JavaScript `fetch()`-POSTs `{question, sessionId}` to `POST /chat`. `sessionId` keeps follow-ups in one conversation.
2. Front door (API Gateway REST). `POST /chat` is an `AWS_PROXY` integration straight to Agent_Lambda (`OPTIONS /chat` handles CORS preflight).
3. Agent_Lambda (`src/agent/handler.py`). Detects the HTTP shape and runs one turn. `residency_guard(MODEL_ID)` (cold start) hard-fails on any `apac.*`/`global.*` model, guaranteeing in-region inference.
4. Recall (AgentCore Memory). `memory.load()` folds the prior turns for this session into the system prompt, so follow-ups have context.
5. Reason (Bedrock Claude 3 Haiku). A Strands agent + the system prompt (`prompt.py`) let Claude pick which `query_<table>` tool to call and with what arguments. Claude never sees the database — it only chooses a tool and parameters.
6. Tool call (MCP + SigV4). The call is SigV4-signed for `bedrock-agentcore` and sent to the AgentCore Gateway, which uses `AWS_IAM` auth — unsigned callers are rejected.
7. Gateway → Tool_Lambda. The Gateway exposes exactly 4 read-only tools and forwards the request to Tool_Lambda (the only component inside the private VPC), with `table` pinned per tool.
8. Safe execution (`src/tool/`). `validate.py` rejects anything off the whitelist (or raw SQL) and runs nothing; `query_builder.py` builds a `SELECT`-only, fully parameterized statement; `handler.py` reads the read-only DB secret via a VPC endpoint (no NAT) and queries Aurora as a read-only user. Credentials are redacted from logs.
9. Query Aurora. The private Aurora PostgreSQL runs the parameterized `SELECT` and returns rows.
10. Compose. Rows flow back up; Claude phrases a plain-English answer from the real numbers; `_clean_answer()` strips any query jargon.
11. Remember + reply. `memory.save()` persists the turn and the answer is returned in the HTTP response; the browser renders it.

Two guarantees: every number comes from a live query (no hallucinated figures — if the data can't answer, the bot says so), and no data leaves India (every hop is in `ap-south-1`; only the browser is outside, and it receives just the answer text). See [ARCHITECTURE.md](ARCHITECTURE.md) for the full version.

## Production considerations (deploying against an existing database)

The one-click deploy is self-contained: it creates a **new VPC** and its **own Aurora PostgreSQL** cluster, loads the synthetic sample data, and points the Tool_Lambda at it. That is ideal for a demo. When connecting to a customer's **existing** PM Surya Ghar database, two changes are required:

1. **Deploy into the same VPC as the database.** The Tool_Lambda must run in a VPC (and subnets/security groups) that can reach the existing Aurora cluster on port 5432. By default this stack provisions a brand-new VPC (`10.20.0.0/16`), which cannot reach a DB in a different VPC. For a real deployment, adapt `infra/provision_network.py` (and the ids it writes to `network_ids.json`) to **reuse the customer's existing VPC, private subnets, and a security group allowed into the DB's security group** — instead of creating new ones. Also ensure a Secrets Manager VPC endpoint (or NAT-free path) exists in that VPC so the Tool_Lambda can read the DB credential in-region.

2. **Point the tool at a read replica, not the primary writer.** The chatbot only ever runs read-only `SELECT` queries, but analytical/aggregation queries can be heavy. To protect the production workload, point the Tool_Lambda at an **Aurora reader endpoint (read replica)** rather than the cluster **writer endpoint** — set `db_endpoint` in `network_ids.json` (consumed by `deploy_tool.py`) to the reader endpoint. This keeps all chatbot load off the primary write instance. The read-only DB user and `set_session(readonly=True)` already enforce read-only access; using the reader endpoint additionally isolates the *performance* impact.

In short: for production, skip the "create new VPC + new Aurora + load sample data" steps and instead wire the Tool_Lambda into the existing VPC and the reader endpoint of the existing cluster. The rest of the stack (Agent_Lambda, Bedrock, AgentCore Gateway/Memory, API, UI) is unchanged.

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

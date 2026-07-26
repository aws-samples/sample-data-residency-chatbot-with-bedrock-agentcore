"""One-command deploy orchestrator for the MNRE AgentCore chatbot (ap-south-1).

Runs the full stack, in dependency order, into the CURRENT AWS account. Each
step is idempotent, so re-running after a failure is safe. Every step writes its
resource ids into infra/network_ids.json (per-account runtime state).

    uv run python deploy.py                # prompts for the target account, then deploys
    uv run python deploy.py --account 123456789012   # non-interactive account
    uv run python deploy.py --with-websocket   # also provision the prod WSS API
    uv run python deploy.py --from deploy_tool # resume from a given step

On start it asks which AWS account to deploy into and verifies your active
credentials resolve to that account (aborting on mismatch). The region is always
ap-south-1 (Mumbai) — fixed for data residency.

Prerequisites (checked by preflight):
  - AWS credentials for the target account (ap-south-1).
  - finch installed + VM started (for the Agent_Lambda container image).
  - Bedrock model access ENABLED for the configured model (Claude 3 Haiku).
  - uv installed (you are already running under it).

See DEPLOYMENT.md for the full guide.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "infra"))
from config import ACCOUNT, DATA_BUCKET, MODEL_ID, REGION  # noqa: E402

import boto3  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# Every step below runs as a fixed, fully literal command line ("uv run python"
# + an in-repo script path). shell is never used and no user-controlled input
# ever reaches a command line. One literal runner per step keeps the argv
# statically auditable end to end.


def _s(name: str, desc: str, runner) -> tuple[str, str, object]:
    return (name, desc, runner)


STEPS: list[tuple[str, str, object]] = [
    _s("make_sample_data", "generate synthetic sample CSVs",
       lambda: subprocess.run(["uv", "run", "python", "tools/make_sample_data.py"], check=True, cwd=HERE)),
    _s("provision_network", "fresh VPC, subnets, SGs, VPC endpoints",
       lambda: subprocess.run(["uv", "run", "python", "infra/provision_network.py"], check=True, cwd=HERE)),
    _s("provision_aurora", "Aurora PostgreSQL Serverless v2 (private)",
       lambda: subprocess.run(["uv", "run", "python", "infra/provision_aurora.py"], check=True, cwd=HERE)),
    _s("wait_aurora", "wait for Aurora + capture endpoint/secret",
       lambda: subprocess.run(["uv", "run", "python", "infra/wait_aurora.py"], check=True, cwd=HERE)),
    _s("provision_iam_ddb", "IAM roles + connections table",
       lambda: subprocess.run(["uv", "run", "python", "infra/provision_iam_ddb.py"], check=True, cwd=HERE)),
    _s("deploy_bootstrap", "create tables + read-only DB user",
       lambda: subprocess.run(["uv", "run", "python", "infra/deploy_bootstrap.py"], check=True, cwd=HERE)),
    _s("deploy_loader", "upload + bulk-load sample data",
       lambda: subprocess.run(["uv", "run", "python", "infra/deploy_loader.py"], check=True, cwd=HERE)),
    _s("deploy_tool", "Tool_Lambda (in-VPC, read-only)",
       lambda: subprocess.run(["uv", "run", "python", "infra/deploy_tool.py"], check=True, cwd=HERE)),
    _s("agentcore_setup", "AgentCore Gateway + Memory",
       lambda: subprocess.run(["uv", "run", "python", "infra/agentcore_setup.py"], check=True, cwd=HERE)),
    _s("deploy_agent", "Agent_Lambda container image (out-of-VPC)",
       lambda: subprocess.run(["uv", "run", "python", "infra/deploy_agent.py"], check=True, cwd=HERE)),
    _s("provision_rest_api", "REST API POST /chat (demo transport)",
       lambda: subprocess.run(["uv", "run", "python", "infra/provision_rest_api.py"], check=True, cwd=HERE)),
    _s("deploy_amplify", "host UI on Amplify (injects REST URL)",
       lambda: subprocess.run(["uv", "run", "python", "infra/deploy_amplify.py"], check=True, cwd=HERE)),
]

# Optional production WebSocket transport (inserted before deploy_amplify when requested).
WS_STEP = _s("provision_websocket", "WebSocket API (production transport)",
             lambda: subprocess.run(["uv", "run", "python", "infra/provision_websocket.py"], check=True, cwd=HERE))


def _run_step(name: str, desc: str, runner) -> None:
    print(f"\n{'=' * 72}\n>>> {name}: {desc}\n{'=' * 72}")
    runner()


def confirm_target_account(supplied: str | None) -> None:
    """Ask which AWS account to deploy into, then verify the active credentials
    actually resolve to that account. Aborts on mismatch so the stack is never
    deployed to the wrong account. Region is fixed to ap-south-1.

    ``supplied`` (from --account) skips the interactive prompt (for automation).
    """
    print(f"\nDeployment region is FIXED to {REGION} (Mumbai) for data residency.")
    target = (supplied or "").strip()
    if not target:
        try:
            target = input("Enter the target AWS account id to deploy into: ").strip()
        except EOFError:
            raise SystemExit(
                "No account id provided. Re-run interactively or pass --account <id>."
            )
    if not (target.isdigit() and len(target) == 12):
        raise SystemExit(f"'{target}' is not a valid 12-digit AWS account id.")

    if target != ACCOUNT:
        raise SystemExit(
            f"ACCOUNT MISMATCH: you asked to deploy into {target}, but your active "
            f"AWS credentials belong to {ACCOUNT}. Switch credentials/profile to the "
            f"target account (e.g. set AWS_PROFILE) and re-run."
        )
    print(f"Confirmed: deploying into account {target} in {REGION}.")


def preflight() -> None:
    print(f"{'=' * 72}\n>>> preflight\n{'=' * 72}")
    print(f"Region        : {REGION}")
    print(f"Account        : {ACCOUNT}")
    print(f"Model          : {MODEL_ID}")
    print(f"Data bucket    : {DATA_BUCKET}")

    ident = boto3.client("sts", region_name=REGION).get_caller_identity()
    print(f"Caller identity: {ident['Arn']}")

    # Container tool for the Agent_Lambda image build. Local default is finch;
    # CodeBuild sets CONTAINER_TOOL=docker (deploy_agent.py honours the same var).
    container_tool = os.environ.get("CONTAINER_TOOL", "finch")
    if shutil.which(container_tool) is None:
        raise SystemExit(
            f"PREFLIGHT FAIL: container tool '{container_tool}' not found on PATH. "
            f"Install it (or set CONTAINER_TOOL). Locally: install finch and run "
            f"'finch vm start'. In CodeBuild, Docker is preinstalled."
        )
    print(f"container tool : {container_tool} found")

    # Bedrock model access — the single most common first-deploy blocker.
    _check_bedrock_model_access()
    print("\nPreflight OK.\n")


def _check_bedrock_model_access() -> None:
    """Confirm the model exists in-region and access is granted (best effort)."""
    br = boto3.client("bedrock", region_name=REGION)
    try:
        models = br.list_foundation_models(byProvider="anthropic")["modelSummaries"]
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not list foundation models: {exc}")
        return
    ids = {m["modelId"] for m in models}
    if MODEL_ID not in ids:
        raise SystemExit(
            f"PREFLIGHT FAIL: model {MODEL_ID} is not available in {REGION}. "
            f"Pick a bare ON_DEMAND modelId available in-region."
        )
    print(f"Bedrock model  : {MODEL_ID} present in-region")
    print(
        "  NOTE: ensure model ACCESS is enabled in the Bedrock console "
        "(Model access → Anthropic Claude 3 Haiku). Without it the first "
        "invoke returns AccessDeniedException."
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Deploy the MNRE chatbot stack")
    ap.add_argument("--with-websocket", action="store_true",
                    help="also provision the production WebSocket API")
    ap.add_argument("--from", dest="from_step", default=None,
                    help="resume from this step name (see STEPS)")
    ap.add_argument("--account", default=None,
                    help="target AWS account id (skips the interactive prompt)")
    ap.add_argument("--skip-preflight", action="store_true")
    args = ap.parse_args()

    # Always confirm the destination account before doing anything.
    confirm_target_account(args.account)

    steps = list(STEPS)
    if args.with_websocket:
        # Insert WS provisioning right before the UI deploy (agent must exist).
        idx = next(i for i, s in enumerate(steps) if s[0] == "deploy_amplify")
        steps.insert(idx, WS_STEP)

    if args.from_step:
        names = [s[0] for s in steps]
        if args.from_step not in names:
            raise SystemExit(f"unknown step '{args.from_step}'. Steps: {names}")
        steps = steps[names.index(args.from_step):]

    if not args.skip_preflight:
        preflight()

    for name, desc, runner in steps:
        _run_step(name, desc, runner)

    print(f"\n{'=' * 72}\nDEPLOY COMPLETE\n{'=' * 72}")
    print("Open infra/network_ids.json for all resource ids.")
    print("The 'amplify_url' is the live dashboard; 'rest_api_url' is the chat endpoint.")


if __name__ == "__main__":
    main()

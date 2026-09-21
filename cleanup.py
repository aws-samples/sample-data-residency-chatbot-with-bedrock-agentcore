"""Clean up / destroy ALL resources deployed by deploy.py for this project
(ap-south-1).

Deletes the resources created by deploy.py, in reverse dependency order, reading
their ids from infra/network_ids.json. Every deletion is guarded (missing/already
-deleted resources are skipped), so this is safe to re-run.

    uv run python cleanup.py                     # prompts for target account + confirmation
    uv run python cleanup.py --account 123456789012 --yes
    uv run python cleanup.py --yes --delete-bucket

On start it asks which AWS account to clean and verifies your active credentials
resolve to that account (aborting on mismatch), so you never destroy the wrong
account's stack. The region is always ap-south-1 (Mumbai).

This is DESTRUCTIVE. It removes the WAF web ACL (Amplify firewall), the VPC,
Aurora, Lambdas, ECR repo, Gateway, Memory, APIs, Amplify app, IAM roles,
DynamoDB table, and read-only DB secret for this project. CloudWatch log groups
are kept (cheap; useful for post-mortem). The demo-data S3 bucket is kept
unless --delete-bucket is passed.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "infra"))
from config import (  # noqa: E402
    ACCOUNT, DATA_BUCKET, IDS_PATH, PROJECT, REGION, TAG_KEY, TAG_VALUE, load_ids,
)

ids = load_ids()


def _try(label: str, fn) -> None:
    try:
        fn()
        print(f"[del] {label}")
    except ClientError as e:
        print(f"[skip] {label}: {e.response['Error'].get('Code', 'error')}")
    except Exception as e:  # noqa: BLE001
        print(f"[skip] {label}: {type(e).__name__}: {e}")


def confirm_target_account(supplied: str | None) -> None:
    """Ask which account to clean, then verify credentials resolve to it."""
    print(f"\nCleanup region is FIXED to {REGION} (Mumbai).")
    target = (supplied or "").strip()
    if not target:
        try:
            target = input("Enter the target AWS account id to clean up: ").strip()
        except EOFError:
            raise SystemExit(
                "No account id provided. Re-run interactively or pass --account <id>."
            )
    if not (target.isdigit() and len(target) == 12):
        raise SystemExit(f"'{target}' is not a valid 12-digit AWS account id.")
    if target != ACCOUNT:
        raise SystemExit(
            f"ACCOUNT MISMATCH: you asked to clean up {target}, but your active AWS "
            f"credentials belong to {ACCOUNT}. Switch credentials/profile to the "
            f"target account (e.g. set AWS_PROFILE) and re-run."
        )
    print(f"Confirmed: cleaning up account {target} in {REGION}.")


def cleanup_waf() -> None:
    """Disassociate + delete the Amplify firewall web ACL.

    The web ACL lives in us-east-1 with CLOUDFRONT scope (required by Amplify
    Hosting's firewall integration — regional ACLs are not compatible). It must
    be disassociated from the app before it can be deleted, so this runs BEFORE
    cleanup_amplify().
    """
    waf_region = ids.get("waf_region", "us-east-1")
    waf_scope = ids.get("waf_scope", "CLOUDFRONT")
    acl_name = ids.get("waf_web_acl_name", f"{PROJECT}-webacl")
    wafv2 = boto3.client("wafv2", region_name=waf_region)

    # State-less fallback: discover the app by name when ids are missing.
    app_id = ids.get("amplify_app_id") or _find_amplify_app_id()
    if app_id:
        app_arn = f"arn:aws:amplify:{REGION}:{ACCOUNT}:apps/{app_id}"
        _try(f"waf disassociate from app {app_id}",
             lambda: wafv2.disassociate_web_acl(ResourceArn=app_arn))
        # Wait for the disassociation to settle so delete_web_acl can succeed.
        amp = boto3.client("amplify", region_name=REGION)
        for _ in range(30):
            try:
                cfg = amp.get_app(appId=app_id)["app"].get("wafConfiguration") or {}
                if not cfg.get("webAclArn") or cfg.get("wafStatus") not in (
                        "DISASSOCIATING", "ASSOCIATION_SUCCESS"):
                    break
            except Exception:  # noqa: BLE001
                break
            time.sleep(10)

    # Find the ACL by name (survives a lost network_ids.json) and delete it.
    acl = None
    try:
        marker = None
        while acl is None:
            kwargs = {"Scope": waf_scope, "Limit": 100}
            if marker:
                kwargs["NextMarker"] = marker
            resp = wafv2.list_web_acls(**kwargs)
            for candidate in resp.get("WebACLs", []):
                if candidate.get("Name") == acl_name:
                    acl = candidate
                    break
            marker = resp.get("NextMarker")
            if not marker or not resp.get("WebACLs"):
                break
    except Exception as e:  # noqa: BLE001
        print(f"[skip] waf list: {type(e).__name__}: {e}")
        return
    if not acl:
        print(f"[skip] waf web ACL {acl_name}: not found")
        return

    # Deletion needs a fresh LockToken; retry while the association drains.
    for attempt in range(6):
        try:
            token = wafv2.get_web_acl(
                Name=acl_name, Scope=waf_scope, Id=acl["Id"])["LockToken"]
            wafv2.delete_web_acl(
                Name=acl_name, Scope=waf_scope, Id=acl["Id"], LockToken=token)
            print(f"[del] waf web ACL {acl_name}")
            return
        except ClientError as e:
            code = e.response["Error"].get("Code", "")
            if code in ("WAFAssociatedItemException",
                        "WAFOptimisticLockException") and attempt < 5:
                time.sleep(15)
                continue
            print(f"[skip] waf web ACL {acl_name}: {code}")
            return
        except Exception as e:  # noqa: BLE001
            print(f"[skip] waf web ACL {acl_name}: {type(e).__name__}: {e}")
            return


def _find_amplify_app_id() -> str | None:
    """Locate the app by its well-known name when local state is missing."""
    amp = boto3.client("amplify", region_name=REGION)
    token = None
    try:
        while True:
            kwargs = {"maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            resp = amp.list_apps(**kwargs)
            for app in resp.get("apps", []):
                if app.get("name") == f"{PROJECT}-ui":
                    return app["appId"]
            token = resp.get("nextToken")
            if not token:
                return None
    except Exception:  # noqa: BLE001
        return None


def cleanup_amplify() -> None:
    # STATE-LESS FALLBACK: the one-click cleanup runs from a fresh bundle
    # without network_ids.json, so discover by name when the id is absent.
    app_id = ids.get("amplify_app_id") or _find_amplify_app_id()
    if not app_id:
        print("[skip] amplify app: not found")
        return
    amp = boto3.client("amplify", region_name=REGION)
    _try(f"amplify app {app_id}", lambda: amp.delete_app(appId=app_id))


def cleanup_apis() -> None:
    apigw = boto3.client("apigateway", region_name=REGION)
    rest_id = ids.get("rest_api_id")
    if not rest_id:
        # Discover by well-known name (stateless one-click cleanup).
        try:
            for item in apigw.get_rest_apis(limit=500).get("items", []):
                if item.get("name") == f"{PROJECT}-rest":
                    rest_id = item["id"]
                    break
        except Exception as e:  # noqa: BLE001
            print(f"[skip] REST API discovery: {type(e).__name__}: {e}")
    if rest_id:
        _try(f"REST API {rest_id}",
             lambda: apigw.delete_rest_api(restApiId=rest_id))
    apigw2 = boto3.client("apigatewayv2", region_name=REGION)
    ws_id = ids.get("ws_api_id")
    if not ws_id:
        try:
            for api in apigw2.get_apis(MaxResults="100").get("Items", []):
                if api.get("Name") == f"{PROJECT}-ws":
                    ws_id = api["ApiId"]
                    break
        except Exception as e:  # noqa: BLE001
            print(f"[skip] WebSocket API discovery: {type(e).__name__}: {e}")
    if ws_id:
        _try(f"WebSocket API {ws_id}",
             lambda: apigw2.delete_api(ApiId=ws_id))


def cleanup_lambdas() -> None:
    lam = boto3.client("lambda", region_name=REGION)
    for fn in (f"{PROJECT}-agent", f"{PROJECT}-tool", f"{PROJECT}-db-bootstrap",
               f"{PROJECT}-db-loader", f"{PROJECT}-ws-connect", f"{PROJECT}-ws-disconnect"):
        _try(f"lambda {fn}", lambda fn=fn: lam.delete_function(FunctionName=fn))


def cleanup_ecr() -> None:
    ecr = boto3.client("ecr", region_name=REGION)
    _try(f"ecr repo {PROJECT}-agent",
         lambda: ecr.delete_repository(repositoryName=f"{PROJECT}-agent", force=True))


def _find_gateway_id(acc) -> str | None:
    """Locate the project gateway by its well-known name (stateless path)."""
    token = None
    try:
        while True:
            kwargs = {"maxResults": 50}
            if token:
                kwargs["nextToken"] = token
            resp = acc.list_gateways(**kwargs)
            for g in resp.get("items", []):
                if g.get("name") == f"{PROJECT}-gateway":
                    return g["gatewayId"]
            token = resp.get("nextToken")
            if not token:
                return None
    except Exception:  # noqa: BLE001
        return None


def _find_memory_id(acc) -> str | None:
    """Locate the project memory by name (summaries omit name; get each)."""
    token = None
    try:
        while True:
            kwargs = {"maxResults": 50}
            if token:
                kwargs["nextToken"] = token
            resp = acc.list_memories(**kwargs)
            for m in resp.get("memories", []):
                detail = acc.get_memory(memoryId=m["id"])["memory"]
                if detail.get("name") == "residency_chatbot_memory":
                    return m["id"]
            token = resp.get("nextToken")
            if not token:
                return None
    except Exception:  # noqa: BLE001
        return None


def cleanup_agentcore() -> None:
    acc = boto3.client("bedrock-agentcore-control", region_name=REGION)
    # STATE-LESS FALLBACK: discover by the well-known names when ids are
    # missing (the one-click cleanup runs without network_ids.json).
    gid = ids.get("gateway_id") or _find_gateway_id(acc)
    if gid:
        tids = [ids["gateway_target_id"]] if ids.get("gateway_target_id") else []
        if not tids:
            try:
                tids = [t["targetId"] for t in
                        acc.list_gateway_targets(gatewayIdentifier=gid).get("items", [])]
            except Exception:  # noqa: BLE001
                tids = []
        for tid in tids:
            _try(f"gateway target {tid}",
                 lambda tid=tid: acc.delete_gateway_target(gatewayIdentifier=gid, targetId=tid))
        if tids:
            # Target deletion is asynchronous; DeleteGateway is rejected with a
            # ValidationException while any target still exists. Poll until
            # the target list drains (a fixed sleep raced and leaked gateways).
            for _ in range(30):
                try:
                    remaining = acc.list_gateway_targets(
                        gatewayIdentifier=gid).get("items", [])
                except Exception:  # noqa: BLE001
                    break
                if not remaining:
                    break
                time.sleep(5)
        _try(f"gateway {gid}", lambda: acc.delete_gateway(gatewayIdentifier=gid))
    else:
        print("[skip] gateway: not found")
    mid = ids.get("memory_id") or _find_memory_id(acc)
    if mid:
        _try(f"memory {mid}", lambda: acc.delete_memory(memoryId=mid))
    else:
        print("[skip] memory: not found")


def cleanup_aurora() -> None:
    rds = boto3.client("rds", region_name=REGION)
    inst = f"{PROJECT}-aurora-1"
    clus = f"{PROJECT}-aurora"
    _try(f"db instance {inst}",
         lambda: rds.delete_db_instance(DBInstanceIdentifier=inst, SkipFinalSnapshot=True))
    # wait for the instance to go before the cluster can be deleted
    try:
        rds.get_waiter("db_instance_deleted").wait(
            DBInstanceIdentifier=inst, WaiterConfig={"Delay": 20, "MaxAttempts": 60})
    except Exception as e:  # noqa: BLE001
        print(f"[info] instance waiter ended: {type(e).__name__} (ok if already gone)")
    _try(f"db cluster {clus}",
         lambda: rds.delete_db_cluster(DBClusterIdentifier=clus, SkipFinalSnapshot=True))
    try:
        rds.get_waiter("db_cluster_deleted").wait(
            DBClusterIdentifier=clus, WaiterConfig={"Delay": 20, "MaxAttempts": 60})
    except Exception as e:  # noqa: BLE001
        print(f"[info] cluster waiter ended: {type(e).__name__} (ok if already gone)")
    _try(f"db subnet group {PROJECT}-db-subnet-group",
         lambda: rds.delete_db_subnet_group(DBSubnetGroupName=f"{PROJECT}-db-subnet-group"))


def cleanup_ddb() -> None:
    ddb = boto3.client("dynamodb", region_name=REGION)
    _try(f"dynamodb {PROJECT}-connections",
         lambda: ddb.delete_table(TableName=f"{PROJECT}-connections"))


def cleanup_secret() -> None:
    sm = boto3.client("secretsmanager", region_name=REGION)
    _try(f"secret {PROJECT}-db-readonly",
         lambda: sm.delete_secret(SecretId=f"{PROJECT}-db-readonly",
                                  ForceDeleteWithoutRecovery=True))


def cleanup_iam() -> None:
    iam = boto3.client("iam", region_name=REGION)
    roles = [f"{PROJECT}-tool-lambda-role", f"{PROJECT}-agent-lambda-role",
             f"{PROJECT}-gateway-role", f"{PROJECT}-ws-helper-role"]
    for role in roles:
        try:
            for p in iam.list_role_policies(RoleName=role).get("PolicyNames", []):
                iam.delete_role_policy(RoleName=role, PolicyName=p)
            for ap in iam.list_attached_role_policies(RoleName=role).get("AttachedPolicies", []):
                iam.detach_role_policy(RoleName=role, PolicyArn=ap["PolicyArn"])
            iam.delete_role(RoleName=role)
            print(f"[del] iam role {role}")
        except ClientError as e:
            print(f"[skip] iam role {role}: {e.response['Error'].get('Code')}")


def _sweep_lambda_enis(ec2, vpc_id: str) -> None:
    """Delete orphaned ENIs left by THIS PROJECT's in-VPC Lambdas.

    Lambda-managed interfaces linger for many minutes after the functions are
    deleted and block subnet deletion. Only interfaces whose description names
    a residency-chatbot-* function are touched (Lambda names them
    "AWS Lambda VPC ENI-<function>-<uuid>"), so a shared/customer VPC is never
    swept of unrelated interfaces. Best-effort: any permission problem degrades
    to the subnet-delete retries below instead of aborting the teardown.
    """
    # Lambda releases its Hyperplane ENIs asynchronously after the function is
    # deleted; observed live at 10-20 minutes. Poll up to ~25 min (150 x 10s).
    for _ in range(150):
        try:
            enis = ec2.describe_network_interfaces(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "description", "Values": [f"AWS Lambda VPC ENI-{PROJECT}-*"]},
            ])["NetworkInterfaces"]
        except ClientError as e:
            print(f"[skip] eni sweep: {e.response['Error'].get('Code', 'error')}")
            return
        if not enis:
            return
        available = [e for e in enis if e.get("Status") == "available"]
        for eni in available:
            _try(f"eni {eni['NetworkInterfaceId']}",
                 lambda eid=eni["NetworkInterfaceId"]:
                 ec2.delete_network_interface(NetworkInterfaceId=eid))
        # In-use interfaces detach asynchronously; wait and re-check.
        time.sleep(2 if available else 10)


def cleanup_network() -> None:
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id = ids.get("vpc_id")
    if not vpc_id:
        # STATE-LESS FALLBACK: discover the project VPC (the one-click cleanup
        # runs from a fresh bundle without network_ids.json). Require BOTH
        # tags the deploy sets, and refuse to guess if more than one matches:
        # the deletions below are total for whichever VPC is chosen.
        try:
            vpcs = ec2.describe_vpcs(Filters=[
                {"Name": "tag:Name", "Values": [f"{PROJECT}-vpc"]},
                {"Name": f"tag:{TAG_KEY}", "Values": [TAG_VALUE]},
            ])["Vpcs"]
        except Exception as e:  # noqa: BLE001
            print(f"[skip] vpc discovery: {type(e).__name__}: {e}")
            return
        if len(vpcs) > 1:
            print(f"[skip] vpc: {len(vpcs)} VPCs match {PROJECT}-vpc — refusing to "
                  f"guess; set vpc_id in network_ids.json and re-run")
            return
        vpc_id = vpcs[0]["VpcId"] if vpcs else None
    if not vpc_id:
        print("[skip] vpc: not found")
        return
    # VPC endpoints
    eps = ec2.describe_vpc_endpoints(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["VpcEndpoints"]
    if eps:
        _try("vpc endpoints",
             lambda: ec2.delete_vpc_endpoints(VpcEndpointIds=[e["VpcEndpointId"] for e in eps]))
        time.sleep(5)
    _sweep_lambda_enis(ec2, vpc_id)
    # subnets (from state, else discovered from the VPC)
    subnet_ids = ids.get("private_subnet_ids") or [
        s["SubnetId"] for s in ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]]
    for sid in subnet_ids:
        for _ in range(6):
            try:
                ec2.delete_subnet(SubnetId=sid)
                print(f"[del] subnet {sid}")
                break
            except ClientError as e:
                if "DependencyViolation" in str(e):
                    time.sleep(10)
                    continue
                print(f"[skip] subnet {sid}: {e.response['Error'].get('Code')}")
                break
    # route tables (from state, else discovered; skip the main table)
    rts = [ids["private_route_table_id"]] if ids.get("private_route_table_id") else [
        rt["RouteTableId"] for rt in ec2.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]
        if not any(a.get("Main") for a in rt.get("Associations", []))]
    for rt in rts:
        try:
            assoc = ec2.describe_route_tables(RouteTableIds=[rt])["RouteTables"][0].get("Associations", [])
            for a in assoc:
                if a.get("RouteTableAssociationId"):
                    ec2.disassociate_route_table(AssociationId=a["RouteTableAssociationId"])
        except Exception as e:  # noqa: BLE001
            print(f"[skip] route table {rt} disassociate: {type(e).__name__}")
        _try(f"route table {rt}", lambda rt=rt: ec2.delete_route_table(RouteTableId=rt))
    # security groups (from state, else discovered; retry — dependencies clear
    # asynchronously)
    sgs = [ids[k] for k in ("aurora_sg", "lambda_sg", "vpce_sg") if ids.get(k)] or [
        g["GroupId"] for g in ec2.describe_security_groups(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]
        if g.get("GroupName") != "default"]
    # A Lambda ENI that is mid-detach can still pin the lambda SG for a few
    # minutes after the ENI sweep above sees none left; retry patiently
    # (observed live: a 60s window left one SG and the VPC behind).
    for sg in sgs:
        for _ in range(30):
            try:
                ec2.delete_security_group(GroupId=sg)
                print(f"[del] sg {sg}")
                break
            except ClientError as e:
                if "DependencyViolation" in str(e):
                    time.sleep(10)
                    continue
                print(f"[skip] sg {sg}: {e.response['Error'].get('Code')}")
                break
    for _ in range(12):
        try:
            ec2.delete_vpc(VpcId=vpc_id)
            print(f"[del] vpc {vpc_id}")
            break
        except ClientError as e:
            if "DependencyViolation" in str(e):
                time.sleep(10)
                continue
            print(f"[skip] vpc {vpc_id}: {e.response['Error'].get('Code')}")
            break


def cleanup_bucket() -> None:
    s3 = boto3.resource("s3", region_name=REGION)
    bucket = s3.Bucket(DATA_BUCKET)
    _try(f"empty bucket {DATA_BUCKET}", lambda: bucket.objects.all().delete())
    _try(f"delete bucket {DATA_BUCKET}", lambda: bucket.delete())


def _clear_ids_state() -> None:
    """Remove the local runtime-state file so a later deploy starts fresh."""
    try:
        if os.path.isfile(IDS_PATH):
            os.remove(IDS_PATH)
            print(f"[del] local state {os.path.basename(IDS_PATH)}")
    except OSError as e:
        print(f"[skip] local state: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Destroy the chatbot stack")
    ap.add_argument("--account", default=None,
                    help="target AWS account id (skips the interactive prompt)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--delete-bucket", action="store_true",
                    help="also delete the demo-data S3 bucket")
    ap.add_argument("--keep-state", action="store_true",
                    help="do not delete the local network_ids.json after cleanup")
    args = ap.parse_args()

    confirm_target_account(args.account)

    print(f"\nRegion {REGION}  Project {PROJECT}")
    print("This DELETES the chatbot stack (WAF web ACL, VPC, Aurora, Lambdas, "
          "ECR, Gateway, Memory, APIs, Amplify, IAM, DynamoDB, DB secret).")
    if not args.yes:
        if input("Type 'delete' to proceed: ").strip().lower() != "delete":
            print("aborted.")
            return

    cleanup_waf()
    cleanup_amplify()
    cleanup_apis()
    cleanup_agentcore()
    cleanup_lambdas()
    cleanup_ecr()
    cleanup_aurora()
    cleanup_ddb()
    cleanup_secret()
    cleanup_network()
    cleanup_iam()
    if args.delete_bucket:
        cleanup_bucket()
    if not args.keep_state:
        _clear_ids_state()

    print("\nCleanup complete (best-effort). Review the console for any lingering resources.")


if __name__ == "__main__":
    main()

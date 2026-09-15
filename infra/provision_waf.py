"""Protect the Amplify-hosted Browser_UI with an AWS WAF web ACL (Firewall).

WHY this exists
---------------
The Amplify app serves the public dashboard. Without a firewall, the URL
accepts any HTTP request from anywhere. This step attaches an AWS WAF web ACL
to the Amplify app (Amplify Hosting "Firewall" integration) so every request
for the UI is inspected before it is served:

  - a rate-based rule throttles request floods per client IP (L7 DDoS),
  - AWSManagedRulesAmazonIpReputationList blocks known-bad source IPs,
  - AWSManagedRulesCommonRuleSet blocks common exploits (XSS, LFI, ...),
  - AWSManagedRulesKnownBadInputsRuleSet blocks known malicious payloads.

SCOPE — read this before changing anything
------------------------------------------
Amplify Hosting's firewall integration REQUIRES the web ACL to be created in
the GLOBAL (CloudFront) scope, which lives in us-east-1. Regional web ACLs are
NOT compatible with Amplify — association is rejected. This is an AWS
constraint, not a choice:
https://docs.aws.amazon.com/amplify/latest/userguide/amplify-waf-configuration.html

RESIDENCY note: the web ACL is firewall CONFIGURATION (rules + counters), a
control-plane resource. It holds no program data. It protects only the static
UI shell that Amplify serves; all PROGRAM DATA still flows exclusively between
the browser and the regional ap-south-1 API endpoint, exactly as before.

Flow (idempotent, safe to re-run):
  1. Find-or-create the web ACL (name: residency-chatbot-webacl, us-east-1,
     CLOUDFRONT scope, default action Allow, 4 rules above).
  2. Associate it with the Amplify app (wafv2 AssociateWebACL with the app
     ARN). A freshly created ACL can briefly raise WAFUnavailableEntityException
     while it propagates — retried.
  3. Poll the app's wafConfiguration until ASSOCIATION_SUCCESS (can take a few
     minutes; the app stays available throughout).
  4. Persist waf_web_acl_* ids into network_ids.json and best-effort verify the
     UI still serves.

Run:  uv run python infra/provision_waf.py   (after deploy_amplify.py)
"""
from __future__ import annotations

import os
import sys
import time

import httpx

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import ACCOUNT, PROJECT, REGION, TAGS, load_ids, save_ids  # noqa: E402

# Amplify's firewall integration only accepts GLOBAL (CloudFront-scope) web
# ACLs, and those can only be created in us-east-1. Do NOT change to REGION.
WAF_REGION = "us-east-1"
WAF_SCOPE = "CLOUDFRONT"

WEB_ACL_NAME = f"{PROJECT}-webacl"
RATE_LIMIT_PER_5_MIN = 2000          # per client IP, trailing 5-minute window

ASSOCIATE_RETRIES = 20               # fresh-ACL propagation can take minutes
ASSOCIATE_RETRY_INTERVAL = 15        # seconds
POLL_TIMEOUT = 900                   # seconds (~15 min) for ASSOCIATION_SUCCESS
POLL_INTERVAL = 15

wafv2 = boto3.client("wafv2", region_name=WAF_REGION)
amplify = boto3.client("amplify", region_name=REGION)


def _managed_rule(priority: int, group_name: str) -> dict:
    """An AWS-managed rule group entry (vendor rules, count-free, no override)."""
    return {
        "Name": group_name,
        "Priority": priority,
        "Statement": {
            "ManagedRuleGroupStatement": {
                "VendorName": "AWS",
                "Name": group_name,
            }
        },
        "OverrideAction": {"None": {}},
        "VisibilityConfig": {
            "SampledRequestsEnabled": True,
            "CloudWatchMetricsEnabled": True,
            "MetricName": group_name,
        },
    }


RULES = [
    {
        # First line of defence: throttle request floods per client IP.
        "Name": f"{PROJECT}-rate-limit",
        "Priority": 0,
        "Statement": {
            "RateBasedStatement": {
                "Limit": RATE_LIMIT_PER_5_MIN,
                "AggregateKeyType": "IP",
            }
        },
        "Action": {"Block": {}},
        "VisibilityConfig": {
            "SampledRequestsEnabled": True,
            "CloudWatchMetricsEnabled": True,
            "MetricName": f"{PROJECT}-rate-limit",
        },
    },
    _managed_rule(1, "AWSManagedRulesAmazonIpReputationList"),
    _managed_rule(2, "AWSManagedRulesCommonRuleSet"),
    _managed_rule(3, "AWSManagedRulesKnownBadInputsRuleSet"),
]


def find_web_acl() -> dict | None:
    """Return the {Name, Id, ARN, ...} summary for our web ACL, or None."""
    marker = None
    while True:
        kwargs = {"Scope": WAF_SCOPE, "Limit": 100}
        if marker:
            kwargs["NextMarker"] = marker
        resp = wafv2.list_web_acls(**kwargs)
        for acl in resp.get("WebACLs", []):
            if acl.get("Name") == WEB_ACL_NAME:
                return acl
        marker = resp.get("NextMarker")
        if not marker or not resp.get("WebACLs"):
            return None


def ensure_web_acl() -> dict:
    """Find-or-create the web ACL; returns its summary {Name, Id, ARN}."""
    acl = find_web_acl()
    if acl:
        print(f"[acl] reuse {WEB_ACL_NAME} id={acl['Id']}")
        return acl
    resp = wafv2.create_web_acl(
        Name=WEB_ACL_NAME,
        Scope=WAF_SCOPE,
        DefaultAction={"Allow": {}},
        Description=(
            "Firewall for the residency-chatbot Amplify UI. CloudFront scope "
            "(us-east-1) is REQUIRED by Amplify Hosting; configuration only, "
            "no program data."
        ),
        Rules=RULES,
        VisibilityConfig={
            "SampledRequestsEnabled": True,
            "CloudWatchMetricsEnabled": True,
            "MetricName": WEB_ACL_NAME,
        },
        Tags=TAGS,
    )
    summary = resp["Summary"]
    print(f"[acl] created {WEB_ACL_NAME} id={summary['Id']}")
    return summary


def current_association(app_id: str) -> tuple[str, str]:
    """Return (webAclArn, wafStatus) currently on the app ('' if none)."""
    app = amplify.get_app(appId=app_id)["app"]
    cfg = app.get("wafConfiguration") or {}
    return cfg.get("webAclArn", "") or "", cfg.get("wafStatus", "") or ""


def associate(web_acl_arn: str, app_arn: str) -> None:
    """AssociateWebACL with retries while a fresh ACL propagates."""
    for attempt in range(1, ASSOCIATE_RETRIES + 1):
        try:
            wafv2.associate_web_acl(WebACLArn=web_acl_arn, ResourceArn=app_arn)
            print(f"[assoc] association requested (attempt {attempt})")
            return
        except ClientError as exc:
            code = exc.response["Error"].get("Code", "")
            if code == "WAFUnavailableEntityException" and attempt < ASSOCIATE_RETRIES:
                print(f"[assoc] ACL still propagating ({attempt}/{ASSOCIATE_RETRIES}); "
                      f"retry in {ASSOCIATE_RETRY_INTERVAL}s")
                time.sleep(ASSOCIATE_RETRY_INTERVAL)
                continue
            raise


def poll_association(app_id: str, web_acl_arn: str) -> str:
    """Poll the app's wafConfiguration until the association settles."""
    deadline = time.time() + POLL_TIMEOUT
    last = None
    while time.time() < deadline:
        arn, status = current_association(app_id)
        if status != last:
            print(f"[assoc] wafStatus={status or '(none)'}")
            last = status
        if status == "ASSOCIATION_SUCCESS" and arn == web_acl_arn:
            return status
        if status == "ASSOCIATION_FAILED":
            return status
        time.sleep(POLL_INTERVAL)
    return last or "TIMEOUT"


def verify_ui(url: str) -> None:
    """Best-effort check that the UI still serves through the firewall."""
    if not url.lower().startswith("https://"):
        return
    for attempt in range(1, 4):
        try:
            resp = httpx.get(url, headers={"User-Agent": "chatbot-waf-verify"},
                             timeout=30, follow_redirects=True)
            print(f"[verify] GET {url} -> {resp.status_code}")
            if resp.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001
            print(f"[verify] attempt {attempt}: {type(exc).__name__}: {exc}")
        time.sleep(10)
    print("[verify] WARN: UI did not return 200 via the firewall yet "
          "(association may still be propagating; check the Amplify console).")


def main() -> None:
    print(f"WAF region {WAF_REGION} (scope {WAF_SCOPE} — required by Amplify)  "
          f"App region {REGION}\n")
    ids = load_ids()
    app_id = ids.get("amplify_app_id", "")
    if not app_id:
        raise SystemExit("amplify_app_id not in network_ids.json — "
                         "run infra/deploy_amplify.py first")
    app_arn = f"arn:aws:amplify:{REGION}:{ACCOUNT}:apps/{app_id}"

    acl = ensure_web_acl()
    web_acl_arn = acl["ARN"]

    existing_arn, existing_status = current_association(app_id)
    if existing_arn and existing_arn != web_acl_arn:
        raise SystemExit(
            f"App {app_id} is already associated with a DIFFERENT web ACL "
            f"({existing_arn}, status={existing_status}). Refusing to replace "
            f"it — disassociate it in the Amplify console (Hosting → Firewall) "
            f"and re-run."
        )
    if existing_arn == web_acl_arn and existing_status == "ASSOCIATION_SUCCESS":
        print("[assoc] already associated — nothing to do")
    else:
        # Associate when nothing is attached yet, or re-request after a
        # previous attempt failed (idempotent re-run remediation).
        if not existing_arn or existing_status == "ASSOCIATION_FAILED":
            associate(web_acl_arn, app_arn)
        status = poll_association(app_id, web_acl_arn)
        if status != "ASSOCIATION_SUCCESS":
            raise SystemExit(
                f"web ACL association did not succeed (status={status}). "
                f"Check the Amplify console → app → Hosting → Firewall."
            )

    print(f"\n=== WAF protection ===")
    print(f"  web ACL   : {WEB_ACL_NAME}")
    print(f"  ARN       : {web_acl_arn}")
    print(f"  app       : {app_id} ({app_arn})")
    print(f"  rules     : rate-limit({RATE_LIMIT_PER_5_MIN}/5min/IP), "
          f"IpReputationList, CommonRuleSet, KnownBadInputsRuleSet")

    ids.update({
        "waf_web_acl_name": WEB_ACL_NAME,
        "waf_web_acl_id": acl["Id"],
        "waf_web_acl_arn": web_acl_arn,
        "waf_scope": WAF_SCOPE,
        "waf_region": WAF_REGION,
    })
    save_ids(ids)

    verify_ui(ids.get("amplify_url", ""))
    print("\nDONE. Amplify app is protected by AWS WAF.")


if __name__ == "__main__":
    main()

"""Render the least-privilege IAM policy for an identity running deploy.py.

The one-click launcher (deploy/codebuild-deploy.yaml) already defines a
scoped-down policy for the CodeBuild role that runs deploy.py — every
statement in it was exercised by a live end-to-end deploy and cleanup. Rather
than hand-maintain a second copy that would drift, this script extracts that
policy document and substitutes your account id and the deploy region, so a
human or CI identity running deploy.py (Option B) gets EXACTLY the same
permissions as the launcher, no more.

Usage:
    uv run python deploy/render_deployer_policy.py --account 123456789012 > deployer-policy.json
    # Attach as an INLINE policy on a dedicated deploy role (the document is
    # ~7 KB: over the 6,144-char customer-managed-policy limit, under the
    # 10,240-char role-inline limit). See DEPLOYMENT.md for the full steps.
    aws iam put-role-policy --role-name residency-chatbot-deployer \
        --policy-name residency-chatbot-deploy-permissions \
        --policy-document file://deployer-policy.json

The ReadSourceBundle statement is dropped because it only covers reading the
source bundle from S3 for the CodeBuild launcher and does not apply to a local
deploy. Everything else is kept verbatim.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "codebuild-deploy.yaml")
REGION = "ap-south-1"  # fixed for data residency; matches infra/config.py

# Statements that exist only for the CodeBuild launcher, not for Option B.
LAUNCHER_ONLY_SIDS = {"ReadSourceBundle"}


class _CfnLoader(yaml.SafeLoader):
    """yaml.SafeLoader plus CloudFormation short-form intrinsics.

    Subclassing SafeLoader (rather than Loader/FullLoader) means only plain
    YAML types and the explicitly registered CFN tags below can be
    constructed — no arbitrary Python object instantiation. The input is the
    repo's own template, not untrusted data.
    """


def _sub(loader, node):
    return {"Fn::Sub": loader.construct_scalar(node)}


def _ref(loader, node):
    return {"Ref": loader.construct_scalar(node)}


def _passthrough(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_CfnLoader.add_constructor("!Sub", _sub)
_CfnLoader.add_constructor("!Ref", _ref)
for tag in ("!GetAtt", "!Join", "!Select", "!Split", "!If", "!Equals", "!Not",
            "!And", "!Or", "!FindInMap", "!Base64", "!Cidr", "!ImportValue",
            "!GetAZs", "!Condition"):
    _CfnLoader.add_constructor(tag, _passthrough)


def _load_template(path: str) -> dict:
    """Parse the CFN template with the SafeLoader-derived loader above."""
    with open(path) as f:
        # B506: Bandit flags any non-safe_load call, but _CfnLoader IS a
        # SafeLoader subclass (see class docstring); safe_load itself cannot
        # be used because it rejects the CFN !Sub / !Ref tags.
        return yaml.load(f, Loader=_CfnLoader)  # nosec B506


def _resolve(value, account: str):
    """Resolve Fn::Sub / Ref pseudo-parameters into concrete strings."""
    if isinstance(value, dict):
        if "Fn::Sub" in value:
            s = value["Fn::Sub"]
            s = s.replace("${AWS::Region}", REGION).replace("${AWS::AccountId}", account)
            if re.search(r"\$\{[^}]+\}", s):
                raise SystemExit(f"unresolved substitution in template: {s}")
            return s
        if "Ref" in value:
            if value["Ref"] == "AWS::Region":
                return REGION
            if value["Ref"] == "AWS::AccountId":
                return account
            raise SystemExit(f"unresolved Ref in template: {value['Ref']}")
        return {k: _resolve(v, account) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, account) for v in value]
    return value


def render(account: str) -> dict:
    tpl = _load_template(TEMPLATE)
    role = tpl["Resources"]["CodeBuildRole"]["Properties"]
    doc = role["Policies"][0]["PolicyDocument"]
    statements = [
        _resolve(s, account) for s in doc["Statement"]
        if s.get("Sid") not in LAUNCHER_ONLY_SIDS
    ]
    return {"Version": "2012-10-17", "Statement": statements}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--account", required=True, help="12-digit AWS account id")
    args = ap.parse_args()
    if not (args.account.isdigit() and len(args.account) == 12):
        raise SystemExit("--account must be a 12-digit AWS account id")
    json.dump(render(args.account), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

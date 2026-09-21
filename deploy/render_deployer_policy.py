"""Render the least-privilege IAM policy for an identity running deploy.py.

The one-click launcher (deploy/codebuild-deploy.yaml) already defines a
scoped-down policy for the CodeBuild role that runs deploy.py — every
statement in it was exercised by a live end-to-end deploy and cleanup. Rather
than hand-maintain a second copy that would drift, this script extracts that
policy document and substitutes your account id and the deploy region, so a
human or CI identity running deploy.py (Option B) gets EXACTLY the same
permissions as the launcher, no more.

Usage:
    uv run python deploy/render_deployer_policy.py --account 123456789012 --out-dir ./policies
    # Writes one JSON file per managed policy (the launcher role attaches three,
    # each under IAM's 6,144-char managed-policy limit). Create each as a
    # customer-managed policy and attach it to a dedicated deploy role. See
    # DEPLOYMENT.md for the full steps.
    for f in ./policies/*.json; do
      arn=$(aws iam create-policy --policy-name "residency-chatbot-$(basename "$f" .json)" \
            --policy-document "file://$f" --query Policy.Arn --output text)
      aws iam attach-role-policy --role-name residency-chatbot-deployer --policy-arn "$arn"
    done

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


IAM_MANAGED_POLICY_LIMIT = 6144  # chars, whitespace excluded, per managed policy


def _iam_size(doc: dict) -> int:
    """Policy size as IAM counts it: JSON characters excluding whitespace."""
    return len(re.sub(r"\s", "", json.dumps(doc)))


def render(account: str) -> list[tuple[str, dict]]:
    """Return [(policy_name, policy_document), ...] — one per managed policy.

    The launcher attaches its permissions to the CodeBuild role as several
    AWS::IAM::ManagedPolicy resources (IAM caps a role's aggregate INLINE
    policy size at 10,240 chars, too small for a fully scoped document, while
    each customer-managed policy gets its own 6,144-char budget). The deployer
    gets the same split. Every rendered document is size-checked so a template
    edit can never produce a policy IAM will reject.
    """
    tpl = _load_template(TEMPLATE)
    resources = tpl["Resources"]
    role = resources["CodeBuildRole"]["Properties"]
    rendered = []
    for ref in role["ManagedPolicyArns"]:
        logical_id = ref["Ref"] if isinstance(ref, dict) else ref
        pol = resources[logical_id]
        if pol.get("Type") != "AWS::IAM::ManagedPolicy":
            continue  # AWS-managed ARN strings, if any, are not rendered
        statements = [
            _resolve(s, account) for s in pol["Properties"]["PolicyDocument"]["Statement"]
            if s.get("Sid") not in LAUNCHER_ONLY_SIDS
        ]
        doc = {"Version": "2012-10-17", "Statement": statements}
        size = _iam_size(doc)
        if size > IAM_MANAGED_POLICY_LIMIT:
            raise SystemExit(
                f"{logical_id}: {size} chars exceeds the IAM managed-policy limit "
                f"of {IAM_MANAGED_POLICY_LIMIT}; move statements to another policy"
            )
        rendered.append((logical_id, doc))
    return rendered


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--account", required=True, help="12-digit AWS account id")
    ap.add_argument("--out-dir", default=".",
                    help="directory to write <policy-name>.json files into (default: cwd)")
    args = ap.parse_args()
    if not (args.account.isdigit() and len(args.account) == 12):
        raise SystemExit("--account must be a 12-digit AWS account id")
    os.makedirs(args.out_dir, exist_ok=True)
    for name, doc in render(args.account):
        path = os.path.join(args.out_dir, f"{name}.json")
        with open(path, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        print(f"wrote {path}  ({_iam_size(doc)} chars, {len(doc['Statement'])} statements)",
              file=sys.stderr)


if __name__ == "__main__":
    main()

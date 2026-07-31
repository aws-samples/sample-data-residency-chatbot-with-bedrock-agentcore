"""Deployment configuration — single source of per-account settings.

WHY this exists
---------------
Every infra script needs the same handful of values: the fixed region, the
caller's account id, an S3 bucket for the demo data, the Bedrock model, and the
project resource-name prefix. This module centralises them so a
partner deploying into their own account only edits ONE file
(``deploy.config.json``) — or nothing at all, since sensible defaults are
derived from their AWS identity.

RESIDENCY: ``REGION`` is FIXED to ``ap-south-1`` (Mumbai) and is intentionally
NOT configurable — data residency is the whole point of this system.

Resolution order for each value:
  1. environment variable (e.g. ``MNRE_DATA_BUCKET``), else
  2. ``deploy.config.json`` at the repo root, else
  3. a derived default (account-namespaced, so it is globally unique).

Usage (from any infra script, which runs with its own dir on sys.path)::

    from config import REGION, ACCOUNT, DATA_BUCKET, MODEL_ID, load_ids, save_ids
"""
from __future__ import annotations

import functools
import json
import os

import boto3

# --------------------------------------------------------------------------- #
# Fixed, non-negotiable settings.
# --------------------------------------------------------------------------- #
# Data residency: everything runs in Mumbai. Do NOT make this configurable.
REGION = "ap-south-1"

# Bedrock model — in-region ON_DEMAND bare modelId (guarded against cross-region
# inference profiles — us./eu./ap./apac./jp./au./global. — by
# src/agent/residency.py). Any bare in-region modelId with Converse tool-use
# support works; see the Bedrock model support by Region page.
DEFAULT_MODEL_ID = "mistral.mistral-large-3-675b-instruct"

# Resource-name prefix. All created resources are named ``<PROJECT>-*`` so they
# are easy to find and delete. Account-scoped, so it needs no uniqueness suffix.
PROJECT = "mnre-chatbot"

# Consistent tag on everything we create (aids cost tracking + teardown).
TAG_KEY = "Project"
TAG_VALUE = "mnre-agentcore-chatbot"

# --------------------------------------------------------------------------- #
# Paths.
# --------------------------------------------------------------------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_CONFIG_PATH = os.path.join(_REPO_ROOT, "deploy.config.json")
# Per-account runtime STATE written by the deploy steps. Gitignored; regenerated
# on each fresh deploy. Never commit this (it holds account-specific ARNs).
IDS_PATH = os.path.join(_HERE, "network_ids.json")


# --------------------------------------------------------------------------- #
# deploy.config.json loader (optional file; all keys optional).
# --------------------------------------------------------------------------- #
@functools.lru_cache(maxsize=1)
def _file_config() -> dict:
    if os.path.isfile(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH) as f:
                return json.load(f) or {}
        except (OSError, ValueError):
            return {}
    return {}


def _resolve(key_env: str, key_file: str, default) -> str:
    """env var > deploy.config.json > default."""
    val = os.environ.get(key_env)
    if val:
        return val
    val = _file_config().get(key_file)
    if val:
        return val
    return default


# --------------------------------------------------------------------------- #
# Account id (resolved once from the caller's AWS identity).
# --------------------------------------------------------------------------- #
@functools.lru_cache(maxsize=1)
def get_account_id() -> str:
    """Return the deploying account id (STS caller identity), region-pinned."""
    explicit = os.environ.get("MNRE_ACCOUNT_ID") or _file_config().get("account_id")
    if explicit:
        return str(explicit)
    return boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]


# Eagerly resolved so scripts can ``from config import ACCOUNT``.
ACCOUNT = get_account_id()

# --------------------------------------------------------------------------- #
# Derived / configurable values.
# --------------------------------------------------------------------------- #
# S3 bucket holding the demo CSVs for the loader. Default is account-namespaced
# so it is globally unique; deploy.py creates it if missing (in ap-south-1).
DATA_BUCKET = _resolve("MNRE_DATA_BUCKET", "data_bucket", f"{PROJECT}-data-{ACCOUNT}")
DATA_PREFIX = _resolve("MNRE_DATA_PREFIX", "data_prefix", "chatbot-load")

MODEL_ID = _resolve("MNRE_MODEL_ID", "model_id", DEFAULT_MODEL_ID)

# Foundation-model ARN the Agent_Lambda role is allowed to invoke.
MODEL_ARN = f"arn:aws:bedrock:{REGION}::foundation-model/{MODEL_ID}"

TAGS = [{"Key": TAG_KEY, "Value": TAG_VALUE}]
TAGS_MAP = {TAG_KEY: TAG_VALUE}


# --------------------------------------------------------------------------- #
# network_ids.json state helpers (load existing, add keys, never overwrite the
# whole file blindly — callers merge and save).
# --------------------------------------------------------------------------- #
def load_ids() -> dict:
    """Load the runtime id state, or {} on first run."""
    if os.path.isfile(IDS_PATH):
        with open(IDS_PATH) as f:
            return json.load(f)
    return {}


def save_ids(ids: dict) -> None:
    """Persist the runtime id state (pretty-printed)."""
    with open(IDS_PATH, "w") as f:
        json.dump(ids, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    # Print the resolved config (no secrets) for a quick sanity check.
    print(f"REGION      = {REGION}")
    print(f"ACCOUNT     = {ACCOUNT}")
    print(f"PROJECT     = {PROJECT}")
    print(f"MODEL_ID    = {MODEL_ID}")
    print(f"DATA_BUCKET = {DATA_BUCKET}")
    print(f"DATA_PREFIX = {DATA_PREFIX}")
    print(f"IDS_PATH    = {IDS_PATH}")

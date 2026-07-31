"""Host the static Browser_UI (ui/index.html) on AWS Amplify Hosting via a
MANUAL (no-Git) zip deployment (ap-south-1).

Why Amplify: it serves the static shell over HTTPS with a clean
``https://{branch}.{appId}.amplifyapp.com`` URL and no bucket-policy /
Block-Public-Access wrangling. The shell (HTML/JS) is the ONLY thing hosted
here; all PROGRAM DATA still flows from the browser to the regional ap-south-1
API endpoint, so the data-residency story is unchanged.

Before zipping, this injects the deployed REST API URL (from network_ids.json)
into the UI's ``DEFAULT_API`` placeholder, so the partner's page calls THEIR
endpoint out of the box.

Manual deploy flow (boto3 ``amplify``, region-pinned ap-south-1):
  1. list_apps -> match by name (idempotent) else create_app.
  2. create_branch (enableAutoBuild=False) if the branch is absent.
  3. Build an in-memory zip with the (URL-injected) index.html at the ROOT.
  4. create_deployment -> PUT zip -> start_deployment.
  5. Poll get_job until SUCCEED/FAILED (~5 min timeout).
  6. Persist the live URL into network_ids.json; verify with an HTTPS GET.

Run:  uv run python infra/deploy_amplify.py
Requires the local AWS identity to have amplify:* on this app.
"""
from __future__ import annotations

import io
import os
import re
import sys
import time
import zipfile

import httpx

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import PROJECT, REGION, load_ids, save_ids  # noqa: E402

APP_NAME = f"{PROJECT}-ui"
BRANCH = "main"
MARKER = "Rooftop Solar Program"  # known string from ui/index.html
POLL_TIMEOUT = 300  # seconds (~5 min)
POLL_INTERVAL = 5

HERE = os.path.dirname(__file__)
UI_HTML = os.path.normpath(os.path.join(HERE, "..", "ui", "index.html"))
UI_VENDOR = os.path.normpath(os.path.join(HERE, "..", "ui", "vendor"))

amplify = boto3.client("amplify", region_name=REGION)


def find_app() -> dict | None:
    token = None
    while True:
        kwargs = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = amplify.list_apps(**kwargs)
        for app in resp.get("apps", []):
            if app.get("name") == APP_NAME:
                return app
        token = resp.get("nextToken")
        if not token:
            return None


def ensure_app() -> dict:
    app = find_app()
    if app:
        print(f"[app] reuse {APP_NAME} appId={app['appId']}")
        return app
    resp = amplify.create_app(
        name=APP_NAME,
        description="data-residency chatbot Browser_UI (manual deploy, static shell only)",
        platform="WEB",
    )
    app = resp["app"]
    print(f"[app] created {APP_NAME} appId={app['appId']}")
    return app


def ensure_branch(app_id: str) -> None:
    try:
        amplify.get_branch(appId=app_id, branchName=BRANCH)
        print(f"[branch] reuse {BRANCH}")
        return
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NotFoundException":
            raise
    amplify.create_branch(
        appId=app_id,
        branchName=BRANCH,
        enableAutoBuild=False,
        description="manual zip deploy of the static UI shell",
    )
    print(f"[branch] created {BRANCH} (enableAutoBuild=False)")


def _inject_api_url(html: str, rest_url: str) -> str:
    """Replace the DEFAULT_API placeholder/value with the deployed REST URL."""
    # The UI declares: var DEFAULT_API = "https://.../prod/chat";
    pattern = re.compile(r'(var\s+DEFAULT_API\s*=\s*")([^"]*)(")')
    if not pattern.search(html):
        print("[warn] DEFAULT_API declaration not found in UI; leaving as-is")
        return html
    new_html = pattern.sub(lambda m: m.group(1) + rest_url + m.group(3), html, count=1)
    print(f"[ui] injected REST API URL -> {rest_url}")
    return new_html


def build_zip(rest_url: str) -> bytes:
    if not os.path.isfile(UI_HTML):
        raise SystemExit(f"UI file not found: {UI_HTML}")
    with open(UI_HTML, "r", encoding="utf-8") as f:
        html = f.read()
    if MARKER not in html:
        raise SystemExit(f"marker {MARKER!r} not present in {UI_HTML}; aborting")
    if rest_url:
        html = _inject_api_url(html, rest_url)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html)  # ROOT, no nested folder
        # Vendored JS libraries (Chart.js, Motion) served alongside the page.
        if os.path.isdir(UI_VENDOR):
            for name in sorted(os.listdir(UI_VENDOR)):
                path = os.path.join(UI_VENDOR, name)
                if os.path.isfile(path):
                    zf.write(path, arcname=f"vendor/{name}")
    data = buf.getvalue()
    print(f"[zip] built {len(data)} bytes (index.html + vendor/ at root)")
    return data


def _require_https(url: str) -> None:
    """Refuse any non-HTTPS URL before making a request."""
    if not url.lower().startswith("https://"):
        raise SystemExit(f"refusing non-HTTPS URL: {url}")


def put_zip(zip_upload_url: str, data: bytes) -> None:
    # The URL is an AWS-presigned Amplify upload URL returned by
    # create_deployment; scheme is validated as https:// above.
    _require_https(zip_upload_url)
    resp = httpx.put(
        zip_upload_url,
        content=data,
        headers={"Content-Type": "application/zip"},
        timeout=120,
    )
    status = resp.status_code
    if status not in (200, 204):
        raise SystemExit(f"zip upload PUT returned {status}")
    print(f"[upload] PUT zip -> {status}")


def deploy(app_id: str, rest_url: str) -> str:
    created = amplify.create_deployment(appId=app_id, branchName=BRANCH)
    job_id = created["jobId"]
    zip_url = created["zipUploadUrl"]
    print(f"[deploy] jobId={job_id}")
    put_zip(zip_url, build_zip(rest_url))
    amplify.start_deployment(appId=app_id, branchName=BRANCH, jobId=job_id)
    print(f"[deploy] started jobId={job_id}")
    return job_id


def poll_job(app_id: str, job_id: str) -> str:
    deadline = time.time() + POLL_TIMEOUT
    last = None
    while time.time() < deadline:
        summary = amplify.get_job(appId=app_id, branchName=BRANCH, jobId=job_id)["job"]["summary"]
        status = summary["status"]
        if status != last:
            print(f"[job] status={status}")
            last = status
        if status in ("SUCCEED", "FAILED", "CANCELLED"):
            return status
        time.sleep(POLL_INTERVAL)
    return last or "TIMEOUT"


def verify(url: str) -> bool:
    # The URL is the deployed https://{branch}.{appId}.amplifyapp.com page;
    # scheme is validated as https:// above.
    _require_https(url)
    try:
        resp = httpx.get(
            url, headers={"User-Agent": "chatbot-deploy-verify"},
            timeout=30, follow_redirects=True,
        )
        status = resp.status_code
        body = resp.text
    except Exception as exc:  # noqa: BLE001
        print(f"[verify] FAIL: GET {url} raised {type(exc).__name__}: {exc}")
        return False
    ok = status == 200 and MARKER in body
    print(f"[verify] GET {url} -> {status}; marker {'found' if MARKER in body else 'MISSING'}")
    return ok


def main() -> None:
    print(f"Region {REGION}  App {APP_NAME}  Branch {BRANCH}\n")
    ids = load_ids()
    rest_url = ids.get("rest_api_url", "")
    if not rest_url:
        print("[warn] rest_api_url not in network_ids.json — run provision_rest_api.py first")

    app = ensure_app()
    app_id = app["appId"]
    default_domain = app.get("defaultDomain", f"{app_id}.amplifyapp.com")
    ensure_branch(app_id)

    job_id = deploy(app_id, rest_url)
    status = poll_job(app_id, job_id)

    url = f"https://{BRANCH}.{app_id}.amplifyapp.com"
    print(f"\n=== Amplify deployment ===")
    print(f"  appId         : {app_id}")
    print(f"  live URL      : {url}")
    print(f"  job status    : {status}")

    ids.update({
        "amplify_app_id": app_id,
        "amplify_branch": BRANCH,
        "amplify_default_domain": default_domain,
        "amplify_url": url,
    })
    save_ids(ids)

    if status != "SUCCEED":
        raise SystemExit(f"deployment did not succeed (status={status})")

    print("\n=== Verification (HTTPS GET) ===")
    ok = False
    for attempt in range(1, 6):
        if verify(url):
            ok = True
            break
        print(f"  retry {attempt}/5 in {POLL_INTERVAL}s…")
        time.sleep(POLL_INTERVAL)
    if not ok:
        raise SystemExit("verification FAILED: URL did not serve the expected page")
    print(f"\nDONE. Browser_UI live at {url}")


if __name__ == "__main__":
    main()

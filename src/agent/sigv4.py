"""SigV4 request signer for MCP-over-streamable-HTTP to AgentCore Gateway.

WHY this exists
---------------
The MNRE AgentCore Gateway uses IAM inbound authorization (``authorizerType=
'AWS_IAM'``), so every MCP HTTP request from the Agent_Lambda must be SigV4-signed
for service ``bedrock-agentcore`` in ``ap-south-1`` using the Lambda execution
role's credentials (the role holds ``bedrock-agentcore:InvokeGateway``).

Rather than pull in the heavyweight ``mcp-proxy-for-aws`` (which depends on
``fastmcp`` and can clash with the ``mcp`` SDK that ships inside ``strands-agents``),
we implement a tiny ``httpx.Auth`` that signs each outgoing request with botocore's
``SigV4Auth`` (botocore is already a transitive dependency of boto3). This auth is
handed to Strands' bundled ``streamablehttp_client(..., auth=...)``.
"""
from __future__ import annotations

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session as BotocoreSession

_SERVICE = "bedrock-agentcore"

# Hop-by-hop / auto-managed headers that must NOT be part of the signed set.
_SKIP_HEADERS = {"connection", "content-length", "host", "transfer-encoding"}


class SigV4HTTPXAuth(httpx.Auth):
    """An ``httpx.Auth`` that SigV4-signs each request for ``bedrock-agentcore``.

    Credentials are resolved from the standard botocore provider chain (the
    Lambda execution role at runtime; the local profile during a CLI smoke test).
    """

    # streamable-HTTP sends a JSON body we must hash, so httpx needs the full
    # request body available before auth runs.
    requires_request_body = True

    def __init__(self, region: str, service: str = _SERVICE, credentials=None):
        self._region = region
        self._service = service
        self._credentials = credentials or BotocoreSession().get_credentials()
        if self._credentials is None:
            raise RuntimeError(
                "No AWS credentials available to sign AgentCore Gateway requests."
            )

    def auth_flow(self, request: httpx.Request):
        body = request.content or b""

        # Build a botocore request mirroring the httpx request, carrying only the
        # headers we want included in the signature.
        signed_headers = {
            k: v for k, v in request.headers.items() if k.lower() not in _SKIP_HEADERS
        }
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=body,
            headers=signed_headers,
        )
        SigV4Auth(self._credentials, self._service, self._region).add_auth(aws_request)

        # Copy the SigV4 headers (Authorization, X-Amz-Date, X-Amz-Security-Token,
        # X-Amz-Content-SHA256, ...) back onto the outgoing httpx request.
        for key, value in aws_request.headers.items():
            request.headers[key] = value

        yield request

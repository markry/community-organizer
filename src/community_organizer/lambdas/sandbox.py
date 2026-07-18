"""D4 sandbox Lambda — used solely for OAC investigation.

Routed at ``/sandbox/*`` via a dedicated CloudFront cache behavior
pointing at this Lambda's Function URL. Lets us iterate the OAC +
ORP + InvokeMode combinations without touching the live ``/`` path
served by ``WebFunction``.

The handler returns a small JSON blob echoing back enough of the
inbound request to diagnose what CloudFront actually forwarded and
what OAC signed. Specifically:

    - rawPath / rawQueryString
    - HTTP method
    - Headers as CloudFront forwarded them (so we can see whether
      Authorization survived our Origin Request Policy)
    - Source IP (CloudFront's edge IP)
    - cookieCount as a sanity check that the Cookie header is
      forwarded normally

Once the investigation finds the working config, this Lambda can
stay (cheap) or be deleted with a one-line template change.
"""
from __future__ import annotations

import json
from typing import Any


def lambda_handler(event: dict, _context: Any) -> dict:
    headers = event.get("headers") or {}
    # CloudFront forwards everything case-insensitively; lambda runtime
    # normalizes to lowercase keys.
    auth_present = "authorization" in {k.lower() for k in headers}
    host = headers.get("host") or headers.get("Host") or ""
    user_agent = headers.get("user-agent") or headers.get("User-Agent") or ""
    cookies = event.get("cookies") or []
    body = {
        "ok": True,
        "marker": "community-organizer-sandbox-v1",
        "rawPath": event.get("rawPath"),
        "rawQueryString": event.get("rawQueryString"),
        "method": (event.get("requestContext", {})
                   .get("http", {}).get("method") or event.get("httpMethod")),
        "sourceIp": (event.get("requestContext", {})
                     .get("http", {}).get("sourceIp")),
        "host": host,
        "user_agent": user_agent,
        "authorization_present_in_headers": auth_present,
        "cookie_count": len(cookies),
        "header_names": sorted(k.lower() for k in headers),
    }
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }

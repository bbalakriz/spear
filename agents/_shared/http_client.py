#!/usr/bin/env python3
"""http helper that routes through the openshell sidecar proxy, same
shape as the reference's agents/http_client.py, minus its auth_client
dependency: that one always stamped one fixed bearer token (client
credentials grant) on every call. neither agent here needs that, the
coordinator's own outbound Authorization to the retrieval agent's a2a
endpoint is injected automatically by the sidecar's own token_grant
provider (spiffe svid -> keycloak access token), not something this
code computes, and the retrieval agent's own outbound Authorization to
spear-shield-gateway is the real end user's own jwt, passed in
explicitly by main.py per call, not one fixed credential. extra_headers
below is how main.py supplies that per call value.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import ssl
import urllib.request
from typing import Any

_PROXY = os.environ.get("OPENSHELL_HTTP_PROXY", "http://127.0.0.1:3128")
_SSL_CONTEXT = ssl._create_unverified_context()
_COOKIE_JAR = http.cookiejar.CookieJar()
_MCP_SESSIONS: dict[str, str] = {}
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": _PROXY, "https": _PROXY}),
    urllib.request.HTTPSHandler(context=_SSL_CONTEXT),
    urllib.request.HTTPCookieProcessor(_COOKIE_JAR),
)


def mcp_json(
    url: str,
    payload: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("accept", "application/json, text/event-stream")
    session_id = _MCP_SESSIONS.get(url)
    if session_id:
        req.add_header("mcp-session-id", session_id)
    for key, value in (extra_headers or {}).items():
        req.add_header(key, value)
    with _OPENER.open(req, timeout=45) as resp:
        new_session = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if new_session:
            _MCP_SESSIONS[url] = new_session
        raw = resp.read().decode("utf-8")
    if raw.startswith("event:"):
        for line in raw.splitlines():
            if line.startswith("data: "):
                raw = line[6:]
                break
    return json.loads(raw) if raw else {}


def http_json(
    url: str,
    payload: dict[str, Any] | None = None,
    method: str = "POST",
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if payload is not None:
        req.add_header("content-type", "application/json")
    req.add_header("accept", "application/json, text/event-stream")
    for key, value in (extra_headers or {}).items():
        req.add_header(key, value)
    with _OPENER.open(req, timeout=45) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}

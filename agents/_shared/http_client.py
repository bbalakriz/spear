#!/usr/bin/env python3
"""http helper that routes through the openshell sidecar proxy. neither
agent needs a fixed bearer token: the coordinator's outbound
Authorization to the retrieval a2a endpoint is injected automatically
by the sidecar's token_grant provider, and the retrieval agent's
outbound Authorization to spear-mcp-gateway is the end user's own jwt,
passed in per call via extra_headers below.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import ssl
import urllib.request
from typing import Any

# supervisor sets HTTPS_PROXY; fallback below for local dev
_PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "http://127.0.0.1:3128"
# supervisor ca bundle at /etc/openshell-tls/proxy/ca-bundle.pem, trust it
# properly when present instead of disabling verification outright
_CA_BUNDLE = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
_SSL_CONTEXT = (
    ssl.create_default_context(cafile=_CA_BUNDLE) if _CA_BUNDLE else ssl._create_unverified_context()
)
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

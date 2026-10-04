#!/usr/bin/env python3
"""stdlib reverse proxy that puts externalized, gateway side guardrail
checks in front of any mcp backend, no backend code changes needed.

sits between the httproute and the real mcp server, see
manifests/spear-shield-agents/04-mcp-guard-proxy.yaml, replacing the
backend service as the httproute's own backendref, then reissuing every
request to the real backend itself over the cluster network. tools/call
requests are inspected on the way in (request guard) and the real
backend's own response is inspected on the way out (response guard),
everything else (initialize, tools/list, ping, notifications) passes
through untouched.

one shared deployment fronts every mcp server, not one per server. found
live, not guessed: mcp-gateway's own ext_proc filter already stamps
x-mcp-servername on every request, "<namespace>/<server-name>", the same
value kuadrant's own tool-access-check AuthPolicy already keys its rbac
off, see manifests/spear-shield-agents/03-auth-policies.yaml. every
mcp server built here follows the same shape, a Service named after the
server on port 8000 serving /mcp, so the real upstream is derivable from
that one header, no per server env var, no mapping file to maintain.
onboarding a new server is a one line httproute backendref change to
this same shared proxy service, nothing else.

a second source for that same namespace/name pair, found live, not
documented anywhere: mcp-gateway's own internal mcp-manager component
dials this proxy directly to discover each registered server's own
tools, bypassing the envoy listener and its ext_proc filter entirely for
that one connection, so x-mcp-servername is never present on it. it does
carry a gateway-server-id header instead, same namespace/name pair as
its own prefix, see resolve_upstream's own comment for the full story.
this is what makes the shared deployment actually scale past one mcp
server, a single UPSTREAM_URL fallback cannot tell two servers' own
discovery connections apart.

why this exists instead of wiring nemo-request-guard/nemo-response-guard
as envoy ext_proc filters: a real, open upstream bug hangs a second
ext_proc filter trying to read a request body that a first ext_proc
filter already read, confirmed live on this cluster this same
investigation, independent of body mode (BUFFERED or FULL_DUPLEX_STREAMED)
and independent of whether mcp-gateway's own ext_proc filter is present
at all. see llm-d/llm-d-inference-payload-processor#209 and
opendatahub-io/ai-gateway-payload-processing#377, both open, no merged
fix as of this writing. plain http proxying has none of that fragility,
same pattern this project already uses everywhere else, this file is
close kin to spear-openproject-mcp's own server.py on purpose.

happy path (both checks pass) is a byte for byte pass through of the
real backend's own response, we only ever re encode a response when
overriding a blocked tool call's result.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8000"))

# convention every mcp server here follows: a Service named after the
# server, same namespace, this port, this path. used to derive the real
# upstream from x-mcp-servername, see resolve_upstream below
UPSTREAM_PORT = os.environ.get("UPSTREAM_PORT", "8000")
UPSTREAM_PATH = os.environ.get("UPSTREAM_PATH", "/mcp")

# only used as a last resort when neither header below is present, e.g.
# a direct test that bypasses mcp-gateway entirely. not needed for
# mcp-gateway's own internal connections any more, see resolve_upstream
UPSTREAM_URL_FALLBACK = os.environ.get("UPSTREAM_URL", "")

GUARDRAILS_ROUTE = os.environ.get("GUARDRAILS_ROUTE", "")
GUARDRAILS_CONFIG_ID = os.environ.get("GUARDRAILS_CONFIG_ID", "work-tracker-config")
GUARDRAILS_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"

# headers we don't blindly copy through to the upstream request, urllib
# manages host and content-length itself based on the real body we send
HOP_BY_HOP = {"host", "content-length", "connection", "transfer-encoding"}


def _namespace_name_to_url(namespace_name: str) -> str | None:
    if namespace_name and "/" in namespace_name:
        namespace, name = namespace_name.split("/", 1)
        if namespace and name:
            return f"http://{name}.{namespace}.svc.cluster.local:{UPSTREAM_PORT}{UPSTREAM_PATH}"
    return None


def resolve_upstream(headers: Any) -> str | None:
    """every mcp server here is a Service named after itself on
    UPSTREAM_PORT serving UPSTREAM_PATH, so the real backend is always
    derivable from a "<namespace>/<name>" pair, no per server config
    needed to add one. two different places carry that pair, checked in
    order:

    x-mcp-servername, stamped by mcp-gateway's own envoy ext_proc filter
    on real client traffic through the gateway's public listener, exact
    "<namespace>/<name>" value, nothing else in it.

    gateway-server-id, found live, not documented anywhere we could find:
    mcp-gateway's own internal mcp-manager component, which periodically
    dials this proxy directly to discover each registered server's own
    tools, bypasses the envoy listener entirely for that connection, so
    it never carries x-mcp-servername. it does carry this header though,
    "<namespace>/<name>:<tool-prefix>:<httproute-hostname>", confirmed
    live by temporarily logging every header on a request missing
    x-mcp-servername. the "<namespace>/<name>" part before the first
    colon is the exact same pair x-mcp-servername carries.

    this matters the moment a second mcp server shares this one proxy
    deployment: UPSTREAM_URL is a single, fixed value, it cannot tell
    two servers' own discovery connections apart, every one of them
    would get silently routed to whatever one backend it happens to be
    set to. gateway-server-id can, since mcp-gateway sends the real
    server's own identity on every discovery connection it makes, not
    just one. UPSTREAM_URL stays only as a genuinely last resort, for a
    direct test that bypasses mcp-gateway entirely and so carries
    neither header.
    """
    upstream = _namespace_name_to_url(headers.get("x-mcp-servername", ""))
    if upstream:
        return upstream
    gateway_server_id = headers.get("gateway-server-id", "")
    upstream = _namespace_name_to_url(gateway_server_id.split(":", 1)[0])
    if upstream:
        return upstream
    return UPSTREAM_URL_FALLBACK or None


def guardrails_check(text: str) -> tuple[bool, str]:
    """same real /v1/guardrail/checks call spear-openproject-mcp's own
    server.py makes, reused here so the proxy and the backend agree on
    what counts as blocked. returns (allowed, verdict).
    """
    if not GUARDRAILS_ROUTE:
        return True, "skipped-no-route-configured"
    try:
        with open(GUARDRAILS_SA_TOKEN_PATH, encoding="utf-8") as f:
            sa_token = f.read().strip()
    except FileNotFoundError:
        return True, "skipped-no-sa-token"

    body = json.dumps(
        {
            "model": "test",
            "messages": [{"role": "user", "content": text}],
            "guardrails": {"config_id": GUARDRAILS_CONFIG_ID},
        }
    ).encode("utf-8")
    req = urllib.request.Request(f"{GUARDRAILS_ROUTE}/v1/guardrail/checks", data=body, method="POST")
    req.add_header("Authorization", f"Bearer {sa_token}")
    req.add_header("Content-Type", "application/json")
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, ValueError) as exc:
        # real network/parse failure, surfaced, not silently allowed
        return False, f"error: {exc}"
    verdict = data.get("status", "error")
    return verdict == "success", verdict


def blocked_result(request_id: Any, stage: str, verdict: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": f"tool {stage} blocked by guardrails, verdict: {verdict}"}],
            "isError": True,
        },
    }


def extract_tool_text(rpc: dict[str, Any]) -> str | None:
    """pulls the plain text out of a tools/call result's content list, the
    same shape both the request guard (arguments) and response guard
    (result) need to hand the guardrails service.
    """
    content = (rpc.get("result") or {}).get("content") or []
    parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    return "\n".join(parts) if parts else None


def encode_rpc(rpc: dict[str, Any], accept_header: str) -> tuple[bytes, str]:
    """mirrors the backend's own accept negotiation, server.py's _send
    plus do_POST, so a caller sees the same framing whether the result
    came from the real backend or from this proxy blocking it.
    """
    encoded = json.dumps(rpc).encode("utf-8")
    if "text/event-stream" in accept_header.lower():
        return b"event: message\ndata: " + encoded + b"\n\n", "text/event-stream"
    return encoded, "application/json"


def decode_body(raw: bytes, content_type: str) -> dict[str, Any] | None:
    """undoes encode_rpc, used to read the real backend's response back
    into a dict so the response guard can inspect it.
    """
    payload = raw
    if "text/event-stream" in content_type.lower():
        for line in raw.split(b"\n"):
            if line.startswith(b"data: "):
                payload = line[len(b"data: ") :]
                break
    try:
        return json.loads(payload.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"guard-proxy {self.address_string()} {fmt % args}")

    def _send(self, code: int, payload: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/health", "/healthz", "/ready"):
            self._send(200, b'{"status":"ok"}', "application/json")
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/", "/mcp", "/mcp/"):
            self._send(404, b'{"error":"not found"}', "application/json")
            return

        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        accept_header = self.headers.get("accept", "")

        upstream_url = resolve_upstream(self.headers)
        if not upstream_url:
            self._send(
                502,
                b'{"error":"no x-mcp-servername or gateway-server-id header and no UPSTREAM_URL fallback configured, cannot resolve a backend"}',
                "application/json",
            )
            return

        try:
            rpc = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send(400, b'{"error":"invalid json"}', "application/json")
            return

        is_tool_call = rpc.get("method") == "tools/call"

        # request guard: check the caller's own tool arguments before the
        # real backend, which never implemented its own request side
        # check, ever sees them
        if is_tool_call:
            arguments = (rpc.get("params") or {}).get("arguments") or {}
            allowed, verdict = guardrails_check(json.dumps(arguments))
            if not allowed:
                encoded, content_type = encode_rpc(blocked_result(rpc.get("id"), "call", verdict), accept_header)
                self._send(200, encoded, content_type)
                return

        upstream_req = urllib.request.Request(upstream_url, data=raw, method="POST")
        for name, value in self.headers.items():
            if name.lower() not in HOP_BY_HOP:
                upstream_req.add_header(name, value)

        try:
            with urllib.request.urlopen(upstream_req, timeout=25) as resp:
                upstream_body = resp.read()
                upstream_status = resp.status
                upstream_content_type = resp.headers.get("content-type", "application/json")
                upstream_session_header = resp.headers.get("mcp-session-id")
        except urllib.error.HTTPError as exc:
            upstream_body = exc.read()
            upstream_status = exc.code
            upstream_content_type = exc.headers.get("content-type", "application/json")
            upstream_session_header = exc.headers.get("mcp-session-id")
        except urllib.error.URLError as exc:
            self._send(502, json.dumps({"error": f"upstream unreachable: {exc}"}).encode("utf-8"), "application/json")
            return

        if upstream_status == 204 and not upstream_body:
            self.send_response(204)
            self.send_header("content-length", "0")
            self.end_headers()
            return

        # response guard: only for a successful tools/call result, every
        # other method (initialize, tools/list, ping) passes through
        # byte for byte, untouched
        if is_tool_call and upstream_status == 200:
            backend_rpc = decode_body(upstream_body, upstream_content_type)
            text = extract_tool_text(backend_rpc) if backend_rpc else None
            if text is not None:
                allowed, verdict = guardrails_check(text)
                if not allowed:
                    encoded, content_type = encode_rpc(
                        blocked_result(rpc.get("id"), "response", verdict), accept_header
                    )
                    self._send(upstream_status, encoded, content_type)
                    return

        self.send_response(upstream_status)
        self.send_header("content-type", upstream_content_type)
        self.send_header("content-length", str(len(upstream_body)))
        self.send_header("cache-control", "no-store")
        if upstream_session_header:
            self.send_header("mcp-session-id", upstream_session_header)
        self.end_headers()
        if upstream_body:
            self.wfile.write(upstream_body)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        f"spear-shield-mcp-guard-proxy listening on {HOST}:{PORT}, "
        f"upstream resolved per request from x-mcp-servername, fallback {UPSTREAM_URL_FALLBACK or '(none)'}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()

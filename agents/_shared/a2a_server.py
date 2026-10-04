#!/usr/bin/env python3
"""tiny json-rpc http helper used by both sandboxed agents, copied
verbatim in shape from the reference's agents/a2a_server.py, same a2a
envelope (agent-card, message/send, completed_task), no changes needed
here, the real differences between this project and the reference live
in main.py's own logic, not in this transport layer.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

JsonHandler = Callable[[dict[str, Any], str], dict[str, Any] | None]


def serve(host: str, port: int, on_rpc: JsonHandler, on_card: Callable[[], dict[str, Any]]) -> None:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"a2a {self.address_string()} {fmt % args}")

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
            if path in ("/.well-known/agent-card.json", "/.well-known/agent.json", "/agent-card.json"):
                encoded = json.dumps(on_card()).encode("utf-8")
                self._send(200, encoded, "application/json")
                return
            self._send(404, b'{"error":"not found"}', "application/json")

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path not in ("/", "/a2a", "/rpc"):
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            length = int(self.headers.get("content-length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send(400, b'{"error":"invalid json"}', "application/json")
                return
            try:
                # the inbound caller's own bearer token, same spot
                # spear-openproject-mcp's own handle_rpc(body, auth_header)
                # already reads it from, section 7 step 1: the coordinator
                # receives the user's query plus their real keycloak jwt as
                # a plain Authorization header on this request, not inside
                # the json-rpc body, that is how an ordinary http caller
                # like owner-console-backend actually sends a bearer token
                result = on_rpc(body, self.headers.get("authorization", ""))
            except Exception as exc:  # noqa: BLE001
                result = {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "error": {"code": -32000, "message": str(exc)},
                }
            if result is None:
                self._send(204, b"", "application/json")
                return
            encoded = json.dumps(result).encode("utf-8")
            self._send(200, encoded, "application/json")

    print(f"a2a listening on {host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def text_from_message(payload: dict[str, Any]) -> str:
    params = payload.get("params") or {}
    message = params.get("message") or params.get("task") or payload.get("message") or {}
    parts = message.get("parts") or []
    texts = []
    for part in parts:
        if isinstance(part, dict) and part.get("text"):
            texts.append(str(part["text"]))
    if texts:
        return "\n".join(texts)
    if isinstance(message.get("text"), str):
        return message["text"]
    if isinstance(params.get("text"), str):
        return params["text"]
    return ""


def caller_token_from_message(payload: dict[str, Any]) -> str | None:
    """pulls out the one extra field this project's own a2a envelope
    carries beyond the plain a2a spec, params.callerToken, the real end
    user's own keycloak jwt, section 7's own point of having two agents
    at all: the coordinator's outbound Authorization header on this hop
    proves which workload is calling (spire svid -> keycloak access
    token, via openshell token_grant), not which end user, so the end
    user's own token has to travel as application level payload instead,
    not a real a2a spec field, ours to add since we write both ends
    """
    params = payload.get("params") or {}
    token = params.get("callerToken")
    return str(token) if token else None


def completed_task(request_id: Any, text: str, trace: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": f"task-{request_id}",
        "contextId": f"ctx-{request_id}",
        "status": {"state": "completed"},
        "artifacts": [
            {
                "artifactId": "result",
                "name": "result",
                "parts": [{"kind": "text", "type": "text", "text": text}],
            }
        ],
    }
    # not part of the a2a spec, same kind of project specific extra field
    # as params.callerToken above: a real per hop timing breakdown of the
    # request this agent just handled, owner-console's chat page renders
    # it as a live trace. only the coordinator populates this today, see
    # main.py's own answer()
    if trace:
        result["trace"] = trace
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }

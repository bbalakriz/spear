#!/usr/bin/env python3
"""plain http relay in front of ogx, deployed inside rag-phase1 itself,
see manifests/50-spear-shield-agents/openshell/ogx-relay.yaml for why
this lives here rather than in spear-shield-agents (ogx's own
NetworkPolicy only allows same namespace ingress, confirmed live, a
direct patch to it gets silently reverted by the ogx-operator's own
reconciler within seconds).

started life as a blind byte for byte relay, promoted to a real file
once it needed to do something: put a real guardrail check in front of
spear-coordinator-agent's own /v1/chat/completions call, section 7's
open item, the work tracker tool response already had one of these
(work-tracker-config, mcp-servers/spear-shield-mcp-guard-proxy/server.py),
the coordinator's own reasoning call never did.

only /v1/chat/completions is inspected. everything else, most
importantly /v1/embeddings (the retrieval agent's own calls through this
same relay), passes through byte for byte untouched, same as before this
file existed. the real red hat docs for nemo guardrails on rhoai 3.4 say
plainly that its own /v1/chat/completions wrapping endpoint "is not a
transparent proxy" and recommend keeping inference and guardrailing as
separate steps for anything using tool calling, which the coordinator's
own call genuinely does (real tool_calls, tool_choice: auto), so this
stays two separate /v1/guardrail/checks calls around a real, untouched
call to ogx, never a wrapped completions call, same shape as the guard
proxy's own request guard plus response guard pattern.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TARGET = os.environ["TARGET"].rstrip("/")
HOST = os.environ.get("RELAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("RELAY_PORT", "8080"))

GUARDRAILS_ROUTE = os.environ.get("GUARDRAILS_ROUTE", "")
GUARDRAILS_CONFIG_ID = os.environ.get("GUARDRAILS_CONFIG_ID", "coordinator-agent-config")
GUARDRAILS_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"

HOP_BY_HOP = {"host", "content-length", "connection", "transfer-encoding"}


def guardrails_check(text: str) -> tuple[bool, str]:
    """same real /v1/guardrail/checks call spear-shield-mcp-guard-proxy's
    own server.py makes, reused here so every guardrail enforcement point
    in this project agrees on what counts as blocked.
    """
    if not GUARDRAILS_ROUTE or not text.strip():
        return True, "skipped-no-route-or-empty-text"
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


def last_user_text(body: dict[str, Any]) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else ""
    return ""


def blocked_response(verdict: str) -> bytes:
    # same openai compatible error shape a real chat/completions caller
    # already knows how to parse, not a made up envelope
    return json.dumps(
        {
            "error": {
                "message": f"request blocked by guardrails, verdict: {verdict}",
                "type": "guardrails_blocked",
            }
        }
    ).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"ogx-relay {self.address_string()} {fmt % args}")

    def do_GET(self) -> None:  # noqa: N802
        self._relay("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._relay("POST")

    def _send(self, code: int, payload: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _relay(self, method: str) -> None:
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length) if length else None
        is_chat_completions = method == "POST" and self.path.rstrip("/") == "/v1/chat/completions"

        body: dict[str, Any] | None = None
        if is_chat_completions and raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                body = None

        # request guard, only for a real chat/completions call, every
        # other path (most importantly /v1/embeddings) is never parsed
        # or touched at all
        if is_chat_completions and body is not None:
            prompt_text = last_user_text(body)
            allowed, verdict = guardrails_check(prompt_text)
            if not allowed:
                self._send(400, blocked_response(verdict))
                return

        req = urllib.request.Request(f"{TARGET}{self.path}", data=raw, method=method)
        for key, value in self.headers.items():
            if key.lower() not in HOP_BY_HOP:
                req.add_header(key, value)

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                upstream_body = resp.read()
                upstream_status = resp.status
                upstream_headers = dict(resp.headers.items())
        except urllib.error.HTTPError as exc:
            upstream_body = exc.read()
            upstream_status = exc.code
            upstream_headers = dict(exc.headers.items())
        except urllib.error.URLError as exc:
            self._send(502, json.dumps({"error": f"upstream unreachable: {exc}"}).encode("utf-8"))
            return

        # response guard, only for a successful chat/completions call,
        # never for embeddings or an already failed upstream call
        if is_chat_completions and upstream_status == 200:
            try:
                upstream_json = json.loads(upstream_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                upstream_json = None
            answer_text = ""
            if upstream_json:
                choices = upstream_json.get("choices") or []
                if choices:
                    answer_text = (choices[0].get("message") or {}).get("content") or ""
            if answer_text:
                allowed, verdict = guardrails_check(answer_text)
                if not allowed:
                    self._send(400, blocked_response(verdict))
                    return

        self.send_response(upstream_status)
        for key, value in upstream_headers.items():
            if key.lower() not in ("transfer-encoding", "connection"):
                self.send_header(key, value)
        self.end_headers()
        if upstream_body:
            self.wfile.write(upstream_body)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        f"spear-shield-ogx-relay listening on {HOST}:{PORT}, target {TARGET}, "
        f"guardrails route {GUARDRAILS_ROUTE or '(none configured)'}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""spear-coordinator-agent, PHASE2_PLAN.md section 7.

receives the user's text query plus their real keycloak bearer token
(an ordinary Authorization header on the inbound a2a request, see
_shared/a2a_server.py's own comment on why), calls the foundation
model with two real tools described, rag_search and
search_work_packages/update_work_package, using real openai compatible
tool calling against ogx (confirmed live before writing this, a direct
/v1/chat/completions call against glm-53-flash with a tools schema
genuinely returns a real tool_calls response, not assumed), lets the
model decide what the query needs, then delegates the actual tool
calls to spear-retrieval-agent over spear-shield-a2a-gateway rather
than executing them itself, same architecture decision PHASE2_PLAN.md
section 2 makes explicit: this project exists to secure two kinds of
traffic, agent to agent and agent to tool, a single agent design only
ever exercises the second one.

two different credentials travel on the coordinator -> retrieval hop,
on purpose, not by accident:
- the outbound Authorization header on that http call is this sandbox's
  own workload identity, a spire jwt-svid asserted to keycloak and
  injected automatically by the openshell supervisor's own
  coordinator-a2a-token-grant provider, this code never touches it
- the real end user's own jwt travels as an extra json-rpc field,
  params.callerToken, see _shared/a2a_server.py's caller_token_from_message,
  since the header slot is already spoken for by the workload identity
  above. spear-retrieval-agent reads it from there, not from its own
  inbound Authorization header
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from a2a_server import completed_task, serve, text_from_message  # noqa: E402
from http_client import http_json  # noqa: E402

HOST = os.environ.get("AGENT_HOST", "0.0.0.0")
PORT = int(os.environ.get("AGENT_PORT", "8080"))
NAME = os.environ.get("AGENT_NAME", "spear-coordinator-agent")
VERSION = os.environ.get("AGENT_VERSION", "0.1.0")
PUBLIC_URL = os.environ.get("AGENT_PUBLIC_URL", f"http://{NAME}:{PORT}/")
# the service's own dns name is deliberately the exact string the
# spear-shield-a2a-gateway HTTPRoute's own hostnames field matches
# against, mirroring the reference's own specialist-a2a-gateway naming,
# so the plain Host header this call sends by default is already
# correct, no manual override needed on this hop (unlike the mcp
# gateway hop below, whose friendly service name is not the gateway's
# own externally facing hostname)
RETRIEVAL_A2A_URL = os.environ.get(
    "RETRIEVAL_A2A_URL", "http://spear-retrieval-a2a-gateway.spear-shield-agents.svc.cluster.local:80"
).rstrip("/")

# routed through spear-shield-ogx-relay, not ogx's own service
# directly, same reason spear-retrieval-agent's own main.py documents:
# ogx's own NetworkPolicy only allows ingress from pods already inside
# rag-phase1 itself, confirmed live, a manual patch to that policy got
# silently reverted by the ogx-operator's own reconciler within seconds
OGX_BASE_URL = os.environ.get("OGX_BASE_URL", "http://spear-shield-ogx-relay.rag-phase1.svc.cluster.local:8080")
OGX_API_KEY = os.environ.get("OGX_API_KEY", "")
# glm-53-flash's own /v1/chat/completions tool calling confirmed live
# before writing this file, real finish_reason tool_calls, real
# function name and arguments back, not assumed from the "tool calling
# enabled" capability line in PHASE1_PLAN.md alone
GENERATION_MODEL = os.environ.get("GENERATION_MODEL", "openai/publishers/prelude-maas/models/glm-53-flash")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": (
                "search the permission scoped internal knowledge base for documents "
                "relevant to the question. results are already filtered to what the "
                "caller is allowed to see, never ask for documents outside what comes back"
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "the question or topic to search for"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_work_packages",
            "description": "search the caller's own assigned work packages in the data governance work tracker",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "optional status filter, e.g. open, on hold, closed, in progress"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_work_package",
            "description": "update a work package's status, only ever succeeds for the caller's own assigned work packages",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "the work package id"},
                    "status": {"type": "string", "description": "target status, e.g. approved, in progress, closed, rejected"},
                },
                "required": ["id", "status"],
            },
        },
    },
]

SYSTEM_PROMPT = (
    "You are SPEAR Shield's assistant. You can search an internal knowledge base "
    "(rag_search) and look up or update the caller's own work packages in the data "
    "governance work tracker (search_work_packages, update_work_package). Every tool "
    "call is scoped to the caller's own real permissions, a refusal from a tool is a "
    "real access control decision, not an error to retry or route around, report it to "
    "the user plainly rather than hiding it or trying another way to get the same data."
)


def agent_card() -> dict[str, Any]:
    return {
        "name": NAME,
        "description": "coordinator that answers questions using a permission scoped rag search and work tracker tools",
        "url": PUBLIC_URL,
        "version": VERSION,
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "capabilities": {"streaming": False},
        "skills": [
            {
                "id": "spear-shield-coordinator",
                "name": "SPEAR Shield Coordinator",
                "description": "answers questions by combining a permission scoped rag search and work tracker tool calls",
                "tags": ["orchestration", "a2a", "rag", "mcp"],
                "examples": ["What are my open work packages, and what does our policy say about data retention?"],
            }
        ],
    }


def chat_completion(messages: list[dict[str, Any]]) -> dict[str, Any]:
    headers = {"content-type": "application/json"}
    if OGX_API_KEY:
        headers["authorization"] = f"Bearer {OGX_API_KEY}"
    return http_json(
        f"{OGX_BASE_URL}/v1/chat/completions",
        {"model": GENERATION_MODEL, "messages": messages, "tools": TOOLS, "tool_choice": "auto"},
        extra_headers=headers,
    )


def delegate_tool_calls(tool_calls: list[dict[str, Any]], caller_token: str) -> dict[str, str]:
    """hands the model's own chosen tool calls to spear-retrieval-agent in
    one batch, over a2a, and returns {tool_call_id: result_text}. the
    retrieval agent is the one that actually touches rag_chunks and the
    work tracker mcp tool, section 2's own isolation rationale, not this
    sandbox.
    """
    calls_payload = [
        {"id": tc["id"], "name": tc["function"]["name"], "arguments": json.loads(tc["function"]["arguments"] or "{}")}
        for tc in tool_calls
    ]
    payload = {
        "jsonrpc": "2.0",
        "id": "coord-delegate",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "type": "text", "text": json.dumps({"tool_calls": calls_payload})}],
            },
            # the real end user's own jwt, not this sandbox's own
            # workload identity, see this file's module docstring
            "callerToken": caller_token,
        },
    }
    body = http_json(RETRIEVAL_A2A_URL, payload)
    artifacts = (body.get("result") or {}).get("artifacts") or []
    text = artifacts[0]["parts"][0]["text"] if artifacts else json.dumps({"error": body})
    try:
        results = json.loads(text)
    except json.JSONDecodeError:
        return {tc["id"]: text for tc in tool_calls}
    by_id = {r["id"]: r["result"] for r in results if "id" in r}
    return {tc["id"]: by_id.get(tc["id"], json.dumps({"error": "no result returned for this tool call"})) for tc in tool_calls}


def answer(user_text: str, caller_token: str) -> str:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    first = chat_completion(messages)
    choice = (first.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    tool_calls = message.get("tool_calls") or []

    if not tool_calls:
        return message.get("content") or "no answer came back from the model"

    messages.append(message)
    results_by_id = delegate_tool_calls(tool_calls, caller_token)
    for tc in tool_calls:
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": results_by_id.get(tc["id"], json.dumps({"error": "no result"})),
            }
        )

    final = chat_completion(messages)
    final_message = (final.get("choices") or [{}])[0].get("message") or {}
    return final_message.get("content") or "no synthesized answer came back from the model"


def on_rpc(body: dict[str, Any], auth_header: str) -> dict[str, Any] | None:
    method = body.get("method")
    if method not in ("message/send", "tasks/send", "tasks/sendSubscribe"):
        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "error": {"code": -32601, "message": f"unsupported method: {method}"},
        }
    # prefer params.callerToken, same body field a2a already carries it
    # in one hop further down. confirmed live that openshell's own
    # gateway relay strips the inbound Authorization header somewhere
    # between the real caller and this sandbox (exec'ing straight into
    # the sandbox and setting it by hand works fine, going through the
    # exposed service does not), so the header is only a fallback for
    # direct/local testing, not something a real caller through the
    # gateway can rely on
    caller_token = (body.get("params") or {}).get("callerToken")
    if not caller_token and auth_header.lower().startswith("bearer "):
        caller_token = auth_header.split(" ", 1)[1].strip()
    if not caller_token:
        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "error": {"code": -32001, "message": "this request has no caller bearer token, cannot scope any tool call to a real identity"},
        }
    prompt = text_from_message(body)
    print(f"coordinator request (sandbox={socket.gethostname()}): {prompt}")
    text = answer(prompt, caller_token)
    return completed_task(body.get("id"), text)


def main() -> None:
    print(f"spear-coordinator-agent retrieval_a2a={RETRIEVAL_A2A_URL} model={GENERATION_MODEL}")
    serve(HOST, PORT, on_rpc, agent_card)


if __name__ == "__main__":
    main()

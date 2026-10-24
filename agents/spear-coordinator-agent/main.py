#!/usr/bin/env python3
"""spear-coordinator-agent.

takes the user's query and keycloak bearer token, calls the foundation
model with rag_search and work tracker tools, then delegates tool
execution to spear-retrieval-agent over spear-a2a-gateway rather than
running tools in this sandbox.

two credentials travel on the coordinator -> retrieval hop: the outbound
Authorization header is this sandbox's own workload identity (spire
jwt-svid, injected by the openshell token_grant provider), while the real
end user's jwt travels as params.callerToken instead, since the header
slot is already taken. see _shared/a2a_server.py's caller_token_from_message.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
from typing import Any

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from a2a_server import completed_task, serve, text_from_message  # noqa: E402
from http_client import http_json  # noqa: E402
import mlflow_tracing  # noqa: E402

HOST = os.environ.get("AGENT_HOST", "0.0.0.0")
PORT = int(os.environ.get("AGENT_PORT", "8080"))
NAME = os.environ.get("AGENT_NAME", "spear-coordinator-agent")
VERSION = os.environ.get("AGENT_VERSION", "0.1.0")
PUBLIC_URL = os.environ.get("AGENT_PUBLIC_URL", f"http://{NAME}:{PORT}/")
# dns name matches the HTTPRoute hostnames field, so the default Host
# header is already correct here (unlike the mcp gateway hop below)
RETRIEVAL_A2A_URL = os.environ.get(
    "RETRIEVAL_A2A_URL", "http://spear-retrieval-a2a-gateway.spear-agents.svc.cluster.local:80"
).rstrip("/")

# routes through coordinator-agent-config's rails, not ogx directly; set
# NetworkPolicy via the OGXServer cr's spec.network.policy.ingress, the
# operator reverts a hand patched derived policy, see 01-ogxserver.yaml
OGX_BASE_URL = os.environ.get("OGX_BASE_URL", "https://GUARDRAILS_HOST_PLACEHOLDER")
# dedicated sa's long lived token; openshell sandboxes set
# automountServiceAccountToken: false, see coordinator-guardrails-sa.yaml
OGX_API_KEY = os.environ.get("OGX_API_KEY", "")
GUARDRAILS_CONFIG_ID = os.environ.get("GUARDRAILS_CONFIG_ID", "coordinator-agent-config")
# glm tool calling verified on /v1/chat/completions (finish_reason tool_calls)
GENERATION_MODEL = os.environ.get("GENERATION_MODEL", "openai/publishers/prelude-maas/models/glm-53-flash")
# derived from the env var so the chat trace label never drifts from the
# real model, same last path segment convention as the trend page's shortModelName
GENERATION_MODEL_SHORT = GENERATION_MODEL.rstrip("/").split("/")[-1]

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
    "the user plainly rather than hiding it or trying another way to get the same data. "
    # the chat page renders this answer as markdown, so always write markdown
    # regardless of which model is behind this prompt: headings, bullet lists and
    # tables for the rag citations and work package results, never a plain prose
    # blob, and never raw markdown syntax left unrendered on the page
    "Always format your entire final answer in markdown: use headings (##), bullet "
    "lists, and tables where they make the answer clearer, and keep code or file "
    "names in backticks. The user interface renders markdown, so plain text answers "
    "and unformatted walls of text are never acceptable."
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


def _trace_step(
    trace: list[dict[str, Any]],
    step: str,
    detail: str,
    started_at: float,
    frm: str,
    to: str,
    protocol: str,
    ok: bool = True,
) -> None:
    # real wall clock timing per hop; ok=False marks a genuinely refused or
    # failed hop so the console's timeline renders red at the actual point
    # of failure. monotonic for duration (can't jump back), time.time for
    # the human readable timestamp
    trace.append(
        {
            "step": step,
            "detail": detail,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
            "timestamp_ms": round(time.time() * 1000),
            "from": frm,
            "to": to,
            "protocol": protocol,
            "ok": ok,
        }
    )


@mlflow_tracing.trace(name="chat_completion", span_type=mlflow_tracing.SPAN_TYPE_LLM)
def chat_completion(messages: list[dict[str, Any]]) -> dict[str, Any]:
    # passthrough: true in coordinator-agent-config is required for
    # tools/tool_choice; blocked rails still return 200 with refusal text
    # in message.content, which the decorator captures into the trace too
    headers = {"content-type": "application/json"}
    if OGX_API_KEY:
        headers["authorization"] = f"Bearer {OGX_API_KEY}"
    response = http_json(
        f"{OGX_BASE_URL}/v1/chat/completions",
        {
            "model": GENERATION_MODEL,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "guardrails": {"config_id": GUARDRAILS_CONFIG_ID},
        },
        extra_headers=headers,
    )
    # pull real token counts into their own span attributes, easier to
    # query across traces than digging through the full response body
    span = mlflow_tracing.current_span()
    if span is not None:
        usage = response.get("usage") or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if key in usage:
                span.set_attribute(key, usage[key])
    return response


def _looks_like_tool_error(raw: str) -> bool:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, dict) and "error" in parsed


def delegate_tool_calls(
    tool_calls: list[dict[str, Any]], caller_token: str, trace: list[dict[str, Any]]
) -> dict[str, str]:
    """hands the model's chosen tool calls to spear-retrieval-agent in one
    batch over a2a, returns {tool_call_id: result_text}. the retrieval
    agent is the one that touches rag_chunks and the work tracker tool.
    """
    started_at = time.monotonic()
    names = ", ".join(tc["function"]["name"] for tc in tool_calls)
    calls_payload = [
        {"id": tc["id"], "name": tc["function"]["name"], "arguments": json.loads(tc["function"]["arguments"] or "{}")}
        for tc in tool_calls
    ]

    @mlflow_tracing.trace(name="tool delegation", span_type=mlflow_tracing.SPAN_TYPE_TOOL)
    def _delegate(calls: list[dict[str, Any]]) -> dict[str, Any]:
        # caller_token stays in this closure, never a bound argument;
        # mlflow.trace auto captures bound arguments and must never see it
        payload = {
            "jsonrpc": "2.0",
            "id": "coord-delegate",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "type": "text", "text": json.dumps({"tool_calls": calls})}],
                },
                "callerToken": caller_token,
            },
        }
        # traceparent header stitches retrieval's spans onto this trace
        trace_headers = mlflow_tracing.outgoing_trace_headers()
        body = http_json(RETRIEVAL_A2A_URL, payload, extra_headers=trace_headers)
        result = body.get("result") or {}
        artifacts = result.get("artifacts") or []
        text = artifacts[0]["parts"][0]["text"] if artifacts else json.dumps({"error": body})
        try:
            results = json.loads(text)
            by_id = {r["id"]: r["result"] for r in results if "id" in r}
        except json.JSONDecodeError:
            by_id = {tc["id"]: text for tc in calls}
        # mark this outer hop red too when any tool call failed, so the
        # whole failing chain stands out once retrieval's sub trace is spliced in
        any_tool_error = any(_looks_like_tool_error(v) for v in by_id.values())
        span = mlflow_tracing.current_span()
        if span is not None:
            span.set_attribute("ok", not any_tool_error)
        return {"by_id": by_id, "sub_trace": result.get("trace") or [], "any_tool_error": any_tool_error}

    try:
        outcome = _delegate(calls_payload)
    except Exception as exc:  # noqa: BLE001
        # the a2a hop itself failed, not a tool level refusal; record it
        # as a block rather than crash the whole request with no trace
        _trace_step(
            trace,
            "tool delegation",
            f"blocked, call to spear-retrieval-agent failed: {exc}",
            started_at,
            frm="coordinator-agent (runc sandbox)",
            to="retrieval-agent (kata sandbox)",
            protocol="a2a message/send",
            ok=False,
        )
        return {tc["id"]: json.dumps({"error": str(exc)}) for tc in tool_calls}

    by_id = outcome["by_id"]
    _trace_step(
        trace,
        "tool delegation",
        f"spear-retrieval-agent (kata sandboxed) ran: {names}",
        started_at,
        frm="coordinator-agent (runc sandbox)",
        to="retrieval-agent (kata sandbox)",
        protocol="a2a message/send",
        ok=not outcome["any_tool_error"],
    )
    # splice in retrieval's own sub hops (rag-query-relay, mcp-gateway)
    trace.extend(outcome["sub_trace"])
    return {tc["id"]: by_id.get(tc["id"], json.dumps({"error": "no result returned for this tool call"})) for tc in tool_calls}


# tool refusal can trigger another tool round before a final answer
MAX_TOOL_ROUNDS = 4


def answer(user_text: str, caller_token: str) -> tuple[str, list[dict[str, Any]]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    trace: list[dict[str, Any]] = []

    for round_num in range(MAX_TOOL_ROUNDS):
        started_at = time.monotonic()
        # chat_completion is its own traced span, round number is
        # implicit in the trace tree's ordering
        response = chat_completion(messages)
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []
        guardrails_node = f"guardrails + {GENERATION_MODEL_SHORT}"
        _trace_step(
            trace,
            f"model reasoning, round {round_num + 1}",
            f"{guardrails_node} decided to call tools" if tool_calls else f"{guardrails_node} wrote a final answer",
            started_at,
            frm="coordinator-agent (runc sandbox)",
            to=guardrails_node,
            protocol="openai chat/completions",
        )

        if not tool_calls:
            return message.get("content") or "no answer came back from the model", trace

        # nemo guardrails indexes every message by content unconditionally;
        # a tool call message with no content key blows up the next call
        message.setdefault("content", "")
        messages.append(message)
        results_by_id = delegate_tool_calls(tool_calls, caller_token, trace)
        for tc in tool_calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": results_by_id.get(tc["id"], json.dumps({"error": "no result"})),
                }
            )

    return "the model kept calling tools without giving a final answer, stopping after too many rounds", trace


def on_rpc(body: dict[str, Any], auth_header: str, headers: dict[str, str]) -> dict[str, Any] | None:
    # timestamp for the "request received" step, built by hand below
    received_at_ms = round(time.time() * 1000)
    method = body.get("method")
    # demo only: console's security page proves the workload identity hop
    # by calling retrieval's whoami over this sandbox's own real channel
    if method == "whoami-downstream":
        downstream = http_json(RETRIEVAL_A2A_URL, {"jsonrpc": "2.0", "id": 1, "method": "whoami", "params": {}})
        return completed_task(body.get("id"), json.dumps(downstream))
    if method not in ("message/send", "tasks/send", "tasks/sendSubscribe"):
        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "error": {"code": -32601, "message": f"unsupported method: {method}"},
        }
    # prefer params.callerToken; openshell strips inbound Authorization
    # between caller and sandbox, so the header only works for local testing
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
    # root mlflow span for this request
    @mlflow_tracing.trace(name="coordinator-agent", span_type=mlflow_tracing.SPAN_TYPE_AGENT)
    def _handle(question: str) -> tuple[str, list[dict[str, Any]]]:
        # caller_token stays in this closure, never a traced argument
        return answer(question, caller_token)

    text, trace = _handle(prompt)
    trace.insert(
        0,
        {
            "step": "request received",
            "detail": f"coordinator sandbox {socket.gethostname()} (runc), caller identity already verified by the a2a gateway's own authpolicy",
            "duration_ms": 0,
            "timestamp_ms": received_at_ms,
            "from": "owner-console-backend",
            "to": "coordinator-agent (runc sandbox)",
            "protocol": "a2a message/send",
        },
    )
    return completed_task(body.get("id"), text, trace)


def main() -> None:
    mlflow_tracing.configure()
    print(f"spear-coordinator-agent retrieval_a2a={RETRIEVAL_A2A_URL} model={GENERATION_MODEL}")
    serve(HOST, PORT, on_rpc, agent_card)


if __name__ == "__main__":
    main()

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
import time
import urllib.error
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

# points straight at rag-phase1-guardrails's own route, not at ogx
# directly: this agent's own reasoning call now runs through
# coordinator-agent-config's input/output rails, manifests/spear-guardrails,
# which itself forwards to the real model (its own models: block). ogx's
# NetworkPolicy, confirmed live, is declared straight on the OGXServer
# cr's own spec.network.policy.ingress field rather than hand patching
# the derived NetworkPolicy (which the operator silently reverts), see
# manifests/spear-inference/01-ogxserver.yaml
OGX_BASE_URL = os.environ.get("OGX_BASE_URL", "https://GUARDRAILS_HOST_PLACEHOLDER")
# a dedicated service account's own long lived token, not a dynamic grant:
# openshell sandboxes set automountServiceAccountToken: false, confirmed
# live, so there is no per-pod token to read the way an ordinary
# deployment would, see coordinator-guardrails-sa.yaml's own comment
OGX_API_KEY = os.environ.get("OGX_API_KEY", "")
GUARDRAILS_CONFIG_ID = os.environ.get("GUARDRAILS_CONFIG_ID", "coordinator-agent-config")
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
    # real wall clock timing for one hop of this request, not a fabricated
    # or estimated number. from/to/protocol name the two real components
    # on each side of this hop, owner-console's chat page renders the
    # whole list as a live box and arrow request flow, not just text. ok
    # is false only for a hop that was genuinely refused or failed, so a
    # blocked request renders as a red box at the real point of failure
    # instead of a generic looking green chain all the way through
    trace.append(
        {
            "step": step,
            "detail": detail,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
            "from": frm,
            "to": to,
            "protocol": protocol,
            "ok": ok,
        }
    )


def chat_completion(messages: list[dict[str, Any]]) -> dict[str, Any]:
    # passthrough: true in coordinator-agent-config is what makes tools/
    # tool_choice actually work here, confirmed live: without it nemo
    # guardrails' own /v1/chat/completions rejects any request carrying
    # them outright, "only supported for non-streaming requests when the
    # guardrails configuration has 'passthrough: true'". a blocked rail
    # still fires in passthrough mode, confirmed live too, it comes back
    # as a normal 200 with a refusal message in message.content, same
    # shape as any other answer, nothing special to catch here
    headers = {"content-type": "application/json"}
    if OGX_API_KEY:
        headers["authorization"] = f"Bearer {OGX_API_KEY}"
    return http_json(
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


def _looks_like_tool_error(raw: str) -> bool:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, dict) and "error" in parsed


def delegate_tool_calls(
    tool_calls: list[dict[str, Any]], caller_token: str, trace: list[dict[str, Any]]
) -> dict[str, str]:
    """hands the model's own chosen tool calls to spear-retrieval-agent in
    one batch, over a2a, and returns {tool_call_id: result_text}. the
    retrieval agent is the one that actually touches rag_chunks and the
    work tracker mcp tool, section 2's own isolation rationale, not this
    sandbox.
    """
    started_at = time.monotonic()
    names = ", ".join(tc["function"]["name"] for tc in tool_calls)
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
    try:
        body = http_json(RETRIEVAL_A2A_URL, payload)
    except Exception as exc:  # noqa: BLE001
        # the a2a hop itself failed outright, not a tool level refusal,
        # recorded as its own real block rather than left to crash this
        # whole request with no trace at all
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

    result = body.get("result") or {}
    artifacts = result.get("artifacts") or []
    text = artifacts[0]["parts"][0]["text"] if artifacts else json.dumps({"error": body})
    try:
        results = json.loads(text)
        by_id = {r["id"]: r["result"] for r in results if "id" in r}
    except json.JSONDecodeError:
        by_id = {tc["id"]: text for tc in tool_calls}
    # a per tool call refusal (spear-openproject-mcp's own assignee=self
    # check, or the gateway's own tool rbac authpolicy, see
    # run_work_tracker_tool in spear-retrieval-agent) already renders its
    # own red hop below once retrieval's sub trace is spliced in, marking
    # this outer hop red too so the whole failing sub chain stands out,
    # not just the one deepest box
    any_tool_error = any(_looks_like_tool_error(v) for v in by_id.values())
    _trace_step(
        trace,
        "tool delegation",
        f"spear-retrieval-agent (kata sandboxed) ran: {names}",
        started_at,
        frm="coordinator-agent (runc sandbox)",
        to="retrieval-agent (kata sandbox)",
        protocol="a2a message/send",
        ok=not any_tool_error,
    )
    # retrieval's own completed_task already carries its own real sub
    # hops (rag-query-relay, mcp-gateway), spliced in right after this
    # hop so the flow diagram shows the whole chain, not just this edge
    trace.extend(result.get("trace") or [])
    return {tc["id"]: by_id.get(tc["id"], json.dumps({"error": "no result returned for this tool call"})) for tc in tool_calls}


# a real tool refusal (e.g. spear-openproject-mcp's own assignee=self
# check refusing a write to someone else's work package, confirmed live,
# this never reaches openproject's own rbac at all) often makes the model
# try a second, different tool before it gives up and writes an actual
# answer, a genuine multi round agent loop, not a single question/single
# tool/single answer shape. found live when a real cross user update
# attempt's first tool call came back refused and the model's very next
# move was another tool_calls response, which the old single round
# version here had no way to handle and just returned an empty "no
# synthesized answer" placeholder
MAX_TOOL_ROUNDS = 4


def answer(user_text: str, caller_token: str) -> tuple[str, list[dict[str, Any]]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    trace: list[dict[str, Any]] = []

    for round_num in range(MAX_TOOL_ROUNDS):
        started_at = time.monotonic()
        response = chat_completion(messages)
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []
        _trace_step(
            trace,
            f"model reasoning, round {round_num + 1}",
            "guardrails + glm-53-flash decided to call tools" if tool_calls else "guardrails + glm-53-flash wrote a final answer",
            started_at,
            frm="coordinator-agent (runc sandbox)",
            to="guardrails + glm-53-flash",
            protocol="openai chat/completions",
        )

        if not tool_calls:
            return message.get("content") or "no answer came back from the model", trace

        # nemo guardrails own get_history_cache_key indexes every message
        # by msg["content"] unconditionally, confirmed live via a real
        # KeyError in its pod logs, so a tool call message with no
        # content key at all (the plain openai shape) blows up the next
        # call. append a content key even though it is empty, this is a
        # guardrails quirk not an openai requirement
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


def on_rpc(body: dict[str, Any], auth_header: str) -> dict[str, Any] | None:
    method = body.get("method")
    # demo only method, owner-console's security demo page uses this to
    # prove the workload identity hop live: makes this sandbox's own real
    # outbound call to the retrieval agent's own whoami method, over the
    # exact same http_json(RETRIEVAL_A2A_URL, ...) channel every real tool
    # delegation already uses, confirmed live this is the only way to
    # observe the openshell supervisor's real injected token, it only
    # credentials this sandbox's own declared main process, never an
    # oc exec'd side process making the identical call by hand
    if method == "whoami-downstream":
        downstream = http_json(RETRIEVAL_A2A_URL, {"jsonrpc": "2.0", "id": 1, "method": "whoami", "params": {}})
        return completed_task(body.get("id"), json.dumps(downstream))
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
    text, trace = answer(prompt, caller_token)
    trace.insert(
        0,
        {
            "step": "request received",
            "detail": f"coordinator sandbox {socket.gethostname()} (runc), caller identity already verified by the a2a gateway's own authpolicy",
            "duration_ms": 0,
            "from": "owner-console-backend",
            "to": "coordinator-agent (runc sandbox)",
            "protocol": "a2a message/send",
        },
    )
    return completed_task(body.get("id"), text, trace)


def main() -> None:
    print(f"spear-coordinator-agent retrieval_a2a={RETRIEVAL_A2A_URL} model={GENERATION_MODEL}")
    serve(HOST, PORT, on_rpc, agent_card)


if __name__ == "__main__":
    main()

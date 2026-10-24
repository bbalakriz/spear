#!/usr/bin/env python3
"""spear-retrieval-agent.

executes rag and work tracker tool calls in a kata sandbox. receives a
batch of tool calls from spear-coordinator-agent over a2a, plus the end
user's own jwt (params.callerToken, see _shared/a2a_server.py), then:

- rag_search: decodes clearance_level/dept/team from the jwt and forwards
  them to rag-query-relay, a small unsandboxed wrapper around
  rag_query.py's abac_filtered_search in spear-data. this cluster's
  openshell build has no transparent tcp, so no direct postgres from here
- search_work_packages / update_work_package: forwards the jwt as the
  Authorization header on a tools/call against spear-mcp-gateway

the jwt is read here without a local signature check, same rationale as
spear-openproject-mcp's caller_username(). authorino's a2a gateway
authpolicy checks this hop's own inbound Authorization instead, not
callerToken, that field is invisible to a header based check. the
signature still gets checked for real at the mcp gateway hop, so a
forged token still fails there with a real 401/403.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
import time
import urllib.error
from typing import Any

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from a2a_server import completed_task, serve, text_from_message  # noqa: E402
from http_client import http_json, mcp_json  # noqa: E402
import mlflow_tracing  # noqa: E402

HOST = os.environ.get("AGENT_HOST", "0.0.0.0")
PORT = int(os.environ.get("AGENT_PORT", "8080"))
NAME = os.environ.get("AGENT_NAME", "spear-retrieval-agent")
VERSION = os.environ.get("AGENT_VERSION", "0.1.0")
PUBLIC_URL = os.environ.get("AGENT_PUBLIC_URL", f"http://{NAME}:{PORT}/")

# cluster dns to the istio service plus Host header for HTTPRoute match
MCP_GATEWAY_URL = os.environ.get(
    "MCP_GATEWAY_URL", "http://spear-mcp-gateway-istio.spear-agents.svc.cluster.local:8080/mcp"
)
# no cluster specific fallback; always comes from the sandbox's own --env (MCP_PUBLIC_HOST)
MCP_GATEWAY_HOST_HEADER = os.environ["MCP_GATEWAY_HOST_HEADER"]
# broker prefixes tool names with the mcp server short name, not the bare
# names spear-openproject-mcp's own tools/list returns
MCP_TOOL_PREFIX_MAP = {
    "search_work_packages": "openproject_search_work_packages",
    "update_work_package": "openproject_update_work_package",
}
# resource client id authorino's tool-access-check keys resource_access
# off, from 01-keycloak-realm.yaml's spear-openproject-mcp tool roles client
MCP_SERVER_RESOURCE_KEY = "spear-worktracker/spear-openproject-mcp"
# exact prefixes spear-mcp-guard-proxy/server.py's blocked_result() writes;
# the only way to tell a guard proxy block apart from an assignee=self
# refusal, both come back as a plain isError result otherwise
GUARD_PROXY_BLOCK_PREFIXES = ("tool call blocked by guardrails", "tool response blocked by guardrails")

# http wrapper, not pgvector/ogx directly, no transparent tcp from this sandbox
RAG_QUERY_RELAY_URL = os.environ.get(
    "RAG_QUERY_RELAY_URL", "http://spear-shield-rag-query-relay.spear-data.svc.cluster.local:8080/search"
)

_mcp_initialized = False


def agent_card() -> dict[str, Any]:
    return {
        "name": NAME,
        "description": "executes abac filtered rag search and work tracker mcp tool calls on behalf of the coordinator",
        "url": PUBLIC_URL,
        "version": VERSION,
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "capabilities": {"streaming": False},
        "skills": [
            {
                "id": "spear-shield-retrieval",
                "name": "SPEAR Shield Retrieval",
                "description": "runs abac filtered rag search and work tracker tool calls scoped to the real caller",
                "tags": ["rag", "mcp", "abac"],
                "examples": [],
            }
        ],
    }


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded)


def decode_claims(jwt: str) -> dict[str, Any]:
    parts = jwt.split(".")
    if len(parts) != 3:
        return {}
    try:
        return json.loads(_b64url_decode(parts[1]))
    except (ValueError, UnicodeDecodeError):
        return {}


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
    # same shape as spear-coordinator-agent's own helper, spliced into the
    # coordinator's trace so the chat page shows one unified timeline
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


@mlflow_tracing.trace(name="rag_search", span_type=mlflow_tracing.SPAN_TYPE_RETRIEVER)
def run_rag_search(arguments: dict[str, Any], claims: dict[str, Any], trace: list[dict[str, Any]]) -> str:
    # no bearer token in these arguments, safe to let the decorator auto
    # capture them; run_work_tracker_tool below closes over caller_token instead
    query = str(arguments.get("query", "")).strip()
    if not query:
        return json.dumps({"error": "rag_search needs a query"})
    started_at = time.monotonic()
    response = http_json(
        RAG_QUERY_RELAY_URL,
        {
            "query": query,
            "caller_clearance": claims.get("clearance_level", ""),
            "caller_dept": claims.get("dept", ""),
            "caller_team": claims.get("team"),
            "top_k": 5,
        },
    )
    documents = response.get("documents") or []
    span = mlflow_tracing.current_span()
    if span is not None:
        span.set_attribute("ok", "error" not in response)
        span.set_attribute("clearance", claims.get("clearance_level") or "none")
        span.set_attribute("dept", claims.get("dept") or "none")
    _trace_step(
        trace,
        "rag_search",
        f"abac filtered search, clearance={claims.get('clearance_level') or 'none'}, dept={claims.get('dept') or 'none'}",
        started_at,
        frm="retrieval-agent (kata sandbox)",
        to="rag-query-relay",
        protocol="http post /search",
        ok="error" not in response,
    )
    if "error" in response:
        return json.dumps({"error": response["error"]})
    # trim to corpus chunk_size so the model sees whole chunks, not a short preview
    trimmed = [
        {"doc_id": r["doc_id"], "source": r["source"], "content": r["content"][:2048]}
        for r in documents
    ]
    return json.dumps({"caller": claims.get("preferred_username"), "count": len(trimmed), "documents": trimmed})


def mcp_call(name: str, arguments: dict[str, Any], caller_token: str) -> dict[str, Any]:
    global _mcp_initialized
    headers = {"authorization": f"Bearer {caller_token}", "host": MCP_GATEWAY_HOST_HEADER}
    if not _mcp_initialized:
        mcp_json(
            MCP_GATEWAY_URL,
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": NAME, "version": VERSION},
                },
            },
            extra_headers=headers,
        )
        _mcp_initialized = True
    prefixed = MCP_TOOL_PREFIX_MAP.get(name, name)
    return mcp_json(
        MCP_GATEWAY_URL,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": prefixed, "arguments": arguments}},
        extra_headers=headers,
    )


def run_work_tracker_tool(
    name: str, arguments: dict[str, Any], caller_token: str, trace: list[dict[str, Any]]
) -> str:
    started_at = time.monotonic()
    # held mirrors authorino's own tool-access-check predicate exactly,
    # computed live, not a label chosen to match whichever branch fired
    claims = decode_claims(caller_token)
    roles = (claims.get("resource_access") or {}).get(MCP_SERVER_RESOURCE_KEY, {}).get("roles", [])
    held = f"tool:{name}" in roles

    def _line(label: str, value: str) -> str:
        return f"\n{label:<21}{value}"

    def _evidence(mcp_line: str) -> str:
        rule_outcome = "allowed through the gateway" if held else "denied before the request ever left the gateway"
        return (
            _line("caller claims", f"resource_access[\"{MCP_SERVER_RESOURCE_KEY}\"].roles = {roles}")
            + _line("authorino rule", f"tool-access-check \u00b7 'tool:{name}' in roles = {str(held).lower()} -> {rule_outcome}")
            + _line("mcp server", mcp_line)
        )

    def _guardrails_evidence(stage: str, verdict_text: str) -> str:
        # a response guard block (spear-mcp-guard-proxy/server.py): the
        # tool already ran and returned the malicious text, the
        # assignee=self check never ran, nothing was wrong with who asked
        status_word, _, rail_name = verdict_text.partition("(")
        status_word = status_word.strip()
        rail_name = rail_name.rstrip(")").strip()
        return (
            _line("mcp server", "returned the result, this never reached the assignee=self check at all")
            + _line("guard proxy", f"{stage} guard, work-tracker-config \u00b7 status={status_word}")
            + (_line("nemo rail", f"'{rail_name}'") if rail_name else "")
            + _line("caught", "after spear-openproject-mcp returned the result, before the retrieval agent or the llm ever saw it")
        )

    @mlflow_tracing.trace(name=f"mcp tool call: {name}", span_type=mlflow_tracing.SPAN_TYPE_TOOL)
    def _call(tool_name: str, tool_arguments: dict[str, Any]) -> dict[str, Any]:
        # caller_token stays in this closure, never a bound argument, same
        # rule as delegate_tool_calls' _delegate in the coordinator
        span = mlflow_tracing.current_span()
        # gateway tool rbac raises HTTPError before openproject mcp sees the call
        try:
            response = mcp_call(tool_name, tool_arguments, caller_token)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:300]
            if span is not None:
                span.set_attribute("ok", False)
                span.set_attribute("category", "tool-rbac")
                span.set_outputs({"error": f"http {exc.code}: {body}"})
            return {"kind": "tool-rbac", "detail": f"http {exc.code}: {body}"}
        except Exception as exc:  # noqa: BLE001
            if span is not None:
                span.set_attribute("ok", False)
                span.set_attribute("category", "hop-failure")
                span.set_outputs({"error": str(exc)})
            return {"kind": "hop-failure", "detail": str(exc)}

        result = response.get("result") or {}
        content = result.get("content") or []
        text = content[0].get("text") if content else json.dumps(result)
        # a refusal (assignee=self, server.py) comes back as a normal http
        # 200 with "error" or isError set, same shape the guard proxy uses
        # for its own response block, see GUARD_PROXY_BLOCK_PREFIXES
        blocked = "error" in response or bool(result.get("isError"))
        category = "assignee-self"
        if blocked and result.get("isError") and text.startswith(GUARD_PROXY_BLOCK_PREFIXES):
            category = "guardrails"
        if span is not None:
            span.set_attribute("ok", not blocked)
            if blocked:
                span.set_attribute("category", category)
            # the real mcp result, not just blocked/ok, is what a reviewer needs
            span.set_outputs({"error": response["error"]} if "error" in response else result)
        if "error" in response:
            return {"kind": "assignee-self", "detail": response["error"]}
        if result.get("isError"):
            return {"kind": category, "detail": text}
        return {"kind": "ok", "text": text}

    outcome = _call(name, arguments)
    kind = outcome["kind"]
    if kind == "tool-rbac":
        _trace_step(
            trace,
            f"mcp tool call: {name}",
            f"blocked, mcp gateway's own tool rbac authpolicy returned {outcome['detail']}"
            + _evidence("never reached, this call did not get past authorino"),
            started_at,
            frm="retrieval-agent (kata sandbox)",
            to="mcp-gateway -> spear-openproject-mcp",
            protocol="mcp json-rpc tools/call",
            ok=False,
        )
        return json.dumps({"error": outcome["detail"]})
    if kind == "hop-failure":
        # a real infra failure, not a security decision, so the claims and
        # policy evidence above stays out of this branch
        _trace_step(
            trace,
            f"mcp tool call: {name}",
            f"blocked, call to mcp-gateway failed: {outcome['detail']}",
            started_at,
            frm="retrieval-agent (kata sandbox)",
            to="mcp-gateway -> spear-openproject-mcp",
            protocol="mcp json-rpc tools/call",
            ok=False,
        )
        return json.dumps({"error": outcome["detail"]})
    if kind == "guardrails":
        # the gateway's guard proxy caught this on the way out, a
        # genuinely different mechanism than tool-rbac/assignee-self.
        # detail is "tool response blocked by guardrails, verdict: ...",
        # the evidence line only needs the verdict part
        verdict_text = outcome["detail"].rsplit("verdict:", 1)[-1].strip()
        stage = "response" if outcome["detail"].startswith("tool response") else "call"
        _trace_step(
            trace,
            f"mcp tool call: {name}",
            f"blocked: {outcome['detail']}" + _guardrails_evidence(stage, verdict_text),
            started_at,
            frm="retrieval-agent (kata sandbox)",
            to="mcp-gateway -> spear-openproject-mcp",
            protocol="mcp json-rpc tools/call",
            ok=False,
        )
        return json.dumps({"error": outcome["detail"]})
    blocked = kind == "assignee-self"
    if blocked:
        headline = f"blocked: {outcome['detail']}"
        mcp_line = "server.py:216, assignee_href != caller -> PermissionError"
    elif name == "update_work_package":
        headline = f"allowed, {claims.get('preferred_username')} holds tool:{name} and is the current assignee, status updated"
        mcp_line = "server.py:216, assignee_href == caller -> PATCH proceeds"
    else:
        headline = f"allowed, {claims.get('preferred_username')} holds tool:{name}"
        mcp_line = "no ownership check for this tool, the query itself is already scoped to the caller's own assignments"
    _trace_step(
        trace,
        f"mcp tool call: {name}",
        headline + _evidence(mcp_line),
        started_at,
        frm="retrieval-agent (kata sandbox)",
        to="mcp-gateway -> spear-openproject-mcp",
        protocol="mcp json-rpc tools/call",
        ok=not blocked,
    )
    if blocked:
        return json.dumps({"error": outcome["detail"]})
    return outcome["text"]


def run_tool_call(
    call: dict[str, Any], claims: dict[str, Any], caller_token: str, trace: list[dict[str, Any]]
) -> str:
    name = call.get("name")
    arguments = call.get("arguments") or {}
    try:
        if name == "rag_search":
            return run_rag_search(arguments, claims, trace)
        if name in MCP_TOOL_PREFIX_MAP:
            return run_work_tracker_tool(name, arguments, caller_token, trace)
        return json.dumps({"error": f"unknown tool: {name}"})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})


def on_rpc(body: dict[str, Any], auth_header: str, headers: dict[str, str]) -> dict[str, Any] | None:
    method = body.get("method")
    # demo only: console's security page proves the workload identity hop.
    # openshell strips any caller presented authorization header before
    # it lands here, so this just confirms the call reached us; the real
    # proof is the contrast console-backend builds between a naked outside
    # call (401) and the coordinator's identical call with no token (succeeds)
    if method == "whoami":
        return completed_task(body.get("id"), json.dumps({"reached": True}))
    if method not in ("message/send", "tasks/send", "tasks/sendSubscribe"):
        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "error": {"code": -32601, "message": f"unsupported method: {method}"},
        }
    params = body.get("params") or {}
    caller_token = params.get("callerToken")
    if not caller_token:
        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "error": {"code": -32001, "message": "no callerToken on this a2a request, cannot scope any tool call to a real identity"},
        }
    claims = decode_claims(caller_token)
    prompt = text_from_message(body)
    print(f"retrieval request (sandbox={socket.gethostname()}, caller={claims.get('preferred_username')}): {prompt}")
    try:
        payload = json.loads(prompt)
        calls = payload.get("tool_calls") or []
    except json.JSONDecodeError:
        calls = []
    trace: list[dict[str, Any]] = []
    # headers arrive in whatever case the coordinator's http client sent,
    # case fold rather than trust one exact spelling
    lowered_headers = {k.lower(): v for k, v in headers.items()}
    inbound_trace_headers = {"traceparent": lowered_headers["traceparent"]} if "traceparent" in lowered_headers else {}

    @mlflow_tracing.trace(name="retrieval-agent", span_type=mlflow_tracing.SPAN_TYPE_AGENT)
    def _handle(caller: str, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # caller_token and trace reach run_tool_call only through this
        # closure, never as bound arguments; a bearer token must never land in a trace
        return [{"id": call.get("id"), "result": run_tool_call(call, claims, caller_token, trace)} for call in tool_calls]

    with mlflow_tracing.continue_trace_from_headers(inbound_trace_headers):
        results = _handle(claims.get("preferred_username"), calls)
    return completed_task(body.get("id"), json.dumps(results), trace)


def main() -> None:
    mlflow_tracing.configure()
    print(f"spear-retrieval-agent mcp_gateway={MCP_GATEWAY_URL}")
    serve(HOST, PORT, on_rpc, agent_card)


if __name__ == "__main__":
    main()

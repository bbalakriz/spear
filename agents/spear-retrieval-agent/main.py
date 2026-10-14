#!/usr/bin/env python3
"""spear-retrieval-agent, PHASE2_PLAN.md section 7, steps 3 to 5.

the one that actually touches retrieved chunks and live work package
data, kata isolated for exactly that reason, section 2. receives a
batch of already model chosen tool calls from spear-coordinator-agent
over a2a, plus the real end user's own jwt (params.callerToken, not
its own inbound Authorization header, see _shared/a2a_server.py's own
comment on why), and executes each one for real:

- rag_search: decodes the caller's own clearance_level/dept/team out of
  their real jwt and forwards them to rag-query-relay, a small
  unsandboxed http wrapper around rag_query.py's own abac_filtered_search
  deployed inside spear-data itself, not called directly from in here:
  this cluster's openshell build does not advertise transparent tcp
  support, confirmed live, so the raw postgres connection
  abac_filtered_search needs cannot be made from inside this sandbox at
  all, see rag_query_server.py's own module docstring
- search_work_packages / update_work_package: forwards the caller's own
  jwt as the Authorization header on a real tools/call against
  spear-shield-gateway, the same full path PHASE2_PLAN.md section 4
  already live tested end to end (siddhartha.de's own real tool rbac
  and assignee=self results), this code is just the first thing to
  actually call it as a real a2a delegated action instead of a one off
  curl

honest note on what is, and is not, independently re verified at this
hop: the caller's jwt here is read without a local signature check,
same rationale spear-openproject-mcp's own caller_username() already
documents, extended one hop further. this hop's own inbound
Authorization (the coordinator's workload identity) is what
authorino's a2a gateway authpolicy actually checks, not
params.callerToken, that field is invisible to a header based jwt
check entirely. the callerToken's own signature still gets checked for
real at the next hop, spear-shield-gateway's own tool rbac authpolicy,
exactly the same as if it had been sent there directly, so a forged or
tampered token still fails there with a real 401/403, not silently
accepted here and laundered through.
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

# the gateway's own istio service directly, same dual mechanism
# PHASE2_PLAN.md section 4's own live test used: connect to the service
# by its real cluster dns name, but override the http Host header to
# the gateway's actual listener hostname so its own HTTPRoute matches,
# a plain ClusterIP hit without that header override does not route
# anywhere, confirmed live in that section, not re-guessed here
MCP_GATEWAY_URL = os.environ.get(
    "MCP_GATEWAY_URL", "http://spear-shield-gateway-istio.spear-shield-agents.svc.cluster.local:8080/mcp"
)
# no cluster specific fallback on purpose, nothing in this source tree
# should bake in one cluster's own hostname, this always comes from the
# --env the sandbox was created with (scripts/08-install-spear-shield-agents.sh's
# own resolve_hosts, MCP_PUBLIC_HOST)
MCP_GATEWAY_HOST_HEADER = os.environ["MCP_GATEWAY_HOST_HEADER"]
# the broker prefixes every federated tool name with the registered mcp
# server's own short name, confirmed live in section 4,
# openproject_search_work_packages / openproject_update_work_package,
# not the bare names spear-openproject-mcp's own tools/list returns
MCP_TOOL_PREFIX_MAP = {
    "search_work_packages": "openproject_search_work_packages",
    "update_work_package": "openproject_update_work_package",
}
# the real resource client id authorino's own tool-access-check predicate
# keys resource_access off, straight out of 01-keycloak-realm.yaml's own
# spear-openproject-mcp tool roles client, not re-typed as a guess, this
# is the exact key a caller's own token carries its tool:<name> roles
# under for this one mcp server
MCP_SERVER_RESOURCE_KEY = "spear-worktracker/spear-openproject-mcp"
# the exact prefixes mcp-servers/spear-shield-mcp-guard-proxy/server.py's
# own blocked_result() writes, "tool call blocked by guardrails" for its
# request side check, "tool response blocked by guardrails" for its
# response side one, the one honest way from here to tell a guard proxy
# block apart from spear-openproject-mcp's own assignee=self refusal,
# both come back as a plain isError result, nothing else distinguishes them
GUARD_PROXY_BLOCK_PREFIXES = ("tool call blocked by guardrails", "tool response blocked by guardrails")

# rag-query-relay, not pgvector or ogx directly, see rag_query_server.py's
# own module docstring: this cluster's openshell build does not
# advertise transparent tcp support, so the sandboxed retrieval agent
# cannot open a raw postgres connection itself at all, a plain http
# call to this relay is the one transport the rest protocol policy
# already proves works from inside this sandbox
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
    # same shape as spear-coordinator-agent's own helper, real wall clock
    # timing per hop, spliced into the coordinator's own trace one level
    # up so owner-console's chat page shows the whole chain as one
    # timeline. ok is false only for a hop that was genuinely refused or
    # failed, so the timeline can render exactly that step red instead
    # of leaving the viewer to guess which one actually blocked the
    # request. timestamp_ms is wall clock (time.time()), duration_ms
    # stays on time.monotonic() so it can't be skewed by a clock jump
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
    # no bearer token in this function's own arguments, safe to let the
    # decorator above auto capture them as the span's real input,
    # unlike run_work_tracker_tool below, which closes over caller_token
    # in a nested function instead for exactly that reason
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
    # was content[:800], an arbitrary preview length that happened to cut
    # off before a real chunk's own sensitive content in a live trace,
    # confirmed via rag_chunks directly: every chunk in this corpus is
    # written at a fixed 2048 char size (phase1_apply_pattern's own
    # chunk_size), so trimming to that instead of a shorter guess means
    # the model always sees a whole chunk, never a silent partial one
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
    # decoded once, up front, same helper rag_search already uses below,
    # this is the one real fact every branch's own evidence line needs:
    # what tool roles did the caller's own token actually carry. 'held'
    # mirrors authorino's own tool-access-check predicate exactly, so it
    # is a live computed answer, not a label chosen to match whichever
    # branch the real call happened to land in
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
        # a response guard block, not a request guard one, see
        # mcp-servers/spear-shield-mcp-guard-proxy/server.py: the real
        # spear-openproject-mcp already ran and already returned the
        # real, now malicious, work package text, the assignee=self
        # check never even ran, there was nothing wrong with who asked.
        # verdict_text looks like "blocked (regex check input)", that
        # rail name is nemoguardrails' own name for it, describing the
        # text it was handed, not which side of this proxy called it.
        # the guard proxy line below already states the real side,
        # response guard, so the rail name needs no extra caveat
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
        # caller_token reaches mcp_call only through this closure, never
        # as one of this function's own bound arguments, same rule as
        # delegate_tool_calls' own _delegate in the coordinator: a
        # bearer token must never land in a trace
        span = mlflow_tracing.current_span()
        # the gateway's own per tool rbac authpolicy rejects a disallowed
        # tool call with a real http error status before spear-openproject-mcp
        # ever sees it, which urllib raises as an exception rather than
        # handing back a normal response body, confirmed live for a caller
        # lacking the update_work_package role
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
        # a refusal that reaches this point came back as a normal http 200,
        # spear-openproject-mcp's own assignee=self check for example, see
        # its server.py, "error" in the jsonrpc body or isError on the mcp
        # result are both still a real block even with no exception at all.
        # the gateway's own guard proxy writes this exact same isError
        # shape for its own, entirely different, response guard block, see
        # GUARD_PROXY_BLOCK_PREFIXES above for how this tells them apart
        blocked = "error" in response or bool(result.get("isError"))
        category = "assignee-self"
        if blocked and result.get("isError") and text.startswith(GUARD_PROXY_BLOCK_PREFIXES):
            category = "guardrails"
        if span is not None:
            span.set_attribute("ok", not blocked)
            if blocked:
                span.set_attribute("category", category)
            # the real mcp result, not just whether it was blocked, this
            # is what a reviewer or an eval judge actually needs to see
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
        # a real infra failure, not a security decision, the claims and
        # policy evidence above has nothing to say about a timeout or a
        # dropped connection, so it stays out of this one branch only
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
        # the gateway's own guard proxy caught this on the way out, see
        # _guardrails_evidence above for why this is a response guard
        # block, not the tool-rbac or assignee-self evidence shape, a
        # genuinely different mechanism fired here. outcome['detail'] is
        # the guard proxy's own full sentence, "tool response blocked by
        # guardrails, verdict: ...", the evidence line below only needs
        # the verdict part, not that same sentence twice. the stage word
        # is real too, GUARD_PROXY_BLOCK_PREFIXES's own two literal
        # strings, "tool call ..." for the request guard, "tool
        # response ..." for the response guard, this one is always the
        # latter for a search/update refusal, call never gets this far
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
    # demo only method, owner-console's security demo page uses this to
    # prove the coordinator's workload identity hop live. confirmed this
    # session that reading our own inbound authorization header here is
    # not a useful proof: the openshell a2a router that forwards traffic
    # into this sandbox strips any caller presented authorization header
    # before it lands here, for every caller, not just this one, a sound
    # anti spoofing property since a2a itself has no per hop auth of its
    # own. this handler just confirms the call reached us at all, the
    # actual proof is the contrast owner-console-backend builds: the
    # identical naked call from outside any sandbox gets a clean 401 from
    # the retrieval a2a gateway's own authpolicy, the real coordinator
    # process making the same call with no token of its own succeeds
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
    # headers come in whatever case the coordinator's own http client sent
    # them with, a plain case folded lookup here is cheaper than trusting
    # one exact spelling, same traceparent mlflow's own distributed
    # tracing helper puts on the coordinator's outbound call
    lowered_headers = {k.lower(): v for k, v in headers.items()}
    inbound_trace_headers = {"traceparent": lowered_headers["traceparent"]} if "traceparent" in lowered_headers else {}

    @mlflow_tracing.trace(name="retrieval-agent", span_type=mlflow_tracing.SPAN_TYPE_AGENT)
    def _handle(caller: str, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # caller_token and the mutable trace list reach run_tool_call
        # only through this closure, never as this function's own bound
        # arguments: a bearer token must never land in a trace, and
        # trace is a side output being appended to, not worth logging
        # again as an input
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

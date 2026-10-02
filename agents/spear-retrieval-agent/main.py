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
  deployed inside rag-phase1 itself, not called directly from in here:
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
from typing import Any

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from a2a_server import completed_task, serve, text_from_message  # noqa: E402
from http_client import http_json, mcp_json  # noqa: E402

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
MCP_GATEWAY_HOST_HEADER = os.environ.get(
    "MCP_GATEWAY_HOST_HEADER", "mcp-spear-shield-agents.apps.cluster-rf7lv.dyn.redhatworkshops.io"
)
# the broker prefixes every federated tool name with the registered mcp
# server's own short name, confirmed live in section 4,
# openproject_search_work_packages / openproject_update_work_package,
# not the bare names spear-openproject-mcp's own tools/list returns
MCP_TOOL_PREFIX_MAP = {
    "search_work_packages": "openproject_search_work_packages",
    "update_work_package": "openproject_update_work_package",
}

# rag-query-relay, not pgvector or ogx directly, see rag_query_server.py's
# own module docstring: this cluster's openshell build does not
# advertise transparent tcp support, so the sandboxed retrieval agent
# cannot open a raw postgres connection itself at all, a plain http
# call to this relay is the one transport the rest protocol policy
# already proves works from inside this sandbox
RAG_QUERY_RELAY_URL = os.environ.get(
    "RAG_QUERY_RELAY_URL", "http://spear-shield-rag-query-relay.rag-phase1.svc.cluster.local:8080/search"
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


def run_rag_search(arguments: dict[str, Any], claims: dict[str, Any]) -> str:
    query = str(arguments.get("query", "")).strip()
    if not query:
        return json.dumps({"error": "rag_search needs a query"})
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
    if "error" in response:
        return json.dumps({"error": response["error"]})
    results = response.get("documents") or []
    trimmed = [
        {"doc_id": r["doc_id"], "source": r["source"], "content": r["content"][:800]}
        for r in results
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


def run_work_tracker_tool(name: str, arguments: dict[str, Any], caller_token: str) -> str:
    response = mcp_call(name, arguments, caller_token)
    if "error" in response:
        return json.dumps({"error": response["error"]})
    result = response.get("result") or {}
    content = result.get("content") or []
    text = content[0].get("text") if content else json.dumps(result)
    if result.get("isError"):
        return json.dumps({"error": text})
    return text


def run_tool_call(call: dict[str, Any], claims: dict[str, Any], caller_token: str) -> str:
    name = call.get("name")
    arguments = call.get("arguments") or {}
    try:
        if name == "rag_search":
            return run_rag_search(arguments, claims)
        if name in MCP_TOOL_PREFIX_MAP:
            return run_work_tracker_tool(name, arguments, caller_token)
        return json.dumps({"error": f"unknown tool: {name}"})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})


def on_rpc(body: dict[str, Any], auth_header: str) -> dict[str, Any] | None:
    method = body.get("method")
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
    results = [{"id": call.get("id"), "result": run_tool_call(call, claims, caller_token)} for call in calls]
    return completed_task(body.get("id"), json.dumps(results))


def main() -> None:
    print(f"spear-retrieval-agent mcp_gateway={MCP_GATEWAY_URL}")
    serve(HOST, PORT, on_rpc, agent_card)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""unsandboxed http wrapper around rag_query.py's own embed_query and
abac_filtered_search, deployed inside spear-data itself, not inside the
kata sandboxed retrieval agent.

openshell here has no transparent tcp, so the kata retrieval agent cannot
reach postgres directly. this relay runs embed_query and abac_filtered_search
in a normal pod; retrieval calls it over http.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.append(os.path.dirname(__file__))
from rag_query import abac_filtered_search, connect, embed_query  # noqa: E402

# pod in spear-data; ogx NetworkPolicy must allow that namespace for /v1/embeddings
OGX_BASE_URL = os.environ.get("OGX_BASE_URL", "http://spear-ogx-service.spear-inference.svc.cluster.local:8321")


class Handler(BaseHTTPRequestHandler):
    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/health", "/healthz"):
            self._respond(200, {"status": "ok"})
            return
        self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/search":
            self._respond(404, {"error": "not found"})
            return
        length = int(self.headers.get("content-length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid json"})
            return
        query = str(body.get("query", "")).strip()
        if not query:
            self._respond(400, {"error": "query is required"})
            return
        try:
            embedding = embed_query(query, ogx_base_url=OGX_BASE_URL)
            conn = connect()
            try:
                results = abac_filtered_search(
                    conn,
                    embedding,
                    caller_clearance=body.get("caller_clearance", ""),
                    caller_dept=body.get("caller_dept", ""),
                    caller_team=body.get("caller_team"),
                    top_k=int(body.get("top_k", 5)),
                )
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            self._respond(502, {"error": str(exc)})
            return
        self._respond(200, {"documents": results})


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    print(f"rag query relay listening on 0.0.0.0:{port}, ogx={OGX_BASE_URL}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()

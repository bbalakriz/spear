#!/usr/bin/env python3
"""abac filtered rag query, the retrieval half of spear-retrieval-agent's
own RAG tool, PHASE2_PLAN.md section 3.

checked live before writing this, not assumed either way per that
section's own instruction: OGX's own File Search / Vector Stores api
(confirmed this is llama-stack under the OGX branding, port 8321) only
knows about vector stores it manages itself, 32 of them found live on
this cluster, each backed by its own private "vs_vs_<uuid>" table in
ragdb. rag_chunks, the real abac tagged table phase1_apply_pattern writes
to directly, is a separate hand rolled table outside that abstraction
entirely, never registered as one of OGX's own vector stores, so File
Search has no mechanism to reach it at all. OGX is therefore used here
purely as the embedding model provider, /v1/embeddings only, the honest
fallback that section already called out, not a shortcut.

the pre retrieval ABAC filter itself was reverse checked against
PHASE2_PLAN.md section 6's own worked persona example on the real live
data, not copied verbatim from section 3's looser prose, since a literal
reading of that prose (clearance_level = 'general' alone granting cross
dept visibility) would have let both demo personas see doc-001 (hr,
general), which section 6 explicitly says neither should see. the real
signal distinguishing the one doc both personas do see outside their own
dept (POLICY-DG-042) from the one neither sees despite also being general
(doc-001) is project_scope = 'enterprise-wide', confirmed against the
live rows, not clearance_level alone.
"""

from __future__ import annotations

import argparse
import json
import os

import psycopg2
import requests

OGX_BASE_URL_DEFAULT = "http://rag-phase1-ogx-service.rag-phase1.svc.cluster.local:8321"
# same embedding model and dimension (768) every real row in rag_chunks
# was already written with, phase1_apply_pattern's own default, a query
# embedded with a different model would not even be comparable
EMBEDDING_MODEL = "vllm-embedding/nomic-embed-text-v1.5"

# general is the floor every real persona here has, confirmed live
# against rag_chunks only general/restricted exist today, kept as a rank
# rather than hardcoded to two values so a future confidential/top_secret
# tier slots in without touching the query itself. a clearance_level this
# project does not recognize ranks above everything, fail closed rather
# than open
CLEARANCE_RANK = {"general": 0, "restricted": 1, "confidential": 2, "top_secret": 3}


def allowed_clearance_levels(caller_clearance: str) -> list[str]:
    caller_rank = CLEARANCE_RANK.get(caller_clearance, -1)
    return [level for level, rank in CLEARANCE_RANK.items() if rank <= caller_rank]


def embed_query(text: str, ogx_base_url: str = OGX_BASE_URL_DEFAULT) -> list[float]:
    # same call shape phase1_apply_pattern's own embed_chunks already
    # uses, no auth header, confirmed live that this cluster's internal
    # embeddings route does not require one
    resp = requests.post(
        f"{ogx_base_url}/v1/embeddings",
        json={"model": EMBEDDING_MODEL, "input": [text]},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


def abac_filtered_search(
    conn,
    query_embedding: list[float],
    caller_clearance: str,
    caller_dept: str,
    caller_team: str | None,
    top_k: int = 5,
) -> list[dict]:
    """a chunk is visible only if its own clearance_level ranks at or
    below the caller's, and either the caller's dept or team matches the
    chunk's, or the chunk is explicitly tagged project_scope
    'enterprise-wide'. evaluated in sql before the vector distance
    operator runs, the pre retrieval gate section 3 asks for, not a post
    filter on top of an already run similarity search.
    """
    # same vector literal format pipeline.py's own apply pattern writer
    # already uses when inserting, repr() keeps full float precision
    vector_literal = "[" + ",".join(repr(v) for v in query_embedding) + "]"
    allowed_levels = allowed_clearance_levels(caller_clearance)

    cur = conn.cursor()
    cur.execute(
        """
        SELECT doc_id, chunk_index, content, source, clearance_level, dept,
               team, project_scope, data_owner,
               embedding <-> %(qv)s::vector AS distance
        FROM rag_chunks
        WHERE clearance_level = ANY(%(allowed_levels)s)
          AND (dept = %(dept)s OR team = %(team)s OR project_scope = 'enterprise-wide')
        ORDER BY embedding <-> %(qv)s::vector
        LIMIT %(top_k)s
        """,
        {
            "qv": vector_literal,
            "allowed_levels": allowed_levels,
            "dept": caller_dept,
            "team": caller_team,
            "top_k": top_k,
        },
    )
    columns = [c.name for c in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def connect():
    # same psycopg2 connection pattern phase1_apply_pattern/pipeline.py
    # already uses against pgvector-creds
    # PGVECTOR_DB_PORT, not PGVECTOR_PORT: found live, kubernetes auto
    # injects PGVECTOR_PORT itself (the legacy service link env var,
    # "tcp://<ip>:5432") for any pod in a namespace with a service
    # literally named pgvector, colliding with a plain port number here
    return psycopg2.connect(
        host=os.environ.get("PGVECTOR_HOST", "pgvector.rag-phase1.svc.cluster.local"),
        port=int(os.environ.get("PGVECTOR_DB_PORT", "5432")),
        dbname=os.environ.get("PGVECTOR_DB", "ragdb"),
        user=os.environ["PGVECTOR_USER"],
        password=os.environ["PGVECTOR_PASSWORD"],
    )


def main() -> None:
    # manual test harness for now, spear-retrieval-agent's own server
    # will call embed_query/abac_filtered_search directly once it exists
    parser = argparse.ArgumentParser(description="abac filtered rag query, manual test harness")
    parser.add_argument("question")
    parser.add_argument("--clearance", required=True)
    parser.add_argument("--dept", required=True)
    parser.add_argument("--team", default=None)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    embedding = embed_query(args.question)
    conn = connect()
    try:
        results = abac_filtered_search(conn, embedding, args.clearance, args.dept, args.team, args.top_k)
    finally:
        conn.close()
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()

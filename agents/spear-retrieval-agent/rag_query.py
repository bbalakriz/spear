#!/usr/bin/env python3
"""abac filtered rag query for spear-retrieval-agent rag_search.

OGX File Search / Vector Stores api
(confirmed this is llama-stack under the OGX branding, port 8321) only
knows about vector stores it manages itself, 32 of them found live on
this cluster, each backed by its own private "vs_vs_<uuid>" table in
ragdb. rag_chunks, the real abac tagged table phase1_apply_pattern writes
to directly, is a separate hand rolled table outside that abstraction
entirely, never registered as one of OGX's own vector stores, so File
Search has no mechanism to reach it at all. OGX is used here purely as
the embedding model provider, /v1/embeddings only.

abac filter uses clearance rank plus dept/team and project_scope enterprise wide,
not clearance_level alone (see persona examples in docs).

the filter lives once, on rag_chunks itself
(pipelines/corpus_index/pipeline.py, the schema owner). this
module does not reimplement that WHERE clause. it only maps the caller's
claims into the three session variables the policy reads. rag_reader has
no bypassrls, so postgres only ever hands back a row the policy allows,
no matter what sql runs against it. a mistake here now fails closed
(postgres returns nothing) not open (postgres returns everything).
"""

from __future__ import annotations

import argparse
import json
import os

import psycopg2
import requests

OGX_BASE_URL_DEFAULT = "http://spear-ogx-service.spear-inference.svc.cluster.local:8321"
# same embedding model and dimension (768) every real row in rag_chunks
# was already written with, phase1_apply_pattern's own default, a query
# embedded with a different model would not even be comparable
EMBEDDING_MODEL = "vllm-embedding/nomic-embed-text-v1.5"

# clearance as rank; unknown levels fail closed
CLEARANCE_RANK = {"general": 0, "restricted": 1, "confidential": 2, "top_secret": 3}


def allowed_clearance_levels(caller_clearance: str) -> list[str]:
    caller_rank = CLEARANCE_RANK.get(caller_clearance, -1)
    return [level for level, rank in CLEARANCE_RANK.items() if rank <= caller_rank]


def embed_query(text: str, ogx_base_url: str = OGX_BASE_URL_DEFAULT) -> list[float]:
    # same shape as corpus_index embed_chunks; internal route needs no auth header
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
    """a chunk is visible only if rag_chunks' own row level security
    policy says so, a row only ever comes back if its clearance_level
    ranks at or below the caller's, and either the caller's dept or team
    matches the chunk's, or the chunk is tagged project_scope
    'enterprise-wide', the exact same rule this function used to run as
    a WHERE clause, now postgres' own job, not this query's.

    this function's own job is resolving the caller's claims into the
    three session variables that policy reads, set_local scoped (the
    third set_config argument), so a value here never outlives this one
    request's own transaction on a connection some later caller reuses.
    """
    # same vector literal format pipeline.py's own apply pattern writer
    # already uses when inserting, repr() keeps full float precision
    vector_literal = "[" + ",".join(repr(v) for v in query_embedding) + "]"
    allowed_levels = allowed_clearance_levels(caller_clearance)

    cur = conn.cursor()
    # set_config, not a hand formatted SET LOCAL string: dept/team come
    # straight from the caller's own jwt claims, this keeps them real
    # bind parameters instead of text spliced into sql even here
    cur.execute("SELECT set_config('app.allowed_levels', %s, true)", (",".join(allowed_levels),))
    cur.execute("SELECT set_config('app.dept', %s, true)", (caller_dept or "",))
    cur.execute("SELECT set_config('app.team', %s, true)", (caller_team or "",))
    cur.execute(
        """
        SELECT doc_id, chunk_index, content, source, clearance_level, dept,
               team, project_scope, data_owner,
               embedding <-> %(qv)s::vector AS distance
        FROM rag_chunks
        ORDER BY embedding <-> %(qv)s::vector
        LIMIT %(top_k)s
        """,
        {"qv": vector_literal, "top_k": top_k},
    )
    columns = [c.name for c in cur.description]
    rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    # the set_config calls above and this select have to share one
    # transaction for the "local" scoping to mean anything, commit here
    # rather than leaving it to the caller, same read only connection
    # either way, nothing this ever needs to roll back
    conn.commit()
    return rows


def connect():
    # same psycopg2 connection pattern corpus_index/pipeline.py
    # already uses against pgvector-creds
    # PGVECTOR_DB_PORT, not PGVECTOR_PORT: found live, kubernetes auto
    # injects PGVECTOR_PORT itself (the legacy service link env var,
    # "tcp://<ip>:5432") for any pod in a namespace with a service
    # literally named pgvector, colliding with a plain port number here
    return psycopg2.connect(
        host=os.environ.get("PGVECTOR_HOST", "pgvector.spear-data.svc.cluster.local"),
        port=int(os.environ.get("PGVECTOR_DB_PORT", "5432")),
        dbname=os.environ.get("PGVECTOR_DB", "ragdb"),
        user=os.environ["PGVECTOR_USER"],
        password=os.environ["PGVECTOR_PASSWORD"],
    )


def main() -> None:
    # manual test harness; main.py calls embed_query/abac_filtered_search directly
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

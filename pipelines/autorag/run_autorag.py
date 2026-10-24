"""triggers a real run of documents-rag-optimization-pipeline, AutoRAG's
own kfp pipeline, rhoai auto registers it on every dspa pipeline server,
rather than reimplementing autorag's dashboard flow ourselves.

mirrors corpus_prepare/compile_and_run.py's auth pattern (caller's own
`oc whoami -t` token) but does not compile or upload anything, this
pipeline already exists on spear-dspa.

parameter names come from this pipeline's live registered input
definitions (see inspect_params.py), not the compiled ir pulled from
github, that copy's parameter shape (llama_stack_secret_name etc) is
older than what this cluster's registered version actually expects
(ogx_secret_name, vector_io_provider_id, embedding_models). always
trust the live registration over any external copy.

parameters point at: sanitized/ in raw_bucket (the sanitized only
boundary sanitize_documents enforces), the eval dataset
generate_eval_dataset wrote to autorag-eval/<batch_id>_test_data.json,
the pgvector vector_io provider on spear-ogx, and both chat models plus
the embedding model using ogx /v1/models ids directly, not sdg_hub's
litellm double prefix, autorag talks to ogx by its own client.

usage: source .venv-kfp/bin/activate && python run_autorag.py [batch_id]
"""
import subprocess
import sys
import time

from kfp.client import Client

AUTORAG_PIPELINE_NAME = "documents-rag-optimization-pipeline"
EXPERIMENT_NAME = "phase1-autorag"

RAW_BUCKET = "rag-documents"
SANITIZED_PREFIX = "sanitized/"
EVAL_PREFIX = "autorag-eval/"
S3_SECRET_NAME = "autorag-s3-creds"
OGX_SECRET_NAME = "autorag-ogx-creds"
VECTOR_IO_PROVIDER_ID = "pgvector"

# ogx /v1/models ids, registered not merely routable; embedding is nomic
# embed v1.5 (v2 moe capped at 512 tokens). GAMOptimizer may sample one
# foundation model for all patterns in a short run
GENERATION_MODELS = [
    "vllm-inference/Qwen3.6-35B-A3B",
    "openai/publishers/prelude-maas/models/glm-53-flash",
]
# ogx exposes one embedding model id per config (not two in one pass)
EMBEDDING_MODELS = ["vllm-embedding/nomic-embed-text-v1.5"]

# balanced preset, 8 patterns, faithfulness metric
MAX_RAG_PATTERNS = 8
PRESET = "balanced"
OPTIMIZATION_METRIC = "faithfulness"

# this is a multi pattern optimization sweep, not meant to finish inside
# one interactive shell call, hands control back to the caller after this
POLL_SECONDS = 90
POLL_INTERVAL = 10


def current_token() -> str:
    return subprocess.run(
        ["oc", "whoami", "-t"], check=True, capture_output=True, text=True
    ).stdout.strip()


def dspa_route() -> str:
    # resolved live, this route hostname is unique per cluster, never hardcode it
    host = subprocess.run(
        ["oc", "get", "route", "ds-pipeline-spear-dspa", "-n", "spear-pipelines",
         "-o", "jsonpath={.spec.host}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return f"https://{host}"


def main() -> None:
    batch_id = sys.argv[1] if len(sys.argv) > 1 else "seed-batch-001"
    test_data_key = f"{EVAL_PREFIX}{batch_id}_test_data.json"

    route = dspa_route()
    client = Client(host=route, existing_token=current_token())

    existing = client.list_pipelines(
        filter=(
            '{"predicates":[{"key":"name","operation":"EQUALS",'
            f'"stringValue":"{AUTORAG_PIPELINE_NAME}"}}]}}'
        )
    )
    pipelines = existing.pipelines or []
    if not pipelines:
        raise RuntimeError(
            f"{AUTORAG_PIPELINE_NAME} not found on {route}. rhoai "
            "docs say this is auto registered whenever the pipeline "
            "server starts, so either this dspa predates that "
            "registration or the pipeline server needs a restart."
        )
    pipeline_id = pipelines[0].pipeline_id
    versions = client.list_pipeline_versions(pipeline_id).pipeline_versions or []
    if not versions:
        raise RuntimeError(f"{AUTORAG_PIPELINE_NAME} has no versions registered")
    version_id = versions[0].pipeline_version_id
    print(f"found {AUTORAG_PIPELINE_NAME}: pipeline_id={pipeline_id} version_id={version_id}")

    experiment = client.create_experiment(name=EXPERIMENT_NAME)

    params = {
        "embedding_models": EMBEDDING_MODELS,
        "generation_models": GENERATION_MODELS,
        "input_data_bucket_name": RAW_BUCKET,
        "input_data_key": SANITIZED_PREFIX,
        "input_data_secret_name": S3_SECRET_NAME,
        "ogx_secret_name": OGX_SECRET_NAME,
        "vector_io_provider_id": VECTOR_IO_PROVIDER_ID,
        "optimization_max_rag_patterns": MAX_RAG_PATTERNS,
        "optimization_metric": OPTIMIZATION_METRIC,
        "preset": PRESET,
        "test_data_bucket_name": RAW_BUCKET,
        "test_data_key": test_data_key,
        "test_data_secret_name": S3_SECRET_NAME,
    }
    print("run parameters:")
    for k, v in params.items():
        print(f"  {k} = {v}")

    run = client.run_pipeline(
        experiment_id=experiment.experiment_id,
        job_name=f"{AUTORAG_PIPELINE_NAME}-{batch_id}",
        pipeline_id=pipeline_id,
        version_id=version_id,
        params=params,
    )
    print(f"started run {run.run_id}")

    # poll briefly, just enough to confirm it is genuinely progressing
    # rather than failing immediately, not a full wait for completion:
    # a real optimization sweep across multiple rag patterns can run for
    # a long time.
    deadline = time.time() + POLL_SECONDS
    last_state = None
    while time.time() < deadline:
        current = client.get_run(run.run_id)
        state = current.state
        if state != last_state:
            print(f"  state: {state}")
            last_state = state
        if state in ("FAILED", "ERROR"):
            raise RuntimeError(f"run {run.run_id} entered state {state} during the initial poll window")
        if state in ("SUCCEEDED", "SKIPPED"):
            break
        time.sleep(POLL_INTERVAL)

    print(f"\nrun {run.run_id} is genuinely progressing (last observed state: {last_state}).")
    print(f"watch it in the dashboard under Experiments > {EXPERIMENT_NAME}, or resume polling with:")
    print(
        f"  python -c \"from kfp.client import Client; "
        f"c = Client(host='{route}', existing_token='<token>'); "
        f"print(c.get_run('{run.run_id}').state)\""
    )
    print("leaderboard/winning pattern will land under this run's own artifacts once it finishes.")


if __name__ == "__main__":
    main()

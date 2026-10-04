"""compiles phase1-apply-pattern-pipeline.yaml then uploads and triggers
one run against rag-phase1-dspa, same auth pattern as
phase1_ingestion/compile_and_run.py (the caller's own `oc whoami -t`
token, no separate pipeline server credential).

this script does not look up the winning pattern itself: minio's s3 api
is only reachable in cluster (confirmed live, this cluster has no
external route for it, only the console ui route), and this script runs
from a caller's own machine. the owner console backend already runs in
cluster and already does this exact lookup for real, in
autorag_leaderboard() (owner-console-backend/server.py), so the settings
below are meant to be copied from that endpoint's own response
(GET /kfp/autorag-leaderboard?run_id=<autorag_run_id> through the
console plugin proxy) rather than re implemented here against an
endpoint this script cannot reach.

usage: source .venv-kfp/bin/activate && python compile_and_run.py \
    <batch_id> <autorag_run_id> <pattern_name> <chunk_size> <chunk_overlap> <embedding_model>

a bare `python compile_and_run.py --register-only` compiles and uploads
the pipeline definition without triggering a run, no batch_id/pattern
needed for that half. scripts/04-install-pipelines.sh calls this mode so
the pipeline is registered on rag-phase1-dspa as part of the normal
install chain, before scripts/90-verify.sh ever checks for it, same
registered-but-not-run state documents-rag-optimization-pipeline is
already in right after rhoai auto registers it. an actual run still
needs a real batch_id and a real winning pattern from a completed
autorag run, copied from the owner console's own autorag leaderboard
response, see the module docstring above, and stays a manual step.
"""
import subprocess
import sys
from pathlib import Path

from kfp import compiler
from kfp.client import Client

sys.path.insert(0, str(Path(__file__).parent))
from pipeline import phase1_apply_pattern_pipeline  # noqa: E402

HERE = Path(__file__).parent
PIPELINE_YAML = HERE / "phase1-apply-pattern-pipeline.yaml"
PIPELINE_NAME = "phase1-apply-pattern-pipeline"
EXPERIMENT_NAME = "phase1-apply-pattern"


def current_token() -> str:
    return subprocess.run(
        ["oc", "whoami", "-t"], check=True, capture_output=True, text=True
    ).stdout.strip()


def route_host(name: str, namespace: str) -> str:
    return subprocess.run(
        ["oc", "get", "route", name, "-n", namespace, "-o", "jsonpath={.spec.host}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def dspa_route() -> str:
    return f"https://{route_host('ds-pipeline-rag-phase1-dspa', 'spear-pipelines')}"


def mlflow_url() -> str:
    return subprocess.run(
        ["oc", "get", "mlflow", "mlflow", "-n", "redhat-ods-applications",
         "-o", "jsonpath={.status.url}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def register() -> tuple[Client, str, str]:
    """compiles and uploads the pipeline definition, no run triggered.
    safe to call with no batch/pattern data at all, idempotent, a rerun
    against an already registered pipeline just uploads a new version."""
    compiler.Compiler().compile(phase1_apply_pattern_pipeline, str(PIPELINE_YAML))
    print(f"compiled {PIPELINE_YAML.name}")

    route = dspa_route()
    client = Client(host=route, existing_token=current_token())

    existing = client.list_pipelines(filter=f'{{"predicates":[{{"key":"name","operation":"EQUALS","stringValue":"{PIPELINE_NAME}"}}]}}')
    pipelines = existing.pipelines or []
    if pipelines:
        pipeline_id = pipelines[0].pipeline_id
        existing_versions = client.list_pipeline_versions(pipeline_id).pipeline_versions or []
        version = client.upload_pipeline_version(
            pipeline_package_path=str(PIPELINE_YAML),
            pipeline_id=pipeline_id,
            pipeline_version_name=f"{PIPELINE_NAME}-v{len(existing_versions)}",
        )
        version_id = version.pipeline_version_id
        print(f"uploaded new version {version_id} of existing pipeline {pipeline_id}")
    else:
        uploaded = client.upload_pipeline(pipeline_package_path=str(PIPELINE_YAML), pipeline_name=PIPELINE_NAME)
        pipeline_id = uploaded.pipeline_id
        default_versions = client.list_pipeline_versions(pipeline_id).pipeline_versions or []
        version_id = default_versions[0].pipeline_version_id
        print(f"uploaded new pipeline {pipeline_id}, version {version_id}")

    return client, pipeline_id, version_id


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "--register-only":
        register()
        return

    if len(sys.argv) != 7:
        print(
            f"usage: python {sys.argv[0]} <batch_id> <autorag_run_id> "
            "<pattern_name> <chunk_size> <chunk_overlap> <embedding_model>\n"
            f"   or: python {sys.argv[0]} --register-only"
        )
        sys.exit(1)
    batch_id, autorag_run_id, pattern_name = sys.argv[1], sys.argv[2], sys.argv[3]
    chunk_size, chunk_overlap, embedding_model = int(sys.argv[4]), int(sys.argv[5]), sys.argv[6]

    client, pipeline_id, version_id = register()

    experiment = client.create_experiment(name=EXPERIMENT_NAME)
    run = client.run_pipeline(
        experiment_id=experiment.experiment_id,
        job_name=f"{PIPELINE_NAME}-{batch_id}",
        pipeline_id=pipeline_id,
        version_id=version_id,
        params={
            "mlflow_url": mlflow_url(),
            "batch_id": batch_id,
            "pattern_name": pattern_name,
            "autorag_run_id": autorag_run_id,
            "embedding_model": embedding_model,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
        },
    )
    print(f"started run {run.run_id}, watch in the dashboard under Experiments > {EXPERIMENT_NAME}")


if __name__ == "__main__":
    main()

"""compiles phase1-ingestion-pipeline.yaml then uploads and triggers one
run against the rag-phase1-dspa pipeline server in spear-pipelines, using the caller's own
`oc whoami -t` token exactly like every other in cluster auth check in
this project, no separate pipeline server credential to manage.

usage: source .venv-kfp/bin/activate && python compile_and_run.py
"""
import subprocess
import sys
from pathlib import Path

from kfp import compiler
from kfp.client import Client

sys.path.insert(0, str(Path(__file__).parent))
from pipeline import phase1_ingestion_pipeline  # noqa: E402

HERE = Path(__file__).parent
PIPELINE_YAML = HERE / "phase1-ingestion-pipeline.yaml"
PIPELINE_NAME = "phase1-ingestion-pipeline"
EXPERIMENT_NAME = "phase1-ingestion"


def current_token() -> str:
    return subprocess.run(
        ["oc", "whoami", "-t"], check=True, capture_output=True, text=True
    ).stdout.strip()


def route_host(name: str, namespace: str) -> str:
    # resolved live, route hostnames are unique per cluster, never hardcode one
    return subprocess.run(
        ["oc", "get", "route", name, "-n", namespace, "-o", "jsonpath={.spec.host}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def dspa_route() -> str:
    return f"https://{route_host('ds-pipeline-rag-phase1-dspa', 'spear-pipelines')}"


def guardrails_route() -> str:
    return f"https://{route_host('rag-phase1-guardrails', 'spear-guardrails')}"


def mlflow_url() -> str:
    # the mlflow cr reports its own external url directly (already includes
    # the /mlflow path suffix), same source scripts/04-install-mlflow.sh
    # already trusts, more portable than guessing at the route name behind it
    return subprocess.run(
        ["oc", "get", "mlflow", "mlflow", "-n", "redhat-ods-applications",
         "-o", "jsonpath={.status.url}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def main() -> None:
    compiler.Compiler().compile(phase1_ingestion_pipeline, str(PIPELINE_YAML))
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

    experiment = client.create_experiment(name=EXPERIMENT_NAME)
    run = client.run_pipeline(
        experiment_id=experiment.experiment_id,
        job_name=f"{PIPELINE_NAME}-seed-run",
        pipeline_id=pipeline_id,
        version_id=version_id,
        params={
            "guardrails_route": guardrails_route(),
            "mlflow_url": mlflow_url(),
        },
    )
    print(f"started run {run.run_id}, watch with:")
    print(f"  python -c \"from kfp.client import Client; Client(host='{route}', existing_token='<token>').wait_for_run_completion('{run.run_id}', timeout=1800)\"")
    print(f"or in the dashboard under Experiments > {EXPERIMENT_NAME}")


if __name__ == "__main__":
    main()

# logs one already completed evalhub job's real results into mlflow, the
# missing half of architecture.html's own "EvalCard auto generated in
# MLflow, zero extra work" promise for providers that have no native save
# path of their own reachable from the real job submission schema.
#
# ibm-clear does have one now, confirmed live once this cluster's mlflow
# was switched to serveArtifacts: true, its own parameters.mlflow_experiment_name
# makes the provider itself create the run and upload its real clear html
# report as a proper artifact, nothing this script does could improve on
# that, so when a benchmark's own result already carries a real
# mlflow_run_id (set by that native path), this script does not create a
# second run, it only tags the existing one into this cycle's parent.
#
# ragas has no equivalent, confirmed by reading the real evalhub api
# server's own compiled binary strings, neither "experiment_name" nor
# "mlflow" appears anywhere in it, the go server only ever forwards
# "parameters" as an opaque map, it has no concept of either field by
# name. ibm-clear's own fallback works purely because its own adapter code
# happens to read "mlflow_experiment_name" back out of that same opaque
# parameters map, ragas's own adapter never looks there, only at
# job_spec.experiment_name directly, a field nothing in the real request
# path can ever populate. so ragas keeps using the full run creation below,
# not a workaround, the only reachable option
#
# reads the job's own result from a local file already fetched by the
# caller (no evalhub api call from inside this pod, that result is already
# in hand by the time this runs)
import json
import os

import mlflow
from mlflow.tracking import MlflowClient
from mlflow.tracking.request_header.abstract_request_header_provider import (
    RequestHeaderProvider,
)
from mlflow.tracking.request_header.registry import _request_header_provider_registry

MLFLOW_URL = os.environ["MLFLOW_URL"]
WORKSPACE = os.environ.get("MLFLOW_WORKSPACE", "spear-pipelines")
EXPERIMENT_NAME = os.environ.get("MLFLOW_EVAL_EXPERIMENT", "spear-shield-evalhub-results")
RESULT_PATH = os.environ.get("RESULT_PATH", "/data/result.json")
# the run created once per script invocation by create_eval_cycle_run.py,
# empty means no grouping requested, each run stays a flat top level entry
PARENT_RUN_ID = os.environ.get("PARENT_RUN_ID", "")


class WorkspaceHeader(RequestHeaderProvider):
    def in_context(self):
        return True

    def request_headers(self):
        # the real constant mlflow's own rest_utils.WORKSPACE_HEADER_NAME
        # uses is this exact casing, all caps mlflow, confirmed live
        # reading the installed source. this registration has actually
        # been a no-op the whole time this script worked, this job's own
        # MLFLOW_WORKSPACE env var below already feeds the same value
        # through mlflow's own env fallback, masking the wrong casing
        # entirely, found while chasing the same bug for real in
        # evalhub/spear-confidential-data-provider/adapter.py, where no
        # such masking env var existed and it broke visibly
        return {"X-MLFLOW-WORKSPACE": WORKSPACE}


_request_header_provider_registry.register(WorkspaceHeader)
mlflow.set_tracking_uri(MLFLOW_URL)
mlflow.set_experiment(EXPERIMENT_NAME)
client = MlflowClient()

with open(RESULT_PATH) as fh:
    job = json.load(fh)

job_name = job.get("name", "unknown-eval-job")
job_id = job.get("resource", {}).get("id", "unknown")
model_name = job.get("model", {}).get("name", "")

for bench in job.get("results", {}).get("benchmarks", []):
    provider_id = bench.get("provider_id", "")
    benchmark_id = bench.get("id", "")
    metrics = bench.get("metrics", {})
    test = bench.get("test", {})
    native_run_id = bench.get("mlflow_run_id")

    if native_run_id:
        # the provider's own save already happened, including any real
        # artifacts it carries, just fold it into this cycle
        if PARENT_RUN_ID:
            client.set_tag(native_run_id, "mlflow.parentRunId", PARENT_RUN_ID)
        client.set_tag(native_run_id, "evalhub.job_id", job_id)
        client.set_tag(native_run_id, "evalhub.job_name", job_name)
        print(
            f"linked existing native run {native_run_id} for {benchmark_id} "
            f"({provider_id}) under parent {PARENT_RUN_ID or '(none)'}"
        )
        continue

    tags = {
        "evalhub.job_id": job_id,
        "evalhub.job_name": job_name,
        "evalhub.provider_id": provider_id,
        "evalhub.benchmark_id": benchmark_id,
    }
    if PARENT_RUN_ID:
        tags["mlflow.parentRunId"] = PARENT_RUN_ID

    with mlflow.start_run(run_name=f"{job_name}-{benchmark_id}"):
        mlflow.set_tags(tags)
        mlflow.log_params(
            {
                "model": model_name,
                "primary_score_metric": test.get("primary_score_metric", ""),
                "threshold": test.get("threshold", ""),
            }
        )
        # only numeric metrics: a couple of ibm-clear's own fields
        # (interactions_no_issues etc) are plain counts, mlflow.log_metric
        # accepts those fine too, nothing here is excluded on purpose
        for name, value in metrics.items():
            if isinstance(value, (int, float)):
                mlflow.log_metric(name, float(value))
        mlflow.log_metric("primary_score", float(test.get("primary_score", 0.0)))
        mlflow.log_metric("pass", 1.0 if test.get("pass") else 0.0)

        print(f"created run for {benchmark_id} ({provider_id}) for job {job_name} in mlflow experiment {EXPERIMENT_NAME}")

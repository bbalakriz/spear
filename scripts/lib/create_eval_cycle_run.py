# parent mlflow run for one evalhub cycle; prints RUN_ID= on stdout
import datetime
import os

import mlflow
from mlflow.tracking.request_header.abstract_request_header_provider import (
    RequestHeaderProvider,
)
from mlflow.tracking.request_header.registry import _request_header_provider_registry

MLFLOW_URL = os.environ["MLFLOW_URL"]
WORKSPACE = os.environ.get("MLFLOW_WORKSPACE", "spear-pipelines")
EXPERIMENT_NAME = os.environ.get("MLFLOW_EVAL_EXPERIMENT", "spear-shield-evalhub-results")


class WorkspaceHeader(RequestHeaderProvider):
    def in_context(self):
        return True

    def request_headers(self):
        # workspace header casing must match mlflow client constant
        return {"X-MLFLOW-WORKSPACE": WORKSPACE}


_request_header_provider_registry.register(WorkspaceHeader)
mlflow.set_tracking_uri(MLFLOW_URL)
mlflow.set_experiment(EXPERIMENT_NAME)

run_name = "eval-cycle-" + datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
with mlflow.start_run(run_name=run_name) as run:
    mlflow.set_tag("evalhub.cycle", "true")
    run_id = run.info.run_id

# mlflow's own client prints "View run"/"View experiment" lines when the
# run context exits, after this script's own prints, so the caller can't
# just grep the last line, a fixed marker prefix is the reliable part
print(f"RUN_ID={run_id}")

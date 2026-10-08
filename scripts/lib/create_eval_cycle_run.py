# creates one parent mlflow run per invocation of 11-submit-evalhub-jobs.sh
# and prints its run id on stdout. every real benchmark run from this
# cycle, ragas, ibm-clear, later garak, gets tagged mlflow.parentRunId
# against this, so mlflow's own run list groups them as one evaluation
# cycle instead of unrelated flat rows. confirmed this tag is all mlflow
# nesting needs, the parent does not need to stay open in this same
# process, every child job runs in its own later pod
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

run_name = "eval-cycle-" + datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
with mlflow.start_run(run_name=run_name) as run:
    mlflow.set_tag("evalhub.cycle", "true")
    run_id = run.info.run_id

# mlflow's own client prints "View run"/"View experiment" lines when the
# run context exits, after this script's own prints, so the caller can't
# just grep the last line, a fixed marker prefix is the reliable part
print(f"RUN_ID={run_id}")

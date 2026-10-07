# pulls a small real ragas dataset (user_input, response, retrieved_contexts)
# straight out of spear-shield-security-trace's own mlflow traces. runs as
# the init container of the job scripts/11-submit-evalhub-jobs.sh builds,
# a plain pod talking to mlflow's own internal service address directly,
# no agent sandbox involved, mlflow is just a normal server the same way
# evalhub's own job pods already read it. only needs a real mlflow sa
# bearer token (MLFLOW_TRACKING_TOKEN), the real service ca bundle
# (SSL_CERT_FILE/REQUESTS_CA_BUNDLE, a self signed cert otherwise), and
# the workspace header mlflow.kubeflow.org experiments rbac is scoped to.
import json
import os

import mlflow
from mlflow.tracking.request_header.abstract_request_header_provider import (
    RequestHeaderProvider,
)
from mlflow.tracking.request_header.registry import _request_header_provider_registry

MLFLOW_URL = os.environ["MLFLOW_URL"]
WORKSPACE = os.environ.get("MLFLOW_WORKSPACE", "spear-pipelines")
EXPERIMENT_NAME = os.environ.get("MLFLOW_EXPERIMENT", "spear-shield-security-trace")
# shared emptyDir mount in the job, not /tmp: the mc container next to this
# one in the same pod needs to read this file back out after this
# container exits, a plain /tmp would only ever live inside this one
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "/tmp/ragas_dataset.jsonl")


class WorkspaceHeader(RequestHeaderProvider):
    def in_context(self):
        return True

    def request_headers(self):
        return {"X-MLflow-Workspace": WORKSPACE}


_request_header_provider_registry.register(WorkspaceHeader)
mlflow.set_tracking_uri(MLFLOW_URL)
from mlflow.tracking import MlflowClient  # noqa: E402 (needs tracking uri set first)

client = MlflowClient()
exp = client.get_experiment_by_name(EXPERIMENT_NAME)
traces = mlflow.search_traces(
    experiment_ids=[exp.experiment_id],
    max_results=50,
    order_by=["timestamp DESC"],
    return_type="list",
)

rows = []
for t in traces:
    d = t.to_dict()
    spans = d["data"]["spans"]
    root = next((s for s in spans if s.get("parent_span_id") is None), None)
    if root is None:
        continue
    try:
        inputs = json.loads(root["attributes"]["mlflow.spanInputs"])
        question = inputs.get("question")
    except Exception:
        question = None
    try:
        outputs = json.loads(root["attributes"]["mlflow.spanOutputs"])
        response = outputs[0] if isinstance(outputs, list) and outputs else None
    except Exception:
        response = None

    contexts = []
    seen = set()
    for s in spans:
        if s.get("name") != "rag_search":
            continue
        try:
            rag_out = json.loads(s["attributes"]["mlflow.spanOutputs"])
        except Exception:
            continue
        for doc in rag_out.get("documents") or []:
            text = doc.get("content") or doc.get("text") or doc.get("chunk") or ""
            # the agent often runs more than one rag_search with overlapping
            # hits, dedupe so the judge prompt doesn't balloon with repeats
            if text and text not in seen:
                seen.add(text)
                contexts.append(text)

    # skip rounds where the agent never actually answered, nothing for the
    # judge to meaningfully score faithfulness against
    if "too many rounds" in (response or ""):
        continue
    if question and response and contexts:
        rows.append(
            {
                "user_input": question,
                "response": response,
                "retrieved_contexts": contexts,
            }
        )

print(f"found {len(rows)} usable rows out of {len(traces)} traces scanned")
with open(OUTPUT_PATH, "w") as fh:
    for r in rows:
        fh.write(json.dumps(r) + "\n")

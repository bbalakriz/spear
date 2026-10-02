"""phase1-ingestion-pipeline: the one kfp pipeline for the whole ingestion
flow, per PHASE1_PLAN.md section 3. every step here is real: list intake,
abac validation/quarantine, guardrails sanitization, and (as of the sdg
hub wiring) a real sdg hub generated evaluation dataset against
rag-phase1-ogx's chat model. the one piece still deliberately kept out of
this pipeline is the actual AutoRAG optimization run itself: that is a
separately registered kfp pipeline (documents-rag-optimization-pipeline,
auto registered on every dspa pipeline server) with its own long running
job semantics, triggered by pipelines/phase1_autorag/run_autorag.py
against this pipeline's eval dataset output, not embedded in this dag.

this pipeline never writes to rag_chunks anymore, on purpose, since
2026-09-28 (see PHASE1_PLAN.md). it used to, with a fixed size placeholder
chunker, before autorag ever had a chance to determine a winning
chunking and embedding recipe against the eval set this same pipeline
produces, which is backwards relative to how autorag is actually meant
to be used: pick the winner first, then index the full corpus with that
winner's exact settings. the real indexing now lives in
pipelines/phase1_apply_pattern/pipeline.py, run once a winning pattern
exists for a batch, not here.

run `python3 pipeline.py` to compile phase1-ingestion-pipeline.yaml, or
`python3 compile_and_run.py` to also upload and trigger a run against
rag-phase1-dspa (see that file for the pipeline server auth/upload code).
"""
from typing import NamedTuple

from kfp import dsl
from kfp import kubernetes

BASE_IMAGE = "registry.access.redhat.com/ubi9/python-311:latest"

MINIO_ENDPOINT_DEFAULT = "minio.minio.svc.cluster.local:9000"
RAW_BUCKET_DEFAULT = "rag-documents"
RAW_PREFIX_DEFAULT = "raw-intake/"
SANITIZED_PREFIX_DEFAULT = "sanitized/"
# guardrails_route and mlflow_url have no default on purpose, see the
# pipeline function below: both are openshift route hostnames, which are
# unique per cluster, a hardcoded default here would silently break on
# any cluster other than the one this was first written against. every
# caller (compile_and_run.py, the owner console backend's trigger
# endpoint) resolves them live via `oc get route` and passes them in
# explicitly instead.
MLFLOW_WORKSPACE_DEFAULT = "rag-phase1"
MLFLOW_EXPERIMENT_NAME_DEFAULT = "phase1-ingestion"
EVAL_PREFIX_DEFAULT = "autorag-eval/"
OGX_BASE_URL_DEFAULT = "http://rag-phase1-ogx-service.rag-phase1.svc.cluster.local:8321"
# glm-53-flash's maas gateway moved to root, body based routing on
# 2026-09-27 (see PHASE1_PLAN.md and pipelines/phase1_autorag/
# run_autorag.py), restored here once that was confirmed live through
# ogx itself, double "openai/" prefixed for the same litellm strip
# reason explained in generate_eval_dataset's own docstring below
GENERATION_MODEL_DEFAULT = "openai/publishers/prelude-maas/models/glm-53-flash"


@dsl.component(base_image=BASE_IMAGE, packages_to_install=["boto3==1.35.99"])
def list_intake_documents(
    minio_endpoint: str,
    bucket: str,
    prefix: str,
    valid_docs: dsl.Output[dsl.Dataset],
    quarantined_docs: dsl.Output[dsl.Dataset],
) -> NamedTuple("Outputs", [("valid_count", int), ("quarantined_count", int)]):
    """reads the batch manifest and the actual objects under raw-intake/,
    then applies the abac validation gate from PHASE1_PLAN.md section 4:
    doc_id, title, source, clearance_level always required, dept or team
    required once clearance_level isn't general, data_owner always
    required. anything failing that, or missing its file in the bucket
    entirely, gets quarantined with a reason rather than silently
    defaulted open or hidden.
    """
    import hashlib
    import json
    import os

    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://{minio_endpoint}",
        aws_access_key_id=os.environ["MINIO_ACCESS_KEY"],
        aws_secret_access_key=os.environ["MINIO_SECRET_KEY"],
    )

    manifest_key = f"{prefix}manifest.json"
    manifest_obj = s3.get_object(Bucket=bucket, Key=manifest_key)
    manifest = json.loads(manifest_obj["Body"].read())

    listed = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    present_files = {
        obj["Key"][len(prefix):] for obj in listed.get("Contents", [])
    } - {"manifest.json"}

    valid, quarantined = [], []
    for entry in manifest.get("documents", []):
        reasons = []
        for field in ("doc_id", "title", "source", "clearance_level"):
            if not entry.get(field):
                reasons.append(f"missing required field '{field}'")
        clearance = entry.get("clearance_level")
        if clearance and clearance != "general" and not (entry.get("dept") or entry.get("team")):
            reasons.append("clearance_level is not general but neither dept nor team is set")
        if not entry.get("data_owner"):
            reasons.append("missing required field 'data_owner'")
        filename = entry.get("filename")
        if not filename or filename not in present_files:
            reasons.append(f"file '{filename}' not found under {prefix} in bucket {bucket}")

        if reasons:
            quarantined.append({**entry, "quarantine_reasons": reasons})
            continue

        content = s3.get_object(Bucket=bucket, Key=f"{prefix}{filename}")["Body"].read()
        entry_out = dict(entry)
        entry_out["content_sha256"] = hashlib.sha256(content).hexdigest()
        valid.append(entry_out)

    with open(valid_docs.path, "w") as f:
        json.dump(valid, f, indent=2)
    with open(quarantined_docs.path, "w") as f:
        json.dump(quarantined, f, indent=2)

    print(f"valid: {len(valid)}, quarantined: {len(quarantined)}")
    for q in quarantined:
        print(f"  quarantined {q.get('doc_id', '?')}: {q['quarantine_reasons']}")

    outputs = NamedTuple("Outputs", [("valid_count", int), ("quarantined_count", int)])
    return outputs(len(valid), len(quarantined))


@dsl.component(base_image=BASE_IMAGE, packages_to_install=["boto3==1.35.99", "requests==2.32.3"])
def sanitize_documents(
    minio_endpoint: str,
    bucket: str,
    raw_prefix: str,
    sanitized_prefix: str,
    guardrails_route: str,
    valid_docs: dsl.Input[dsl.Dataset],
    sanitize_report: dsl.Output[dsl.Dataset],
) -> NamedTuple("Outputs", [("passed_count", int), ("blocked_count", int)]):
    """calls the already deployed NemoGuardrails cr's /v1/guardrail/checks
    per document, picking internal-docs-config (the default, omit
    config_id) or vendor-submission-config based on the manifest's
    source field, exactly the config_id switch PHASE1_PLAN.md section 2
    describes. documents that pass get copied to sanitized/, the one
    thing autorag is ever allowed to sample from. the seeded vendor dpa
    document with its embedded override attempt is expected to end up in
    blocked, not sanitized/, that is this step working as designed, not
    a failure.

    auth: the pod's own projected service account token, the in cluster
    equivalent of `oc whoami -t`, works here because
    manifests/20-guardrails/04-pipeline-rbac.yaml grants this pipeline's
    service account get on exactly the one service the guardrails route's
    kube-rbac-proxy checks for, confirmed by reading its config live.
    """
    import json
    import os

    import boto3
    import requests

    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://{minio_endpoint}",
        aws_access_key_id=os.environ["MINIO_ACCESS_KEY"],
        aws_secret_access_key=os.environ["MINIO_SECRET_KEY"],
    )
    with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
        sa_token = f.read().strip()

    with open(valid_docs.path) as f:
        docs = json.load(f)

    report = []
    passed = blocked = 0
    for doc in docs:
        filename = doc["filename"]
        content = s3.get_object(Bucket=bucket, Key=f"{raw_prefix}{filename}")["Body"].read().decode("utf-8")
        config_id = "vendor-submission-config" if doc.get("source") == "external-vendor" else None

        body = {"model": "test", "messages": [{"role": "user", "content": content}]}
        if config_id:
            body["guardrails"] = {"config_id": config_id}

        resp = requests.post(
            f"{guardrails_route}/v1/guardrail/checks",
            headers={"Authorization": f"Bearer {sa_token}", "Content-Type": "application/json"},
            json=body,
            verify=False,
            timeout=30,
        )
        verdict = "error"
        activated_rails = []
        try:
            data = resp.json()
            verdict = data.get("status", "error")
            activated_rails = data.get("rails_status", data.get("activated_rails", []))
        except ValueError:
            pass

        row = {
            "doc_id": doc["doc_id"],
            "filename": filename,
            "source": doc.get("source"),
            "config_id": config_id or "internal-docs-config",
            "http_status": resp.status_code,
            "verdict": verdict,
            "activated_rails": activated_rails,
        }
        if verdict == "success":
            s3.put_object(Bucket=bucket, Key=f"{sanitized_prefix}{filename}", Body=content.encode("utf-8"))
            passed += 1
        else:
            blocked += 1
        report.append(row)
        print(f"  {doc['doc_id']}: {verdict} (config_id={row['config_id']})")

    with open(sanitize_report.path, "w") as f:
        json.dump(report, f, indent=2)

    outputs = NamedTuple("Outputs", [("passed_count", int), ("blocked_count", int)])
    return outputs(passed, blocked)


@dsl.component(base_image=BASE_IMAGE, packages_to_install=["boto3==1.35.99", "sdg_hub==0.9.4", "datasets>=4.0.0"])
def generate_eval_dataset(
    minio_endpoint: str,
    bucket: str,
    sanitized_prefix: str,
    eval_prefix: str,
    batch_id: str,
    ogx_base_url: str,
    generation_model: str,
    valid_docs: dsl.Input[dsl.Dataset],
    sanitize_report: dsl.Input[dsl.Dataset],
    eval_dataset: dsl.Output[dsl.Dataset],
) -> NamedTuple("Outputs", [("qa_pairs_created", int), ("eval_dataset_key", str)]):
    """PHASE1_PLAN.md section 3, step 3: real sdg hub eval dataset
    generation, replacing the old autorag_status_stub. uses sdg hub's
    built in "RAG Evaluation Dataset" flow (registry id loud-dawn-245,
    required columns document + document_outline, output columns
    question/response/ground_truth_context) against every document that
    passed guardrails sanitization, one document per row so each
    generated question can still be traced back to the doc_id it came
    from.

    document_outline is not a separate authored field anywhere upstream,
    so this uses each document's manifest title as a lightweight stand
    in, honest simplification, not sdg hub's fault.

    talks to rag-phase1-ogx over its in cluster service url. one real,
    empirically confirmed gotcha: sdg hub calls litellm's `openai/`
    custom api_base provider routing, which strips exactly one leading
    path segment (up to the first '/') before sending the model field
    to ogx, so the model string passed in here always needs one extra
    leading "openai/" over whatever id ogx itself expects. ogx's own
    foundation model id for glm-53-flash is
    "openai/publishers/prelude-maas/models/glm-53-flash" (the long form
    is maas's own body based routing id, see
    pipelines/phase1_autorag/run_autorag.py and PHASE1_PLAN.md for why),
    so this needs a double prefixed
    "openai/openai/publishers/prelude-maas/models/glm-53-flash" to
    survive the strip.

    writes the AutoRAG test-data json schema
    (question/correct_answers/correct_answer_document_ids) both as this
    step's own kfp artifact and to minio under eval_prefix, so
    pipelines/phase1_autorag/run_autorag.py can point the real AutoRAG
    kfp pipeline's test_data_key straight at it.
    """
    import json
    import os

    import boto3
    from datasets import Dataset
    from sdg_hub import Flow, FlowRegistry

    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://{minio_endpoint}",
        aws_access_key_id=os.environ["MINIO_ACCESS_KEY"],
        aws_secret_access_key=os.environ["MINIO_SECRET_KEY"],
    )

    with open(valid_docs.path) as f:
        docs_by_id = {d["doc_id"]: d for d in json.load(f)}
    with open(sanitize_report.path) as f:
        report = json.load(f)

    sanitized_docs = []
    for row in report:
        if row["verdict"] != "success":
            continue
        doc = docs_by_id[row["doc_id"]]
        content = s3.get_object(
            Bucket=bucket, Key=f"{sanitized_prefix}{row['filename']}"
        )["Body"].read().decode("utf-8")
        sanitized_docs.append({
            "doc_id": doc["doc_id"],
            # ai4rag's own docling chunker sets each retrieved chunk's
            # document_id metadata to the raw filename (doc.name), never
            # our abac manifest's doc_id. correct_answer_document_ids
            # has to be written in that same namespace or
            # context_correctness is deterministically 0 regardless of
            # retrieval quality, confirmed by reading
            # ai4rag/rag/chunking/docling_chunker.py and
            # ai4rag/core/experiment/utils.py directly out of the
            # odh-autorag-rhel9 image.
            "filename": row["filename"],
            "document": content,
            "document_outline": doc.get("title", doc["doc_id"]),
        })

    eval_rows = []
    if not sanitized_docs:
        print("no sanitized documents available, writing an empty eval dataset")
    else:
        from sdg_hub.core.utils.error_handling import EmptyDatasetError

        FlowRegistry.discover_flows()
        flow_path = FlowRegistry.get_flow_path_safe("loud-dawn-245")
        flow = Flow.from_yaml(flow_path)
        flow.set_model_config(
            model=f"openai/{generation_model}",
            api_base=f"{ogx_base_url}/v1",
            api_key=os.environ["OGX_API_KEY"],
        )

        ds = Dataset.from_dict({
            "doc_id": [d["doc_id"] for d in sanitized_docs],
            "document": [d["document"] for d in sanitized_docs],
            "document_outline": [d["document_outline"] for d in sanitized_docs],
        })

        # the flow's own last stage (filter_ungrounded) is a real quality
        # gate: it drops any row whose critic score judged the generated
        # answer as not grounded in the context. on a small batch that
        # can legitimately empty the whole dataset out, sdg hub raises
        # EmptyDatasetError rather than returning zero rows. a couple of
        # retries absorb ordinary llm sampling variance (confirmed live:
        # the exact same single-document flow succeeded cleanly in an
        # earlier standalone run), if every attempt still comes back
        # empty this is reported as a real zero, never faked, not a
        # pipeline crash.
        result = None
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                result = flow.generate(ds, max_concurrency=4)
                break
            except EmptyDatasetError:
                print(f"attempt {attempt}/{attempts}: every row failed sdg hub's groundedness filter")
                if attempt == attempts:
                    print("giving up after all retries, reporting zero qa pairs for this batch")

        if result is not None and len(result) > 0:
            result_cols = result.column_names if hasattr(result, "column_names") else result.columns.tolist()
            filename_by_doc_id = {d["doc_id"]: d["filename"] for d in sanitized_docs}
            for idx in range(len(result)):
                row = result[idx] if hasattr(result, "__getitem__") else result.iloc[idx].to_dict()
                doc_id = row["doc_id"] if "doc_id" in result_cols else sanitized_docs[idx]["doc_id"]
                eval_rows.append({
                    "question": row["question"],
                    "correct_answers": [row["response"]],
                    # filename, not doc_id, see the comment above where
                    # sanitized_docs is built
                    "correct_answer_document_ids": [filename_by_doc_id[doc_id]],
                })

    with open(eval_dataset.path, "w") as f:
        json.dump(eval_rows, f, indent=2)

    eval_key = f"{eval_prefix}{batch_id}_test_data.json"
    s3.put_object(
        Bucket=bucket, Key=eval_key,
        Body=json.dumps(eval_rows, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    print(f"wrote {len(eval_rows)} qa pairs to s3://{bucket}/{eval_key}")

    outputs = NamedTuple("Outputs", [("qa_pairs_created", int), ("eval_dataset_key", str)])
    return outputs(len(eval_rows), eval_key)


@dsl.component(base_image=BASE_IMAGE, packages_to_install=["boto3==1.35.99", "requests==2.32.3"])
def log_ingestion_report(
    mlflow_url: str,
    mlflow_workspace: str,
    mlflow_experiment_name: str,
    batch_id: str,
    minio_endpoint: str,
    artifact_bucket: str,
    valid_count: int,
    quarantined_count: int,
    passed_count: int,
    blocked_count: int,
    qa_pairs_created: int,
    eval_dataset_key: str,
    valid_docs: dsl.Input[dsl.Dataset],
    quarantined_docs: dsl.Input[dsl.Dataset],
    sanitize_report: dsl.Input[dsl.Dataset],
    eval_dataset: dsl.Input[dsl.Dataset],
) -> str:
    """PHASE1_PLAN.md section 3, step 8: one json artifact aggregating
    every earlier step's real output, logged to the shared mlflow
    instance under the rag-phase1 workspace (X-MLflow-Workspace header,
    see manifests/00-platform/06-mlflow.yaml for why that header is
    required on this build). this is what the eventual console plugin
    backend reads, nothing there should ever recompute anything this
    step already produced.

    uses raw rest calls plus a direct boto3 artifact write instead of the
    mlflow client package: the client's http layer doesn't have a clean
    way to attach the workspace header this build requires per request,
    and we already have the minio credentials this run's artifact_uri
    resolves into on this bucket, so writing directly there is simpler
    than adding a whole client dependency for one file.
    """
    import json
    import os
    import time

    import boto3
    import requests

    with open(valid_docs.path) as f:
        valid = json.load(f)
    with open(quarantined_docs.path) as f:
        quarantined = json.load(f)
    with open(sanitize_report.path) as f:
        sanitize = json.load(f)
    with open(eval_dataset.path) as f:
        eval_rows = json.load(f)

    report = {
        "batch_id": batch_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ingestion": {
            "documents_scanned": valid_count + quarantined_count,
            "documents_valid": valid_count,
            "documents_quarantined": quarantined_count,
            "quarantined_detail": quarantined,
        },
        "guardrails": {
            "documents_sanitized": passed_count,
            "documents_blocked": blocked_count,
            "audit": sanitize,
        },
        "abac_tagging": {
            "status": "pending",
            "note": (
                "this batch is not indexed into rag_chunks yet. indexing now "
                "happens after a winning pattern is selected, see "
                "pipelines/phase1_apply_pattern/pipeline.py, not as part of "
                "ingestion. trigger an apply pattern run against this "
                "batch's autorag run once it has a winner."
            ),
        },
        "sdg_hub_eval_dataset": {
            "qa_pairs_created": qa_pairs_created,
            "s3_key": eval_dataset_key,
            "sample": eval_rows[:3],
        },
        "autorag_trigger": (
            "not run from inside this pipeline, see "
            "pipelines/phase1_autorag/run_autorag.py, which triggers the "
            "real documents-rag-optimization-pipeline against this "
            "batch's eval dataset separately"
        ),
        "valid_documents": valid,
    }

    headers = {"X-MLflow-Workspace": mlflow_workspace, "Content-Type": "application/json"}
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    if os.path.exists(token_path):
        with open(token_path) as f:
            headers["Authorization"] = f"Bearer {f.read().strip()}"

    search_resp = requests.post(
        f"{mlflow_url}/api/2.0/mlflow/experiments/search",
        headers=headers, json={"max_results": 100}, verify=False, timeout=30,
    )
    print(f"experiments/search: {search_resp.status_code} {search_resp.text}")
    search = search_resp.json()
    exp = next((e for e in search.get("experiments", []) if e["name"] == mlflow_experiment_name), None)
    if exp is None:
        create_resp = requests.post(
            f"{mlflow_url}/api/2.0/mlflow/experiments/create",
            headers=headers, json={"name": mlflow_experiment_name}, verify=False, timeout=30,
        )
        print(f"experiments/create: {create_resp.status_code} {create_resp.text}")
        exp = create_resp.json()
        experiment_id = exp["experiment_id"]
    else:
        experiment_id = exp["experiment_id"]

    run = requests.post(
        f"{mlflow_url}/api/2.0/mlflow/runs/create",
        headers=headers,
        json={"experiment_id": experiment_id, "start_time": int(time.time() * 1000),
              "tags": [{"key": "mlflow.runName", "value": batch_id}]},
        verify=False, timeout=30,
    ).json()
    run_id = run["run"]["info"]["run_id"]
    artifact_uri = run["run"]["info"]["artifact_uri"]

    metrics_ts = int(time.time() * 1000)
    requests.post(
        f"{mlflow_url}/api/2.0/mlflow/runs/log-batch",
        headers=headers,
        json={
            "run_id": run_id,
            "params": [{"key": "batch_id", "value": batch_id}],
            "metrics": [
                {"key": "documents_scanned", "value": valid_count + quarantined_count, "timestamp": metrics_ts},
                {"key": "documents_quarantined", "value": quarantined_count, "timestamp": metrics_ts},
                {"key": "documents_sanitized", "value": passed_count, "timestamp": metrics_ts},
                {"key": "documents_blocked", "value": blocked_count, "timestamp": metrics_ts},
                {"key": "eval_qa_pairs_created", "value": qa_pairs_created, "timestamp": metrics_ts},
            ],
        },
        verify=False, timeout=30,
    )

    # artifact_uri looks like s3://<bucket>/<prefix...>, write the report
    # straight there with the same credentials mlflow itself uses.
    assert artifact_uri.startswith("s3://"), artifact_uri
    _, path = artifact_uri[len("s3://"):].split("/", 1)
    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://{minio_endpoint}",
        aws_access_key_id=os.environ["MINIO_ACCESS_KEY"],
        aws_secret_access_key=os.environ["MINIO_SECRET_KEY"],
    )
    s3.put_object(
        Bucket=artifact_bucket,
        Key=f"{path}/ingestion_report.json",
        Body=json.dumps(report, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    requests.post(
        f"{mlflow_url}/api/2.0/mlflow/runs/update",
        headers=headers,
        json={"run_id": run_id, "status": "FINISHED", "end_time": int(time.time() * 1000)},
        verify=False, timeout=30,
    )

    print(f"logged mlflow run {run_id} in experiment {mlflow_experiment_name} (workspace {mlflow_workspace})")
    return run_id


def _use_minio_creds(task):
    kubernetes.use_secret_as_env(
        task, secret_name="minio-dsp-creds",
        secret_key_to_env={"accesskey": "MINIO_ACCESS_KEY", "secretkey": "MINIO_SECRET_KEY"},
    )


def _use_ogx_creds(task):
    kubernetes.use_secret_as_env(
        task, secret_name="ogx-glm-key",
        secret_key_to_env={"apiKey": "OGX_API_KEY"},
    )


@dsl.pipeline(
    name="phase1-ingestion-pipeline",
    description="docs -> abac validation gate -> nemo guardrails -> sdg hub eval dataset -> mlflow report",
)
def phase1_ingestion_pipeline(
    guardrails_route: str,
    mlflow_url: str,
    batch_id: str = "seed-batch-001",
    minio_endpoint: str = MINIO_ENDPOINT_DEFAULT,
    raw_bucket: str = RAW_BUCKET_DEFAULT,
    raw_prefix: str = RAW_PREFIX_DEFAULT,
    sanitized_prefix: str = SANITIZED_PREFIX_DEFAULT,
    mlflow_workspace: str = MLFLOW_WORKSPACE_DEFAULT,
    mlflow_experiment_name: str = MLFLOW_EXPERIMENT_NAME_DEFAULT,
    artifact_bucket: str = "dsp-pipeline-artifacts",
    eval_prefix: str = EVAL_PREFIX_DEFAULT,
    ogx_base_url: str = OGX_BASE_URL_DEFAULT,
    generation_model: str = GENERATION_MODEL_DEFAULT,
):
    list_task = list_intake_documents(minio_endpoint=minio_endpoint, bucket=raw_bucket, prefix=raw_prefix)
    _use_minio_creds(list_task)
    list_task.set_caching_options(False)

    sanitize_task = sanitize_documents(
        minio_endpoint=minio_endpoint, bucket=raw_bucket, raw_prefix=raw_prefix,
        sanitized_prefix=sanitized_prefix, guardrails_route=guardrails_route,
        valid_docs=list_task.outputs["valid_docs"],
    )
    _use_minio_creds(sanitize_task)
    sanitize_task.set_caching_options(False)

    eval_task = generate_eval_dataset(
        minio_endpoint=minio_endpoint, bucket=raw_bucket, sanitized_prefix=sanitized_prefix,
        eval_prefix=eval_prefix, batch_id=batch_id, ogx_base_url=ogx_base_url,
        generation_model=generation_model,
        valid_docs=list_task.outputs["valid_docs"], sanitize_report=sanitize_task.outputs["sanitize_report"],
    )
    _use_minio_creds(eval_task)
    _use_ogx_creds(eval_task)
    eval_task.set_caching_options(False)

    report_task = log_ingestion_report(
        mlflow_url=mlflow_url, mlflow_workspace=mlflow_workspace,
        mlflow_experiment_name=mlflow_experiment_name, batch_id=batch_id,
        minio_endpoint=minio_endpoint, artifact_bucket=artifact_bucket,
        valid_count=list_task.outputs["valid_count"],
        quarantined_count=list_task.outputs["quarantined_count"],
        passed_count=sanitize_task.outputs["passed_count"],
        blocked_count=sanitize_task.outputs["blocked_count"],
        qa_pairs_created=eval_task.outputs["qa_pairs_created"],
        eval_dataset_key=eval_task.outputs["eval_dataset_key"],
        valid_docs=list_task.outputs["valid_docs"],
        quarantined_docs=list_task.outputs["quarantined_docs"],
        sanitize_report=sanitize_task.outputs["sanitize_report"],
        eval_dataset=eval_task.outputs["eval_dataset"],
    )
    _use_minio_creds(report_task)
    report_task.set_caching_options(False)


if __name__ == "__main__":
    from kfp import compiler

    # matches compile_and_run.py's own output filename, was previously
    # writing to pipeline.yaml instead by accident (__file__ replace bug)
    out = __file__.replace("pipeline.py", "phase1-ingestion-pipeline.yaml")
    compiler.Compiler().compile(phase1_ingestion_pipeline, out)
    print(f"compiled {out}")

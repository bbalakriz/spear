# backend for the phase1-ingestion-console ConsolePlugin's spec.proxy, see
# PHASE1_PLAN.md section 6 plus the "trigger and business value" addendum.
#
# unlike the first version of this file, this one is no longer stdlib only:
# reading a completed autorag run's per pattern scores needs a real signed s3
# get (boto3), and triggering kfp runs correctly needs the real kfp rest field
# names rather than a hand rolled json body, so this now ships as a built
# image (see owner-console-backend/Dockerfile) instead of a configmap mounted
# script.
#
# mlflow reads still use this pod's own service account token, unchanged from
# before. anything that touches the kfp pipeline server (listing kfp runs,
# reading autorag pattern scores, triggering a run) uses the signed in user's
# own forwarded token instead, since the consoleplugin proxy alias is now
# authorization: UserToken, not None. that means a user can only trigger or
# inspect kfp runs if their own openshift identity already has rbac on
# rag-phase1's pipeline resources, this backend's own service account never
# gains that power just by existing.
import json
import os
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3

# mlflow's external url, the dspa pipeline server's route, minio's own
# console route and the rhoai dashboard route are all unique per cluster,
# never hardcode any of them: all four are resolved once at deploy time and
# injected here as env vars, see manifests/40-owner-console/01-backend.yaml
# and scripts/07-install-owner-console.sh
MLFLOW_URL = os.environ["MLFLOW_URL"]
DSPA_ROUTE = os.environ["DSPA_ROUTE"]
GUARDRAILS_ROUTE = os.environ["GUARDRAILS_ROUTE"]
MINIO_CONSOLE_URL = os.environ["MINIO_CONSOLE_URL"]
RHOAI_DASHBOARD_URL = os.environ["RHOAI_DASHBOARD_URL"]
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


def _env_list(name, default_csv):
    # every pipeline/experiment name, bucket, secret name and autorag knob
    # below used to be a python constant baked into the image, so changing
    # any of them meant a rebuild. moved to env vars 2026-09-28 so an
    # operator can retune this deployment with `oc set env` alone, same
    # spirit as the route vars above. defaults below match this project's
    # own known good values, so an unpatched deployment behaves exactly as
    # before, see scripts/07-install-owner-console.sh for where these are
    # actually set explicitly rather than left to fall back on a default.
    raw = os.environ.get(name, default_csv)
    return [item.strip() for item in raw.split(",") if item.strip()]


WORKSPACE = os.environ.get("RAG_WORKSPACE", "rag-phase1")
INGESTION_EXPERIMENT_NAME = os.environ.get("INGESTION_EXPERIMENT_NAME", "phase1-ingestion")

INGESTION_PIPELINE_NAME = os.environ.get("INGESTION_PIPELINE_NAME", "phase1-ingestion-pipeline")
INGESTION_KFP_EXPERIMENT_NAME = os.environ.get("INGESTION_KFP_EXPERIMENT_NAME", "phase1-ingestion")
AUTORAG_PIPELINE_NAME = os.environ.get("AUTORAG_PIPELINE_NAME", "documents-rag-optimization-pipeline")
AUTORAG_KFP_EXPERIMENT_NAME = os.environ.get("AUTORAG_KFP_EXPERIMENT_NAME", "phase1-autorag")
APPLY_PATTERN_PIPELINE_NAME = os.environ.get(
    "APPLY_PATTERN_PIPELINE_NAME", "phase1-apply-pattern-pipeline"
)
APPLY_PATTERN_KFP_EXPERIMENT_NAME = os.environ.get(
    "APPLY_PATTERN_KFP_EXPERIMENT_NAME", "phase1-apply-pattern"
)

# these mirror run_autorag.py's own defaults, confirmed live against this
# cluster's own registered pipeline, not from any external copy of the
# pipeline. kept in sync by hand since this backend intentionally does not
# import that script (it lives outside the built image's context). the
# model lists are the two most likely to need retuning without a rebuild,
# e.g. reverting to glm-53-flash once it is confirmed live again in ogx's
# own /v1/models, see PHASE1_PLAN.md.
AUTORAG_RAW_BUCKET = os.environ.get("AUTORAG_RAW_BUCKET", "rag-documents")
AUTORAG_S3_SECRET_NAME = os.environ.get("AUTORAG_S3_SECRET_NAME", "autorag-s3-creds")
AUTORAG_OGX_SECRET_NAME = os.environ.get("AUTORAG_OGX_SECRET_NAME", "autorag-ogx-creds")
AUTORAG_VECTOR_IO_PROVIDER_ID = os.environ.get("AUTORAG_VECTOR_IO_PROVIDER_ID", "pgvector")
AUTORAG_EMBEDDING_MODELS = _env_list(
    "AUTORAG_EMBEDDING_MODELS", "vllm-embedding/nomic-embed-text-v1.5"
)
AUTORAG_GENERATION_MODELS = _env_list(
    "AUTORAG_GENERATION_MODELS",
    "vllm-inference/Qwen3.6-35B-A3B,openai/publishers/prelude-maas/models/qwen38-27b",
)
AUTORAG_MAX_RAG_PATTERNS = int(os.environ.get("AUTORAG_MAX_RAG_PATTERNS", "8"))
AUTORAG_PRESET = os.environ.get("AUTORAG_PRESET", "balanced")
AUTORAG_OPTIMIZATION_METRIC = os.environ.get("AUTORAG_OPTIMIZATION_METRIC", "faithfulness")

MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://minio.minio.svc.cluster.local:9000")
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "dsp-pipeline-artifacts")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "")


def sa_token():
    with open(TOKEN_PATH) as f:
        return f.read().strip()


def mlflow_headers():
    return {
        "X-MLflow-Workspace": WORKSPACE,
        "Authorization": f"Bearer {sa_token()}",
        "Content-Type": "application/json",
    }


def _no_verify_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def mlflow_post(path, body):
    req = urllib.request.Request(
        f"{MLFLOW_URL}{path}",
        data=json.dumps(body).encode(),
        headers=mlflow_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30, context=_no_verify_ctx()) as resp:
        return json.loads(resp.read())


def mlflow_get_artifact(run_id, path):
    url = f"{MLFLOW_URL}/get-artifact?path={path}&run_uuid={run_id}"
    req = urllib.request.Request(url, headers=mlflow_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=30, context=_no_verify_ctx()) as resp:
        return resp.read()


def mlflow_get_run(run_id):
    # unlike runs/search, runs/get is a real get only endpoint, confirmed
    # live, posting a json body to it 404s.
    url = f"{MLFLOW_URL}/api/2.0/mlflow/runs/get?run_id={run_id}"
    req = urllib.request.Request(url, headers=mlflow_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=30, context=_no_verify_ctx()) as resp:
        return json.loads(resp.read())


def find_ingestion_experiment():
    experiments = mlflow_post("/api/2.0/mlflow/experiments/search", {"max_results": 100})
    return next(
        (e for e in experiments.get("experiments", []) if e["name"] == INGESTION_EXPERIMENT_NAME),
        None,
    )


def recent_ingestion_runs(max_results=20):
    # lists the most recent ingestion runs so the console can offer a picker
    # instead of only ever jumping straight to the latest one.
    exp = find_ingestion_experiment()
    if exp is None:
        return []
    runs = mlflow_post(
        "/api/2.0/mlflow/runs/search",
        {
            "experiment_ids": [exp["experiment_id"]],
            "max_results": max_results,
            "order_by": ["attributes.start_time DESC"],
        },
    )
    out = []
    for run in runs.get("runs", []):
        info = run["info"]
        out.append(
            {
                "run_id": info["run_id"],
                "run_name": info.get("run_name"),
                "start_time": info.get("start_time"),
                "status": info.get("status"),
            }
        )
    return out


def ingestion_report(run_id=None):
    exp = find_ingestion_experiment()
    if exp is None:
        return {"error": f"No {INGESTION_EXPERIMENT_NAME!r} experiment found in workspace {WORKSPACE!r}"}

    if run_id:
        got = mlflow_get_run(run_id)
        run = got.get("run")
        if run is None:
            return {"error": f"Run {run_id!r} not found"}
    else:
        runs = mlflow_post(
            "/api/2.0/mlflow/runs/search",
            {
                "experiment_ids": [exp["experiment_id"]],
                "max_results": 1,
                "order_by": ["attributes.start_time DESC"],
            },
        )
        run_list = runs.get("runs", [])
        if not run_list:
            return {"error": "Experiment exists but has no runs yet"}
        run = run_list[0]

    resolved_run_id = run["info"]["run_id"]
    report_bytes = mlflow_get_artifact(resolved_run_id, "ingestion_report.json")
    report = json.loads(report_bytes)
    report["_mlflow_run_id"] = resolved_run_id
    report["_mlflow_run_name"] = run["info"].get("run_name")
    report["_mlflow_run_url"] = mlflow_run_link(run["info"]["experiment_id"], resolved_run_id)

    # every document this report mentions, quarantined or merely audited,
    # always still has its original copy under raw-intake/, confirmed live
    # against pipeline.py: quarantined docs never leave raw-intake/, and a
    # sanitize verdict of "success" only ever adds a second copy under
    # sanitized/, it never removes the raw-intake/ one. linking to
    # raw-intake/ is therefore always correct, pass, block or quarantine.
    for doc in report.get("ingestion", {}).get("quarantined_detail", []):
        doc["minio_url"] = minio_object_link(AUTORAG_RAW_BUCKET, f"raw-intake/{doc['filename']}")
    for entry in report.get("guardrails", {}).get("audit", []):
        entry["minio_url"] = minio_object_link(AUTORAG_RAW_BUCKET, f"raw-intake/{entry['filename']}")
    return report


# ---- kfp pipeline server rest helpers, all authenticated with the caller's
# own forwarded token, never this pod's own identity, see the header comment
# above. field names confirmed against kfp_server_api's generated models and
# live curl calls against this cluster's own dspa route, 2026-09-28, not
# guessed from the kfp sdk's python surface. ----

def kfp_headers(user_token):
    return {"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"}


def kfp_get(path, user_token):
    req = urllib.request.Request(f"{DSPA_ROUTE}{path}", headers=kfp_headers(user_token), method="GET")
    with urllib.request.urlopen(req, timeout=30, context=_no_verify_ctx()) as resp:
        return json.loads(resp.read())


def kfp_post(path, body, user_token):
    req = urllib.request.Request(
        f"{DSPA_ROUTE}{path}",
        data=json.dumps(body).encode(),
        headers=kfp_headers(user_token),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30, context=_no_verify_ctx()) as resp:
        return json.loads(resp.read())


def kfp_name_filter(display_name):
    return json.dumps(
        {"predicates": [{"key": "display_name", "operation": "EQUALS", "string_value": display_name}]}
    )


def find_pipeline_id(pipeline_name, user_token):
    q = urllib.parse.quote(kfp_name_filter(pipeline_name))
    resp = kfp_get(f"/apis/v2beta1/pipelines?filter={q}", user_token)
    pipelines = resp.get("pipelines", [])
    if not pipelines:
        raise LookupError(f"No KFP pipeline named {pipeline_name!r} is registered on this pipeline server yet")
    return pipelines[0]["pipeline_id"]


def find_latest_pipeline_version_id(pipeline_id, user_token):
    resp = kfp_get(
        f"/apis/v2beta1/pipelines/{pipeline_id}/versions?sort_by=created_at%20desc&page_size=1",
        user_token,
    )
    versions = resp.get("pipeline_versions", [])
    if not versions:
        raise LookupError(f"Pipeline {pipeline_id!r} has no versions registered")
    return versions[0]["pipeline_version_id"]


def find_or_create_experiment_id(experiment_name, user_token):
    q = urllib.parse.quote(kfp_name_filter(experiment_name))
    resp = kfp_get(f"/apis/v2beta1/experiments?filter={q}", user_token)
    experiments = resp.get("experiments", [])
    if experiments:
        return experiments[0]["experiment_id"]
    created = kfp_post("/apis/v2beta1/experiments", {"display_name": experiment_name}, user_token)
    return created["experiment_id"]


def trigger_kfp_run(pipeline_name, experiment_name, job_name, parameters, user_token):
    pipeline_id = find_pipeline_id(pipeline_name, user_token)
    version_id = find_latest_pipeline_version_id(pipeline_id, user_token)
    experiment_id = find_or_create_experiment_id(experiment_name, user_token)
    body = {
        "display_name": job_name,
        "experiment_id": experiment_id,
        "pipeline_version_reference": {"pipeline_id": pipeline_id, "pipeline_version_id": version_id},
        "runtime_config": {"parameters": parameters},
    }
    run = kfp_post("/apis/v2beta1/runs", body, user_token)
    return {"run_id": run["run_id"], "display_name": run.get("display_name"), "state": run.get("state")}


def dashboard_run_link(run_id):
    # confirmed live against this cluster 2026-09-28: navigating the
    # console browser to https://<rhods-dashboard route>/pipelineRuns/<ns>
    # is honored as a valid post login redirect target by the sso flow,
    # and opendatahub-io/odh-dashboard's own GlobalPipelineRunsRoutes.tsx
    # (via deepwiki) confirms the single run detail path is exactly
    # /pipelineRuns/:namespace/runs/:runId, never guessed from memory.
    return f"{RHOAI_DASHBOARD_URL}/pipelineRuns/{WORKSPACE}/runs/{run_id}"


def mlflow_run_link(experiment_id, run_id):
    # this exact hash route, "/#/experiments/<id>/runs/<run_id>", is not
    # guessed: it is mlflow's own tracking client's default run link,
    # see mlflow/tracking/_tracking_service/client.py's get_run_link.
    # confirmed live 2026-09-28 that this project's own mlflow route
    # answers on it (a 302 into its oauth proxy's login flow, the same
    # shape every other route link here gets when curled unauthenticated).
    return f"{MLFLOW_URL}/#/experiments/{experiment_id}/runs/{run_id}"


def autorag_results_link(run_id):
    # gen-ai-studio's own autorag results page for a completed run, exact
    # path given directly and confirmed live 2026-09-28: same 301 into the
    # dashboard's own login flow that dashboard_run_link above gets when
    # curled unauthenticated, not a 404.
    return f"{RHOAI_DASHBOARD_URL}/gen-ai-studio/autorag/results/{WORKSPACE}/{run_id}"


def minio_object_link(bucket, key):
    # confirmed live against this cluster 2026-09-28 by logging into the
    # minio console (server RELEASE.2025-09-07's own embedded console) and
    # clicking through to a real object: the browser lands on
    # /browser/<bucket>/<key with every '/' percent encoded as %2F>, quote
    # with safe="" reproduces that exactly.
    return f"{MINIO_CONSOLE_URL}/browser/{bucket}/{urllib.parse.quote(key, safe='')}"


def _parse_kfp_timestamp(value):
    # kfp's created_at/finished_at are rfc3339 strings, mlflow's start_time
    # is already epoch milliseconds, this gets both onto the same footing
    # so the merged run list below can sort on one field.
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def recent_kfp_runs(pipeline_name, max_results, user_token):
    # the runs list endpoint has no pipeline name filter, only experiment or
    # run field filters, so this filters client side on the job_name prefix
    # this project's own trigger scripts always use instead.
    resp = kfp_get(f"/apis/v2beta1/runs?sort_by=created_at%20desc&page_size=50", user_token)
    runs = resp.get("runs", [])
    matched = [r for r in runs if (r.get("display_name") or "").startswith(pipeline_name)]
    # gen-ai-studio's own results page only exists for autorag runs, not
    # sanitization or apply pattern ones, so this extra link is only added
    # when listing the autorag pipeline's own runs.
    is_autorag = pipeline_name == AUTORAG_PIPELINE_NAME
    return [
        {
            "run_id": r["run_id"],
            "display_name": r.get("display_name"),
            "state": r.get("state"),
            "created_at": r.get("created_at"),
            "finished_at": r.get("finished_at"),
            "dashboard_url": dashboard_run_link(r["run_id"]),
            **({"autorag_results_url": autorag_results_link(r["run_id"])} if is_autorag else {}),
        }
        for r in matched[:max_results]
    ]


def merged_ingestion_runs(user_token):
    # the run picker needs to show a run the moment it is triggered, but
    # mlflow only ever has a run once log_ingestion_report (the pipeline's
    # last step) writes it, see pipeline.py. kfp's own run list already has
    # every in flight run, so this merges the two, correlated by batch_id:
    # kfp's display_name is always f"{INGESTION_PIPELINE_NAME}-{batch_id}"
    # (set in the /trigger/ingestion handler below) and mlflow's run_name is
    # always the batch_id itself (pipeline.py's own mlflow.runName tag).
    prefix = f"{INGESTION_PIPELINE_NAME}-"
    by_batch = {}
    for r in recent_kfp_runs(INGESTION_PIPELINE_NAME, 30, user_token):
        display_name = r["display_name"] or ""
        batch_id = display_name[len(prefix):] if display_name.startswith(prefix) else display_name
        by_batch[batch_id] = {
            "batch_id": batch_id,
            "mlflow_run_id": None,
            "mlflow_status": None,
            "kfp_run_id": r["run_id"],
            "kfp_state": r["state"],
            "start_time": _parse_kfp_timestamp(r["created_at"]),
            "has_report": False,
            "dashboard_url": r["dashboard_url"],
        }
    for r in recent_ingestion_runs(30):
        batch_id = r["run_name"]
        entry = by_batch.setdefault(
            batch_id,
            {
                "batch_id": batch_id,
                "mlflow_run_id": None,
                "mlflow_status": None,
                "kfp_run_id": None,
                "kfp_state": None,
                "start_time": r["start_time"] or 0,
                "has_report": False,
                "dashboard_url": None,
            },
        )
        entry["mlflow_run_id"] = r["run_id"]
        entry["mlflow_status"] = r["status"]
        entry["has_report"] = True

    return sorted(by_batch.values(), key=lambda e: e["start_time"], reverse=True)


def autorag_leaderboard(run_id, user_token):
    # there is no single "leaderboard" artifact, confirmed live 2026-09-28
    # against a completed run: the real per pattern scores live as
    # rag_patterns/Pattern<N>/pattern.json files under the run's own
    # rag-templates-optimization task in minio, and one of pattern.json's
    # metrics is flagged optimization_metric: true, that is the field this
    # project actually optimizes on (matches run_autorag.py's own default).
    #
    # the task's own kfp task_id (the folder name minio actually uses) does
    # not reliably match the "rag-templates-optimization" dag task's own
    # task_id in run_details.task_details, confirmed live: the dag level
    # task_id and the minio folder's task_id are two different ids, most
    # likely because the dag task and the executor that actually writes
    # artifacts are separate task tree nodes that this kfp version does not
    # cleanly cross reference to each other. simpler and more robust to just
    # ask minio directly which task_id folder exists under this run's own
    # rag-templates-optimization prefix, rather than trust the task tree.
    run = kfp_get(f"/apis/v2beta1/runs/{run_id}", user_token)
    state = run.get("state")
    if state != "SUCCEEDED":
        return {"state": state, "patterns_tested": 0, "winner": None}

    s3 = boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
    )
    stage_prefix = f"{AUTORAG_PIPELINE_NAME}/{run_id}/rag-templates-optimization/"
    stage_listing = s3.list_objects_v2(Bucket=ARTIFACT_BUCKET, Prefix=stage_prefix, Delimiter="/")
    task_folders = [p["Prefix"] for p in stage_listing.get("CommonPrefixes", [])]
    if not task_folders:
        return {"state": state, "patterns_tested": 0, "winner": None}

    prefix = f"{task_folders[0]}rag_patterns/"
    paginator = s3.get_paginator("list_objects_v2")
    pattern_keys = [
        obj["Key"]
        for page in paginator.paginate(Bucket=ARTIFACT_BUCKET, Prefix=prefix)
        for obj in page.get("Contents", [])
        if obj["Key"].endswith("/pattern.json")
    ]

    best = None
    for key in pattern_keys:
        body = json.loads(s3.get_object(Bucket=ARTIFACT_BUCKET, Key=key)["Body"].read())
        metrics = body.get("evaluation", {}).get("metrics", [])
        metric = next((m for m in metrics if m.get("optimization_metric")), None)
        if metric is None:
            continue
        score = metric["scores"]["mean"]
        if best is None or score > best["score"]:
            # a pattern.json's own metrics list carries several named scores
            # at once (faithfulness, answer_correctness, context_correctness,
            # answer_relevance, overall_score, confirmed live 2026-09-28
            # against a real completed run), not only the one flagged
            # optimization_metric. that one still decides the winner, but
            # the rest are worth showing too, so keep all of them here
            # rather than discarding everything except the winning score.
            best = {
                "pattern_name": body.get("name"),
                "metric_name": metric.get("name"),
                "score": score,
                "settings": body.get("settings"),
                "all_metrics": [
                    {"name": m.get("name"), "score": m.get("scores", {}).get("mean")}
                    for m in metrics
                    if m.get("scores", {}).get("mean") is not None
                ],
            }

    return {"state": state, "patterns_tested": len(pattern_keys), "winner": best}


class Handler(BaseHTTPRequestHandler):
    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _user_token(self):
        auth = self.headers.get("Authorization", "")
        if not auth.lower().startswith("bearer "):
            raise PermissionError(
                "No forwarded user token, is the proxy alias set to authorization: UserToken?"
            )
        return auth.split(" ", 1)[1].strip()

    def _query(self):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        query = self._query()
        try:
            if path == "/healthz":
                self._json(200, {"status": "ok"})
            elif path == "/runs":
                self._json(200, {"runs": merged_ingestion_runs(self._user_token())})
            elif path == "/report":
                self._json(200, ingestion_report(query.get("run_id")))
            elif path == "/kfp/ingestion-runs":
                self._json(200, {"runs": recent_kfp_runs(INGESTION_PIPELINE_NAME, 10, self._user_token())})
            elif path == "/kfp/autorag-runs":
                self._json(200, {"runs": recent_kfp_runs(AUTORAG_PIPELINE_NAME, 10, self._user_token())})
            elif path == "/kfp/apply-pattern-runs":
                self._json(200, {"runs": recent_kfp_runs(APPLY_PATTERN_PIPELINE_NAME, 10, self._user_token())})
            elif path == "/kfp/autorag-leaderboard":
                run_id = query.get("run_id")
                if not run_id:
                    self._json(400, {"error": "run_id query param is required"})
                else:
                    self._json(200, autorag_leaderboard(run_id, self._user_token()))
            else:
                self._json(404, {"error": "Not found"})
        except PermissionError as exc:
            self._json(401, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - always report as json, never a bare 500 page
            self._json(502, {"error": str(exc)})

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        try:
            if path == "/trigger/ingestion":
                # a fixed fallback batch id used to collide with itself on a
                # second click with the field left blank, both mlflow's
                # run_name and the merged run picker key on batch_id, so two
                # such runs would have hidden each other. a timestamp makes
                # every auto generated batch id unique.
                batch_id = body.get("batch_id") or f"console-{int(time.time())}"
                result = trigger_kfp_run(
                    INGESTION_PIPELINE_NAME,
                    INGESTION_KFP_EXPERIMENT_NAME,
                    f"{INGESTION_PIPELINE_NAME}-{batch_id}",
                    {
                        "batch_id": batch_id,
                        "guardrails_route": GUARDRAILS_ROUTE,
                        "mlflow_url": MLFLOW_URL,
                    },
                    self._user_token(),
                )
                result["batch_id"] = batch_id
                result["dashboard_url"] = dashboard_run_link(result["run_id"])
                self._json(200, result)
            elif path == "/trigger/autorag":
                batch_id = body.get("batch_id")
                if not batch_id:
                    self._json(
                        400,
                        {
                            "error": "batch_id is required, must match the sanitization "
                            "batch that produced the eval dataset it will score against"
                        },
                    )
                    return
                params = {
                    "embedding_models": AUTORAG_EMBEDDING_MODELS,
                    "generation_models": AUTORAG_GENERATION_MODELS,
                    "input_data_bucket_name": AUTORAG_RAW_BUCKET,
                    "input_data_key": "sanitized/",
                    "input_data_secret_name": AUTORAG_S3_SECRET_NAME,
                    "ogx_secret_name": AUTORAG_OGX_SECRET_NAME,
                    "vector_io_provider_id": AUTORAG_VECTOR_IO_PROVIDER_ID,
                    "optimization_max_rag_patterns": AUTORAG_MAX_RAG_PATTERNS,
                    "optimization_metric": AUTORAG_OPTIMIZATION_METRIC,
                    "preset": AUTORAG_PRESET,
                    "test_data_bucket_name": AUTORAG_RAW_BUCKET,
                    "test_data_key": f"autorag-eval/{batch_id}_test_data.json",
                    "test_data_secret_name": AUTORAG_S3_SECRET_NAME,
                }
                result = trigger_kfp_run(
                    AUTORAG_PIPELINE_NAME,
                    AUTORAG_KFP_EXPERIMENT_NAME,
                    f"{AUTORAG_PIPELINE_NAME}-{batch_id}",
                    params,
                    self._user_token(),
                )
                result["batch_id"] = batch_id
                result["dashboard_url"] = dashboard_run_link(result["run_id"])
                self._json(200, result)
            elif path == "/trigger/apply-pattern":
                batch_id = body.get("batch_id")
                autorag_run_id = body.get("autorag_run_id")
                if not batch_id or not autorag_run_id:
                    self._json(
                        400,
                        {"error": "batch_id and autorag_run_id are both required"},
                    )
                    return
                user_token = self._user_token()
                leaderboard = autorag_leaderboard(autorag_run_id, user_token)
                winner = leaderboard.get("winner")
                if winner is None:
                    self._json(
                        400,
                        {
                            "error": f"Run {autorag_run_id!r} has no winning pattern yet "
                            f"(state: {leaderboard.get('state')}), nothing to apply"
                        },
                    )
                    return
                settings = winner["settings"]
                params = {
                    "mlflow_url": MLFLOW_URL,
                    "batch_id": batch_id,
                    "pattern_name": winner["pattern_name"],
                    "autorag_run_id": autorag_run_id,
                    "embedding_model": settings["embedding"]["model_id"],
                    "chunk_size": settings["chunking"]["chunk_size"],
                    "chunk_overlap": settings["chunking"]["chunk_overlap"],
                }
                result = trigger_kfp_run(
                    APPLY_PATTERN_PIPELINE_NAME,
                    APPLY_PATTERN_KFP_EXPERIMENT_NAME,
                    f"{APPLY_PATTERN_PIPELINE_NAME}-{batch_id}",
                    params,
                    user_token,
                )
                result["batch_id"] = batch_id
                result["pattern_name"] = winner["pattern_name"]
                result["dashboard_url"] = dashboard_run_link(result["run_id"])
                self._json(200, result)
            else:
                self._json(404, {"error": "Not found"})
        except PermissionError as exc:
            self._json(401, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json(502, {"error": str(exc)})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile="/etc/tls/private/tls.crt", keyfile="/etc/tls/private/tls.key")
    server = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    server.serve_forever()

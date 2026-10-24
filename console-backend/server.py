# backend for the spear-console ConsolePlugin's spec.proxy
# mlflow reads use this pod's own service account token
# kfp calls (runs, autorag scores, triggers) use the signed in user's own
# forwarded token, so rbac on spear-pipelines gates them, not this sa
import base64
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
import yaml
from kubernetes import client as k8s_client, config as k8s_config
from kubernetes.stream import stream as k8s_stream

# per cluster routes, injected at deploy time, see scripts/08-install-console.sh
MLFLOW_URL = os.environ["MLFLOW_URL"]
DSPA_ROUTE = os.environ["DSPA_ROUTE"]
GUARDRAILS_ROUTE = os.environ["GUARDRAILS_ROUTE"]
MINIO_CONSOLE_URL = os.environ["MINIO_CONSOLE_URL"]
RHOAI_DASHBOARD_URL = os.environ["RHOAI_DASHBOARD_URL"]
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


def _env_list(name, default_csv):
    # tunable via `oc set env`, no rebuild needed; defaults match known good values
    raw = os.environ.get(name, default_csv)
    return [item.strip() for item in raw.split(",") if item.strip()]


# see docs/decisions.md D9
WORKSPACE = os.environ.get("RAG_WORKSPACE", "spear-pipelines")
INGESTION_EXPERIMENT_NAME = os.environ.get("INGESTION_EXPERIMENT_NAME", "spear-corpus-prepare")
# written by scripts/11-submit-evalhub-jobs.sh each eval cycle
EVAL_RESULTS_EXPERIMENT_NAME = os.environ.get(
    "EVAL_RESULTS_EXPERIMENT_NAME", "spear-shield-evalhub-results"
)

INGESTION_PIPELINE_NAME = os.environ.get("INGESTION_PIPELINE_NAME", "spear-corpus-prepare-pipeline")
INGESTION_KFP_EXPERIMENT_NAME = os.environ.get("INGESTION_KFP_EXPERIMENT_NAME", "spear-corpus-prepare")
AUTORAG_PIPELINE_NAME = os.environ.get("AUTORAG_PIPELINE_NAME", "documents-rag-optimization-pipeline")
AUTORAG_KFP_EXPERIMENT_NAME = os.environ.get("AUTORAG_KFP_EXPERIMENT_NAME", "phase1-autorag")
APPLY_PATTERN_PIPELINE_NAME = os.environ.get(
    "APPLY_PATTERN_PIPELINE_NAME", "spear-corpus-index-pipeline"
)
APPLY_PATTERN_KFP_EXPERIMENT_NAME = os.environ.get(
    "APPLY_PATTERN_KFP_EXPERIMENT_NAME", "spear-corpus-index"
)

# mirrors run_autorag.py defaults; kept in sync by hand
AUTORAG_RAW_BUCKET = os.environ.get("AUTORAG_RAW_BUCKET", "rag-documents")
AUTORAG_S3_SECRET_NAME = os.environ.get("AUTORAG_S3_SECRET_NAME", "autorag-s3-creds")
AUTORAG_OGX_SECRET_NAME = os.environ.get("AUTORAG_OGX_SECRET_NAME", "autorag-ogx-creds")
AUTORAG_VECTOR_IO_PROVIDER_ID = os.environ.get("AUTORAG_VECTOR_IO_PROVIDER_ID", "pgvector")
AUTORAG_EMBEDDING_MODELS = _env_list(
    "AUTORAG_EMBEDDING_MODELS", "vllm-embedding/nomic-embed-text-v1.5"
)
AUTORAG_GENERATION_MODELS = _env_list(
    "AUTORAG_GENERATION_MODELS",
    "vllm-inference/Qwen3.6-35B-A3B,openai/publishers/prelude-maas/models/glm-53-flash",
)
AUTORAG_MAX_RAG_PATTERNS = int(os.environ.get("AUTORAG_MAX_RAG_PATTERNS", "8"))
AUTORAG_PRESET = os.environ.get("AUTORAG_PRESET", "balanced")
AUTORAG_OPTIMIZATION_METRIC = os.environ.get("AUTORAG_OPTIMIZATION_METRIC", "faithfulness")

MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://minio.minio.svc.cluster.local:9000")
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "dsp-pipeline-artifacts")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "")
# knowledge base page's source strip. no secrets here, those are mounted into
# the pipeline's own task pods, never through this backend. fallback only,
# ingestion_source_config() below prefers the live registered pipeline spec
INGESTION_SOURCE_ENDPOINT = os.environ.get(
    "INGESTION_SOURCE_ENDPOINT", "minio.minio.svc.cluster.local:9000"
)
INGESTION_SOURCE_BUCKET = os.environ.get("INGESTION_SOURCE_BUCKET", AUTORAG_RAW_BUCKET)
INGESTION_SOURCE_PREFIX = os.environ.get("INGESTION_SOURCE_PREFIX", "raw-intake/")

# spear shield chat tab, calls spear-coordinator-agent via the a2a gateway
# (see docs/decisions.md D18 for identity resolution)
SPEAR_SHIELD_ISSUER_URL = os.environ.get("SPEAR_SHIELD_ISSUER_URL", "")
SPEAR_COORDINATOR_A2A_URL = os.environ.get("SPEAR_COORDINATOR_A2A_URL", "")
SPEAR_COORDINATOR_CLIENT_ID = os.environ.get("SPEAR_COORDINATOR_CLIENT_ID", "spear-coordinator")
SPEAR_SHIELD_CREDS_DIR = os.environ.get("SPEAR_SHIELD_CREDS_DIR", "/secrets/spear-shield-console-creds")
SPEAR_COORDINATOR_CLIENT_SECRET_PATH = os.environ.get(
    "SPEAR_COORDINATOR_CLIENT_SECRET_PATH", "/secrets/spear-coordinator-client-secret/client-secret"
)

# architecture status + security demo pages, exec via this pod's own sa
# (manifests/spear-agents/08-console-backend-rbac.yaml), not the caller's token
AGENTS_NAMESPACE = os.environ.get("AGENTS_NAMESPACE", "spear-agents")
COORDINATOR_POD = os.environ.get("SPEAR_SHIELD_COORDINATOR_POD", "default--spear-coordinator")
RETRIEVAL_POD = os.environ.get("SPEAR_SHIELD_RETRIEVAL_POD", "default--spear-retrieval")
SPEAR_MCP_GATEWAY_URL = os.environ.get(
    "SPEAR_MCP_GATEWAY_URL",
    "http://spear-mcp-gateway-istio.spear-agents.svc.cluster.local:8080/mcp",
)
SPEAR_MCP_GATEWAY_HOST_HEADER = os.environ.get("SPEAR_MCP_GATEWAY_HOST_HEADER", "")
# same url spear-coordinator-agent's own main.py calls (coordinator-a2a-token-grant.yaml)
RETRIEVAL_A2A_INTERNAL_URL = os.environ.get(
    "RETRIEVAL_A2A_INTERNAL_URL",
    "http://spear-retrieval-a2a-gateway.spear-agents.svc.cluster.local:80",
)
# sandbox sidecar proxy address, matches agents/_shared/http_client.py
SANDBOX_PROXY_CURL = "curl -s -x http://127.0.0.1:3128"
# in cluster service name, no route needed
RAG_QUERY_RELAY_URL = "http://spear-shield-rag-query-relay.spear-data.svc.cluster.local:8080"


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


def mlflow_list_artifacts(run_id):
    url = f"{MLFLOW_URL}/api/2.0/mlflow/artifacts/list?run_id={run_id}"
    req = urllib.request.Request(url, headers=mlflow_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=30, context=_no_verify_ctx()) as resp:
        return json.loads(resp.read()).get("files", [])


def mlflow_get_run(run_id):
    # runs/get is a plain GET endpoint, unlike runs/search; a POST body 404s
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

    # every document keeps its raw-intake/ copy regardless of verdict, link is always valid
    for doc in report.get("ingestion", {}).get("quarantined_detail", []):
        doc["minio_url"] = minio_object_link(AUTORAG_RAW_BUCKET, f"raw-intake/{doc['filename']}")
    for entry in report.get("guardrails", {}).get("audit", []):
        entry["minio_url"] = minio_object_link(AUTORAG_RAW_BUCKET, f"raw-intake/{entry['filename']}")
    return report


# ---- eval cycle trend: reads what 11-submit-evalhub-jobs.sh already
# writes (create_eval_cycle_run.py / log_eval_to_mlflow.py), nothing new
# logged here, just shaped for a chart ----

def find_eval_results_experiment():
    experiments = mlflow_post("/api/2.0/mlflow/experiments/search", {"max_results": 100})
    return next(
        (e for e in experiments.get("experiments", []) if e["name"] == EVAL_RESULTS_EXPERIMENT_NAME),
        None,
    )


def recent_eval_cycles(max_results=10):
    # one entry per cycle, oldest first for a left to right chart; children
    # grouped by the mlflow.parentRunId tag every child run carries
    exp = find_eval_results_experiment()
    if exp is None:
        return []

    parents = mlflow_post(
        "/api/2.0/mlflow/runs/search",
        {
            "experiment_ids": [exp["experiment_id"]],
            "filter": "tags.`evalhub.cycle` = 'true'",
            "max_results": max_results,
            "order_by": ["attributes.start_time DESC"],
        },
    ).get("runs", [])

    cycles = []
    for parent in parents:
        parent_id = parent["info"]["run_id"]
        children = mlflow_post(
            "/api/2.0/mlflow/runs/search",
            {
                "experiment_ids": [exp["experiment_id"]],
                "filter": f"tags.`mlflow.parentRunId` = '{parent_id}'",
                "max_results": 20,
            },
        ).get("runs", [])

        benchmarks = []
        for child in children:
            data = child.get("data", {})
            tags = {t["key"]: t["value"] for t in data.get("tags", [])}
            metrics = {m["key"]: float(m["value"]) for m in data.get("metrics", [])}
            params = {p["key"]: p["value"] for p in data.get("params", [])}
            run_id = child["info"]["run_id"]
            provider_id = tags.get("evalhub.provider_id", "")

            # ibm-clear saves natively and never gets our provider_id tag;
            # that absence is the signal to look for its own html artifact
            report_path = None
            if not provider_id:
                for f in mlflow_list_artifacts(run_id):
                    if not f.get("is_dir") and f.get("path", "").endswith(".html"):
                        report_path = f["path"]
                        break

            benchmarks.append(
                {
                    "run_id": run_id,
                    # safe across all providers; ibm-clear has no provider_id
                    "job_name": tags.get("evalhub.job_name", ""),
                    # evalhub job id (not this mlflow run id), used by find_leak_detail_tag
                    "job_id": tags.get("evalhub.job_id", ""),
                    "provider_id": provider_id or params.get("provider_id", ""),
                    "benchmark_id": tags.get("evalhub.benchmark_id", "") or params.get("benchmark_id", ""),
                    "metrics": metrics,
                    "params": params,
                    "report_url": (
                        f"/trend/artifact?run_id={run_id}&path={urllib.parse.quote(report_path)}"
                        if report_path
                        else None
                    ),
                }
            )

        cycles.append(
            {
                "run_id": parent_id,
                "run_name": parent["info"].get("run_name"),
                "start_time": parent["info"].get("start_time"),
                "mlflow_url": mlflow_run_link(exp["experiment_id"], parent_id),
                "benchmarks": benchmarks,
            }
        )

    cycles.reverse()
    return cycles


GUARDRAILS_NS = os.environ.get("GUARDRAILS_NS", "spear-guardrails")
GUARDRAILS_CM = "coordinator-agent-config"


def find_leak_detail_tag(job_id):
    # adapter.py tags a small run with the real leaked text (evalhub's own
    # job api strips this detail, only the leak count survives there). a
    # clean scan creates no such run, that absence is the "nothing to patch" signal
    exp = find_eval_results_experiment()
    if exp is None:
        return None
    runs = mlflow_post(
        "/api/2.0/mlflow/runs/search",
        {
            "experiment_ids": [exp["experiment_id"]],
            "filter": f"tags.`evalhub.job_id` = '{job_id}'",
            "max_results": 5,
        },
    ).get("runs", [])
    for run in runs:
        tags = {t["key"]: t["value"] for t in run.get("data", {}).get("tags", [])}
        if "spear.leak_detail_json" in tags:
            return tags["spear.leak_detail_json"]
    return None


# mirrors the confidential data provider's own recognizers; catches
# paraphrases, not just exact reprints of the planted table cell
CANONICAL_CONFIDENTIAL_PATTERNS = [
    r"18\.5\s*%[^.\n]{0,40}rebate|rebate[^.\n]{0,40}18\.5\s*%",
    r"\$\s?2,?400,?000[^.\n]{0,40}(?:commitment|volume)|(?:commitment|volume)[^.\n]{0,40}\$\s?2,?400,?000",
]


def leak_patterns_from_detail(raw_detail_json):
    # re.escape turns a space into "\ "; collapse that exact run instead of
    # a raw \s+ pass, which would double escape and corrupt the pattern
    leaking_traces = json.loads(raw_detail_json)
    texts = sorted({hit["text"] for trace in leaking_traces for hit in trace.get("hits", [])})
    patterns = []
    for text in texts:
        escaped = re.escape(text.strip())
        patterns.append(re.sub(r"(?:\\ )+", r"\\s+", escaped))
    for canonical in CANONICAL_CONFIDENTIAL_PATTERNS:
        if canonical not in patterns:
            patterns.append(canonical)
    return patterns


def _regex_side_patterns(config, side):
    # create the side if a pre closed loop cm only had input rails
    rd = config.setdefault("rails", {}).setdefault("config", {}).setdefault("regex_detection", {})
    side_cfg = rd.setdefault(side, {})
    patterns = side_cfg.setdefault("patterns", [])
    side_cfg.setdefault("case_insensitive", True)
    return patterns


def _ensure_output_rails(config):
    # leak scanner scores answers, so tighten must enable output flows or
    # the new patterns never run against the model response
    sdd = config.setdefault("rails", {}).setdefault("config", {}).setdefault("sensitive_data_detection", {})
    out_entities = sdd.setdefault("output", {}).setdefault("entities", [])
    if not out_entities:
        out_entities.extend(sdd.get("input", {}).get("entities") or ["EMAIL_ADDRESS", "CREDIT_CARD", "US_SSN"])
    flows = config.setdefault("rails", {}).setdefault("output", {}).setdefault("flows", [])
    for name in ("detect sensitive data on output", "regex check output"):
        if name not in flows:
            flows.append(name)


def apply_closed_loop_patterns(job_id):
    # in process patch, not a Job: fast enough for a button click, a mlflow
    # read plus one configmap patch
    raw_detail = find_leak_detail_tag(job_id)
    if raw_detail is None:
        return {"added": [], "message": "no leak detail found for this job, nothing to patch"}

    candidates = leak_patterns_from_detail(raw_detail)
    cm = k8s_core_v1().read_namespaced_config_map(GUARDRAILS_CM, GUARDRAILS_NS)
    config = yaml.safe_load(cm.data["config.yaml"])
    _ensure_output_rails(config)

    added = []
    for side in ("input", "output"):
        patterns = _regex_side_patterns(config, side)
        existing = set(patterns)
        for p in candidates:
            if p not in existing:
                patterns.append(p)
                added.append(f"{side}:{p}")
                existing.add(p)

    if not added:
        return {"added": [], "message": f"all {len(candidates)} candidate pattern(s) already present on input and output"}

    new_yaml = yaml.safe_dump(config, default_flow_style=False, sort_keys=False)
    k8s_core_v1().patch_namespaced_config_map(
        GUARDRAILS_CM, GUARDRAILS_NS, {"data": {"config.yaml": new_yaml}}
    )
    return {"added": added, "message": f"added {len(added)} new pattern(s) to {GUARDRAILS_CM} (input and output)"}


# ---- kfp rest helpers, always the caller's forwarded token, never this pod's own ----

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


# the registered pipeline's own compiled spec is the source of truth for
# minio_endpoint/raw_bucket/raw_prefix, not a second hand copy of pipeline.py's
# defaults. no separate "get template" call exists; the version response
# embeds the full spec under pipeline_spec.pipeline_spec.root.inputDefinitions
_INGESTION_SOURCE_CACHE_TTL_SECONDS = 300
_ingestion_source_cache = {"values": None, "expires_at": 0.0}


def fetch_ingestion_pipeline_source_defaults(user_token):
    pipeline_id = find_pipeline_id(INGESTION_PIPELINE_NAME, user_token)
    version_id = find_latest_pipeline_version_id(pipeline_id, user_token)
    resp = kfp_get(f"/apis/v2beta1/pipelines/{pipeline_id}/versions/{version_id}", user_token)
    spec = resp.get("pipeline_spec", {}).get("pipeline_spec", {})
    params = spec.get("root", {}).get("inputDefinitions", {}).get("parameters", {})
    missing = [name for name in ("minio_endpoint", "raw_bucket", "raw_prefix") if name not in params]
    if missing:
        raise LookupError(f"registered pipeline spec is missing expected parameters: {missing}")
    return {
        "minio_endpoint": params["minio_endpoint"].get("defaultValue"),
        "bucket": params["raw_bucket"].get("defaultValue"),
        "prefix": params["raw_prefix"].get("defaultValue"),
    }


def ingestion_source_config(user_token):
    now = time.time()
    if _ingestion_source_cache["values"] and _ingestion_source_cache["expires_at"] > now:
        values = _ingestion_source_cache["values"]
    else:
        try:
            values = fetch_ingestion_pipeline_source_defaults(user_token)
            _ingestion_source_cache["values"] = values
            _ingestion_source_cache["expires_at"] = now + _INGESTION_SOURCE_CACHE_TTL_SECONDS
        except Exception:
            # render something rather than break the page; not cached, so
            # the next request retries the live lookup
            values = {
                "minio_endpoint": INGESTION_SOURCE_ENDPOINT,
                "bucket": INGESTION_SOURCE_BUCKET,
                "prefix": INGESTION_SOURCE_PREFIX,
            }
    return {
        **values,
        "manifest_url": minio_object_link(values["bucket"], f"{values['prefix']}manifest.json"),
        "console_url": MINIO_CONSOLE_URL,
    }


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
    # /pipelineRuns/:namespace/runs/:runId, a valid post login redirect target
    return f"{RHOAI_DASHBOARD_URL}/pipelineRuns/{WORKSPACE}/runs/{run_id}"


def mlflow_run_link(experiment_id, run_id):
    # mlflow's own tracking client default run link shape
    return f"{MLFLOW_URL}/#/experiments/{experiment_id}/runs/{run_id}"


def autorag_results_link(run_id):
    # gen-ai-studio's autorag results page for a completed run
    return f"{RHOAI_DASHBOARD_URL}/gen-ai-studio/autorag/results/{WORKSPACE}/{run_id}"


def minio_object_link(bucket, key):
    # minio console browser path; every '/' in key must be percent encoded
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
    # runs list has no pipeline name filter, so filter client side on the
    # display_name prefix every trigger uses
    resp = kfp_get(f"/apis/v2beta1/runs?sort_by=created_at%20desc&page_size=50", user_token)
    runs = resp.get("runs", [])
    matched = [r for r in runs if (r.get("display_name") or "").startswith(pipeline_name)]
    # gen-ai-studio results page only exists for autorag runs
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
    # mlflow only gets a run once log_ingestion_report writes it; kfp has
    # every in flight run. merge the two by batch_id: kfp display_name is
    # f"{INGESTION_PIPELINE_NAME}-{batch_id}", mlflow run_name is the batch_id
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
    # no single leaderboard artifact; per pattern scores live as
    # rag_patterns/Pattern<N>/pattern.json under the run's own
    # rag-templates-optimization task in minio. the dag task_id in
    # run_details does not reliably match the minio folder's task_id (kfp
    # keeps them as separate tree nodes), so ask minio directly instead
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
            # optimization_metric decides the winner, but keep the other
            # named scores too (faithfulness, answer_correctness, etc), worth showing
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


# ---- spear shield chat


def _k8s_api_ctx():
    # real in cluster apiserver ca, not the skip verification context used elsewhere
    return ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")


def openshift_username(user_token):
    # self lookup any token can do, never trust a client supplied username
    req = urllib.request.Request(
        "https://kubernetes.default.svc/apis/user.openshift.io/v1/users/~",
        headers={"Authorization": f"Bearer {user_token}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10, context=_k8s_api_ctx()) as resp:
        return json.loads(resp.read())["metadata"]["name"]


def persona_password(username):
    # one file per username, same shape as spear-openproject-mcp's OPENPROJECT_TOKENS_DIR
    path = os.path.join(SPEAR_SHIELD_CREDS_DIR, username)
    if not os.path.isfile(path):
        raise LookupError(f"no spear-agents realm password on file for {username!r}")
    with open(path) as f:
        return f.read().strip()


def mint_spear_shield_token(username, password):
    data = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": SPEAR_COORDINATOR_CLIENT_ID,
            "client_secret": open(SPEAR_COORDINATOR_CLIENT_SECRET_PATH).read().strip(),
            "username": username,
            "password": password,
            "scope": "openid",
        }
    ).encode()
    req = urllib.request.Request(
        f"{SPEAR_SHIELD_ISSUER_URL}/protocol/openid-connect/token", data=data, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15, context=_no_verify_ctx()) as resp:
        return json.loads(resp.read())["access_token"]


def ask_spear_shield(message_text, user_token):
    username = openshift_username(user_token)
    password = persona_password(username)
    shield_token = mint_spear_shield_token(username, password)

    rpc_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "callerToken": shield_token,
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": message_text}],
                "messageId": f"console-{int(time.time() * 1000)}",
            },
        },
    }
    req = urllib.request.Request(
        SPEAR_COORDINATOR_A2A_URL,
        data=json.dumps(rpc_body).encode(),
        headers={
            "Content-Type": "application/json",
            # gateway authpolicy needs a bearer token too; coordinator code reads callerToken instead
            "Authorization": f"Bearer {shield_token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90, context=_no_verify_ctx()) as resp:
        rpc_response = json.loads(resp.read())

    if "error" in rpc_response:
        raise RuntimeError(rpc_response["error"].get("message", "spear shield returned an error"))

    result = rpc_response.get("result", {})
    artifacts = result.get("artifacts", [])
    texts = [
        part["text"]
        for artifact in artifacts
        for part in artifact.get("parts", [])
        if part.get("kind") == "text"
    ]
    answer_text = "\n\n".join(texts) if texts else "no answer came back from spear shield"
    # real trace from a2a_server.py's completed_task, measured wall clock durations
    return answer_text, result.get("trace") or []


# ---- architecture status + security demo, shared k8s helpers ----

_k8s_core_v1 = None


def k8s_core_v1():
    global _k8s_core_v1
    if _k8s_core_v1 is None:
        k8s_config.load_incluster_config()
        _k8s_core_v1 = k8s_client.CoreV1Api()
    return _k8s_core_v1


_k8s_custom_objects = None


def k8s_custom_objects():
    global _k8s_custom_objects
    if _k8s_custom_objects is None:
        k8s_config.load_incluster_config()
        _k8s_custom_objects = k8s_client.CustomObjectsApi()
    return _k8s_custom_objects


def live_cluster_spiffeid(name):
    # read live, not a copy of 06-cluster-spiffeid.yaml's text
    obj = k8s_custom_objects().get_cluster_custom_object("spire.spiffe.io", "v1alpha1", "clusterspiffeids", name)
    return obj.get("spec", {})


def live_authpolicy(namespace, name):
    # read live, not quoted from 07-a2a-gateway.yaml
    obj = k8s_custom_objects().get_namespaced_custom_object("kuadrant.io", "v1", namespace, "authpolicies", name)
    return obj.get("spec", {})


def live_keycloak_client(client_id):
    # read only, same admin-cli grant as ensure_spiffe_federated_auth; scoped
    # rbac for this one secret in manifests/spear-agents/10-console-backend-pathway-rbac.yaml
    secret = k8s_core_v1().read_namespaced_secret("keycloak-initial-admin", "keycloak")
    admin_user = base64.b64decode(secret.data["username"]).decode()
    admin_pass = base64.b64decode(secret.data["password"]).decode()
    kc_base = SPEAR_SHIELD_ISSUER_URL.rsplit("/realms/", 1)[0]
    realm = SPEAR_SHIELD_ISSUER_URL.rsplit("/", 1)[-1]
    token_req = urllib.request.Request(
        f"{kc_base}/realms/master/protocol/openid-connect/token",
        data=urllib.parse.urlencode(
            {"grant_type": "password", "client_id": "admin-cli", "username": admin_user, "password": admin_pass}
        ).encode(),
        method="POST",
    )
    with urllib.request.urlopen(token_req, timeout=15, context=_no_verify_ctx()) as resp:
        admin_token = json.loads(resp.read())["access_token"]
    lookup_req = urllib.request.Request(
        f"{kc_base}/admin/realms/{realm}/clients?clientId={client_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    with urllib.request.urlopen(lookup_req, timeout=15, context=_no_verify_ctx()) as resp:
        clients = json.loads(resp.read())
    if not clients:
        raise LookupError(f"keycloak client {client_id!r} not found in realm {realm!r}")
    return clients[0]


def resolve_spire_agent_pod(coordinator_pod):
    # one spire-agent daemonset pod per node; only the one sharing this
    # node holds the relevant log, resolved by node name
    node_name = k8s_core_v1().read_namespaced_pod(coordinator_pod, AGENTS_NAMESPACE).spec.node_name
    agents = k8s_core_v1().list_namespaced_pod(
        "zero-trust-workload-identity-manager", label_selector="app.kubernetes.io/name=spire-agent"
    )
    agent_pod = next((p.metadata.name for p in agents.items if p.spec.node_name == node_name), None)
    if agent_pod is None:
        raise LookupError(f"no spire-agent pod found on node {node_name!r}")
    return agent_pod


def find_svid_log_line(agent_pod, spiffe_id):
    # first issuance logs "Creating X509-SVID", renewals (~30 min later)
    # log "Renewing" instead; both count as evidence spire is managing this
    # identity. matching only "Creating" was the bug, it ages out of any
    # bounded tail fast. note: the jwt-svid the token_grant provider fetches
    # is not logged at this verbosity, grepping for "jwt" here finds nothing
    log_text = k8s_core_v1().read_namespaced_pod_log(
        agent_pod, "zero-trust-workload-identity-manager", tail_lines=1000
    )
    matches = [
        line
        for line in log_text.splitlines()
        if spiffe_id in line and ("Creating X509-SVID" in line or "Renewing X509-SVID" in line)
    ]
    if not matches:
        raise LookupError(f"no recent X509-SVID line for {spiffe_id!r} in {agent_pod!r}'s own log")
    return matches[-1]


def k8s_exec(pod, command, timeout=25, api=None):
    # runs a shell command in the sandbox's "agent" container, returns combined
    # stdout/stderr. callers running two of these concurrently must each
    # pass their own client, see _fresh_core_v1
    api = api or k8s_core_v1()
    return k8s_stream(
        api.connect_get_namespaced_pod_exec,
        pod,
        AGENTS_NAMESPACE,
        container="agent",
        command=["sh", "-c", command],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
        _preload_content=True,
        _request_timeout=timeout,
    ).strip()


def _fresh_core_v1():
    # concurrent execs on the shared module level client corrupt each
    # other's connection pool; each side of a parallel pair needs its own client
    k8s_config.load_incluster_config()
    return k8s_client.CoreV1Api()


def k8s_pod_status(pod):
    p = k8s_core_v1().read_namespaced_pod(pod, AGENTS_NAMESPACE)
    statuses = p.status.container_statuses or []
    return {
        "name": pod,
        "phase": p.status.phase,
        "ready": bool(statuses) and all(c.ready for c in statuses),
        "runtime_class": p.spec.runtime_class_name or "runc (default)",
        "node": p.spec.node_name,
    }


def _probe(url, headers=None, method="GET", timeout=8):
    # a real, short, read only reachability probe, used by the
    # architecture status page's live badges below. any http response at
    # all, even an error one, counts as "reachable", only a connection
    # failure or timeout counts as down
    try:
        req = urllib.request.Request(url, headers=headers or {}, method=method)
        with urllib.request.urlopen(req, timeout=timeout, context=_no_verify_ctx()) as resp:
            return True, resp.status
    except urllib.error.HTTPError as exc:
        return True, exc.code
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def spear_shield_status():
    coordinator = k8s_pod_status(COORDINATOR_POD)
    retrieval = k8s_pod_status(RETRIEVAL_POD)
    keycloak_ok, keycloak_detail = _probe(f"{SPEAR_SHIELD_ISSUER_URL}/.well-known/openid-configuration")
    a2a_ok, a2a_detail = _probe(SPEAR_COORDINATOR_A2A_URL, method="POST")
    mcp_ok, mcp_detail = _probe(
        SPEAR_MCP_GATEWAY_URL,
        headers={"host": SPEAR_MCP_GATEWAY_HOST_HEADER} if SPEAR_MCP_GATEWAY_HOST_HEADER else {},
        method="POST",
    )
    guardrails_ok, guardrails_detail = _probe(f"{GUARDRAILS_ROUTE}/v1/chat/completions", method="POST")
    return {
        "components": [
            {
                "id": "keycloak",
                "label": "keycloak, spear-agents realm",
                "ok": keycloak_ok,
                "detail": str(keycloak_detail),
            },
            {
                "id": "coordinator",
                "label": "spear-coordinator sandbox, runc",
                "ok": coordinator["phase"] == "Running" and coordinator["ready"],
                "detail": f"phase={coordinator['phase']} ready={coordinator['ready']} runtimeClass={coordinator['runtime_class']} node={coordinator['node']}",
            },
            {
                "id": "retrieval",
                "label": "spear-retrieval sandbox, kata",
                "ok": retrieval["phase"] == "Running" and retrieval["ready"],
                "detail": f"phase={retrieval['phase']} ready={retrieval['ready']} runtimeClass={retrieval['runtime_class']} node={retrieval['node']}",
            },
            {
                "id": "a2a-gateway",
                "label": "a2a gateway, spire workload identity + kuadrant authpolicy",
                "ok": a2a_ok,
                "detail": str(a2a_detail),
            },
            {
                "id": "mcp-gateway",
                "label": "mcp gateway, per tool rbac",
                "ok": mcp_ok,
                "detail": str(mcp_detail),
            },
            {
                "id": "guardrails",
                "label": "ogx guardrails rails",
                "ok": guardrails_ok,
                "detail": str(guardrails_detail),
            },
        ],
        "same_node": coordinator["node"] == retrieval["node"] and coordinator["node"] is not None,
    }


def _mcp_tool_text(response):
    # both a gateway rbac rejection and an assignee=self rejection inside
    # spear-openproject-mcp land as a top level json-rpc error; only a
    # genuine success reaches result.content
    if "error" in response:
        return response["error"].get("message", json.dumps(response["error"]))
    content = (response.get("result") or {}).get("content") or []
    return content[0].get("text") if content else json.dumps(response.get("result") or response)


def mcp_tool_call(name, arguments, caller_token):
    headers = {
        "authorization": f"Bearer {caller_token}",
        "host": SPEAR_MCP_GATEWAY_HOST_HEADER,
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    init_body = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "owner-console-backend", "version": "1.0"},
        },
    }
    req = urllib.request.Request(
        SPEAR_MCP_GATEWAY_URL, data=json.dumps(init_body).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15, context=_no_verify_ctx()) as resp:
        session_id = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
    if session_id:
        headers["mcp-session-id"] = session_id

    call_body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    req = urllib.request.Request(
        SPEAR_MCP_GATEWAY_URL, data=json.dumps(call_body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=_no_verify_ctx()) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
    if raw.startswith("event:"):
        for line in raw.splitlines():
            if line.startswith("data: "):
                raw = line[6:]
                break
    return json.loads(raw) if raw else {}


# ---- under the hood demo scenarios, each a repeatable version of a live
# check against the real cluster. abac scoping and prompt injection
# refusal need no scenario here, /spear-shield/ask already demonstrates
# both. tool response injection defense is still only reachable via the
# real chat, no demo button yet. kata vs runc, network and filesystem
# containment are covered together by demo_sandbox_compare below ----


def demo_identity_rejected():
    rpc_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "callerToken": "not-a-real-token",
            "message": {"role": "user", "parts": [{"kind": "text", "text": "what are my open work packages"}]},
        },
    }
    req = urllib.request.Request(
        SPEAR_COORDINATOR_A2A_URL,
        data=json.dumps(rpc_body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer not-a-real-token"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=_no_verify_ctx()) as resp:
            status, detail = resp.status, resp.read().decode()[:300]
    except urllib.error.HTTPError as exc:
        status, detail = exc.code, exc.read().decode()[:300]
    return {
        "title": "wrong identity rejected at the a2a gateway",
        "steps": [
            {
                "label": "send a garbage bearer token straight at the coordinator's own a2a route",
                "command": f"POST {SPEAR_COORDINATOR_A2A_URL}\nAuthorization: Bearer not-a-real-token",
                "output": f"HTTP {status}\n{detail}",
                "ok": status in (401, 403),
            }
        ],
    }


def _exec_both(command, timeout=25):
    # same command against both sandboxes at once, neither waits on the
    # other; each side gets its own api client, see _fresh_core_v1
    with ThreadPoolExecutor(max_workers=2) as pool:
        c_future = pool.submit(k8s_exec, COORDINATOR_POD, command, timeout, _fresh_core_v1())
        r_future = pool.submit(k8s_exec, RETRIEVAL_POD, command, timeout, _fresh_core_v1())
        return c_future.result(), r_future.result()


# marker printed right before each command in a batch, chosen to be
# something real command output is never going to produce by accident
_BATCH_MARK = "@@PROBE@@"


def _exec_batch(pod, commands, timeout=40, api=None):
    # all probes for one pod in a single exec instead of one round trip
    # each; marker printed ahead of every command so output splits back apart
    script = "".join(f'echo "{_BATCH_MARK}{i}"\n{cmd}\n' for i, cmd in enumerate(commands))
    raw = k8s_exec(pod, script, timeout, api=api)
    outputs = [""] * len(commands)
    for chunk in raw.split(_BATCH_MARK)[1:]:
        index, _, output = chunk.partition("\n")
        if index.strip().isdigit():
            outputs[int(index)] = output.strip()
    return outputs


def _exec_both_batch(commands, timeout=40):
    # same lockstep pattern as _exec_both, batched per sandbox
    with ThreadPoolExecutor(max_workers=2) as pool:
        c_future = pool.submit(_exec_batch, COORDINATOR_POD, commands, timeout, _fresh_core_v1())
        r_future = pool.submit(_exec_batch, RETRIEVAL_POD, commands, timeout, _fresh_core_v1())
        return c_future.result(), r_future.result()


def _reachable(curl_output):
    # http_code=000 (or no http_code at all) means no response ever came back
    return "http_code=" in curl_output and "http_code=000" not in curl_output


def _annotate_blocked(curl_output):
    # http_code=000 means the sidecar proxy refused the connect outright,
    # destination is off this sandbox's egress allow list
    if "http_code=000" in curl_output:
        return curl_output + "\nblocked before any response: destination is not on this sandbox's own egress allow list"
    return curl_output


def _egress_probe_cmd(url):
    return (
        SANDBOX_PROXY_CURL
        + " --cacert /etc/openshell-tls/proxy/ca-bundle.pem"
        + f' -o /dev/null -w "http_code=%{{http_code}}" "{url}"'
    )


def _probe_result(probe_id, label, command, c_output, r_output, signal_fn=lambda o: o):
    return {
        "id": probe_id,
        "label": label,
        "command": command,
        "coordinator_output": c_output,
        "retrieval_output": r_output,
        "verdict": "same" if signal_fn(c_output) == signal_fn(r_output) else "differs",
    }


def _group_runtime_isolation(coordinator, retrieval):
    kernel_c, kernel_r = _exec_both("uname -r")
    return {
        "id": "runtime-isolation",
        "label": "Runtime Isolation",
        "probes": [
            _probe_result(
                "runtime-class",
                "which runtime is this pod",
                "oc get pod {pod} -o jsonpath='{.spec.runtimeClassName}'",
                coordinator["runtime_class"],
                retrieval["runtime_class"],
            ),
            _probe_result("uname", "uname -r", "uname -r", kernel_c, kernel_r),
        ],
    }


def _group_resource_visibility(_coordinator, _retrieval):
    # kata boots its own micro vm so nproc/free report only what the vm got,
    # not the host's; expected to differ, that is the point of this probe
    footing_c, footing_r = _exec_both("echo cpus=$(nproc); free -h")
    return {
        "id": "resource-visibility",
        "label": "Resource Visibility",
        "probes": [_probe_result("cpu-mem", "nproc + free -h", "nproc; free -h", footing_c, footing_r)],
    }


def _group_common_containment(_coordinator, _retrieval):
    # two probes, one exec per pod rather than two, see _exec_batch
    commands = [
        f'{SANDBOX_PROXY_CURL} -o /dev/null -w "http_code=%{{http_code}}" https://1.1.1.1',
        "rm -f /usr/bin/ls /usr/bin/cat 2>&1; echo exit=$?",
    ]
    coordinator_out, retrieval_out = _exec_both_batch(commands)
    host_c, rm_c = coordinator_out
    host_r, rm_r = retrieval_out
    return {
        "id": "common-containment",
        "label": "Common Containment",
        "probes": [
            _probe_result(
                "arbitrary-host",
                "curl an arbitrary internet host",
                "curl -x 127.0.0.1:3128 https://1.1.1.1",
                _annotate_blocked(host_c),
                _annotate_blocked(host_r),
                _reachable,
            ),
            _probe_result(
                "delete-binaries",
                "delete real system binaries",
                "rm -f /usr/bin/ls /usr/bin/cat",
                rm_c,
                rm_r,
                lambda o: "Permission denied" in o,
            ),
        ],
    }


def _group_least_privilege_egress(_coordinator, _retrieval):
    commands = [
        _egress_probe_cmd(f"{GUARDRAILS_ROUTE}/v1/chat/completions"),
        _egress_probe_cmd(f"{RAG_QUERY_RELAY_URL}/healthz"),
    ]
    coordinator_out, retrieval_out = _exec_both_batch(commands)
    guardrails_c, relay_c = coordinator_out
    guardrails_r, relay_r = retrieval_out
    return {
        "id": "least-privilege-egress",
        "label": "Least Privilege Egress",
        "probes": [
            _probe_result(
                "curl-guardrails",
                "curl the guardrails route",
                f'curl -x 127.0.0.1:3128 "{GUARDRAILS_ROUTE}/v1/chat/completions"',
                _annotate_blocked(guardrails_c),
                _annotate_blocked(guardrails_r),
                _reachable,
            ),
            _probe_result(
                "curl-rag-relay",
                "curl the rag-query-relay route",
                f'curl -x 127.0.0.1:3128 "{RAG_QUERY_RELAY_URL}/healthz"',
                _annotate_blocked(relay_c),
                _annotate_blocked(relay_r),
                _reachable,
            ),
        ],
    }


# declared in display order, a run all walks this same order so a single
# group run and a run all land identically either way
COMPARE_GROUP_IDS = ["runtime-isolation", "resource-visibility", "common-containment", "least-privilege-egress"]
_COMPARE_GROUP_BUILDERS = {
    "runtime-isolation": _group_runtime_isolation,
    "resource-visibility": _group_resource_visibility,
    "common-containment": _group_common_containment,
    "least-privilege-egress": _group_least_privilege_egress,
}


def demo_sandbox_identity():
    # no exec involved, cheap enough to call on page load
    coordinator = k8s_pod_status(COORDINATOR_POD)
    retrieval = k8s_pod_status(RETRIEVAL_POD)
    return {
        "namespace": AGENTS_NAMESPACE,
        "coordinator": {"pod": COORDINATOR_POD, **coordinator},
        "retrieval": {"pod": RETRIEVAL_POD, **retrieval},
    }


def demo_sandbox_compare(group_id=None):
    # all groups when group_id is empty, otherwise just the one pressed.
    # verdict (same/differs) is computed live, never pre labeled; "differs"
    # is not automatically a problem, kata vs runc is supposed to differ
    coordinator = k8s_pod_status(COORDINATOR_POD)
    retrieval = k8s_pod_status(RETRIEVAL_POD)
    group_ids = [group_id] if group_id else COMPARE_GROUP_IDS
    groups = [_COMPARE_GROUP_BUILDERS[g](coordinator, retrieval) for g in group_ids]
    return {
        "namespace": AGENTS_NAMESPACE,
        "coordinator": {"pod": COORDINATOR_POD, **coordinator},
        "retrieval": {"pod": RETRIEVAL_POD, **retrieval},
        "groups": groups,
    }


def demo_exec_both_command(command):
    # the free typed companion to the groups above, same two pods, same
    # parallel exec, whatever is typed runs for real against both at once
    c_output, r_output = _exec_both(command)
    return {"command": command, "coordinator_output": c_output, "retrieval_output": r_output}


def _workload_identity_pathway_steps():
    # four atomic steps, each a real command and raw output read live off
    # the cluster, never typed into this file
    steps = []

    spiffeid_spec = live_cluster_spiffeid("spear-agents-sandboxes")
    steps.append(
        {
            "label": "spire's own ClusterSPIFFEID, the identity template this sandbox is matched against",
            "command": "oc get clusterspiffeid spear-agents-sandboxes -o jsonpath='{.spec}'",
            "output": json.dumps(spiffeid_spec, indent=2),
            "ok": bool(spiffeid_spec),
        }
    )
    # trust domain only exists inlined in spiffeIDTemplate's own spiffe://.../ns/... string
    template_match = re.match(r"spiffe://([^/]+)/", spiffeid_spec.get("spiffeIDTemplate", ""))
    trust_domain = template_match.group(1) if template_match else ""
    coordinator_hash = next(
        (
            m.get("values", [""])[0]
            for m in spiffeid_spec.get("podSelector", {}).get("matchExpressions", [])
            if m.get("key") == "agents.x-k8s.io/sandbox-name-hash"
        ),
        "",
    )
    coordinator_spiffe_id = f"spiffe://{trust_domain}/ns/{AGENTS_NAMESPACE}/sandbox/{coordinator_hash}"

    # resolve the real pod name first so a no-match failure still shows what was actually checked
    try:
        agent_pod = resolve_spire_agent_pod(COORDINATOR_POD)
        command = f"oc logs -n zero-trust-workload-identity-manager {agent_pod} --tail=1000 | grep '{coordinator_spiffe_id}'"
        try:
            svid_log_line = find_svid_log_line(agent_pod, coordinator_spiffe_id)
            output, ok = svid_log_line.strip(), True
        except Exception as exc:  # noqa: BLE001
            output, ok = f"no match this call: {exc}", False
    except Exception as exc:  # noqa: BLE001
        command = "oc get pods -n zero-trust-workload-identity-manager -l app.kubernetes.io/name=spire-agent -o wide"
        output, ok = f"could not resolve the coordinator's own node's spire-agent pod: {exc}", False
    steps.append(
        {
            "label": f"spire-agent's own log, real X509-SVID issuance for {coordinator_spiffe_id}",
            "command": command,
            "output": output,
            "ok": ok,
        }
    )
    # honest gap: spire-agent logs X509-SVID issuance, not the jwt-svid the
    # token grant below actually fetches; the line above only proves spire
    # is managing this identity, not that a jwt-svid fetch happened

    kc_base = SPEAR_SHIELD_ISSUER_URL.rsplit("/realms/", 1)[0]
    realm = SPEAR_SHIELD_ISSUER_URL.rsplit("/", 1)[-1]
    client = live_keycloak_client("spear-coordinator-workload")
    # read only admin inspection, openshell never calls this endpoint; the
    # real exchange is a separate client_credentials POST with a
    # jwt-spiffe client_assertion, see coordinator-a2a-token-grant.yaml.
    # not reproduced here, it needs a real spire issued svid, an admin
    # credential cannot stand in for that
    steps.append(
        {
            "label": (
                "keycloak's own client config, admin api read only, this never mints a token, "
                f"the real exchange is a separate POST to {realm}/protocol/openid-connect/token "
                "with the spire svid as client_assertion, see coordinator-a2a-token-grant.yaml"
            ),
            "command": f"curl -s '{kc_base}/admin/realms/{realm}/clients?clientId=spear-coordinator-workload' -H 'Authorization: Bearer <admin token>'",
            "output": json.dumps(
                {
                    "clientId": client.get("clientId"),
                    "clientAuthenticatorType": client.get("clientAuthenticatorType"),
                    "attributes": client.get("attributes"),
                },
                indent=2,
            ),
            "ok": bool(client),
        }
    )

    authpolicy_spec = live_authpolicy(AGENTS_NAMESPACE, "spear-retrieval-a2a-auth")
    steps.append(
        {
            "label": "the retrieval gateway's own real AuthPolicy, the rule that actually accepts this token",
            "command": "oc get authpolicy spear-retrieval-a2a-auth -n spear-agents -o jsonpath='{.spec.rules.authentication}'",
            "output": json.dumps(authpolicy_spec.get("rules", {}).get("authentication", {}), indent=2),
            "ok": bool(authpolicy_spec),
        }
    )
    return steps


def demo_workload_identity():
    # a pass/fail contrast, not a claims readout. openshell strips any
    # caller presented authorization header before the sandboxed app sees
    # it, so the retrieval agent's inbound header is not usable proof.
    # instead this shows the same naked call two ways: from outside any
    # sandbox with no token (rejected by the authpolicy) versus the real
    # coordinator process with no token of its own (succeeds on identity)
    try:
        pathway_steps = _workload_identity_pathway_steps()
    except Exception as exc:  # noqa: BLE001
        pathway_steps = [
            {
                "label": "the real pathway this hop actually uses, read live off the cluster",
                "command": "see output",
                "output": f"could not read the live pathway off the cluster: {exc}",
                "ok": False,
            }
        ]

    naked_body = {"jsonrpc": "2.0", "id": 1, "method": "whoami", "params": {}}
    naked_req = urllib.request.Request(
        RETRIEVAL_A2A_INTERNAL_URL,
        data=json.dumps(naked_body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(naked_req, timeout=15, context=_no_verify_ctx()) as resp:
            naked_status, naked_detail = resp.status, resp.read().decode()[:300]
    except urllib.error.HTTPError as exc:
        naked_status, naked_detail = exc.code, exc.read().decode()[:300]

    # the authpolicy still gates this outer hop; any persona works since
    # whoami-downstream checks no caller identity itself
    gateway_token = mint_spear_shield_token("balakrishnan.b", persona_password("balakrishnan.b"))
    downstream_body = {"jsonrpc": "2.0", "id": 1, "method": "whoami-downstream", "params": {}}
    downstream_req = urllib.request.Request(
        SPEAR_COORDINATOR_A2A_URL,
        data=json.dumps(downstream_body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {gateway_token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(downstream_req, timeout=20, context=_no_verify_ctx()) as resp:
            outer = json.loads(resp.read())
        inner_text = outer["result"]["artifacts"][0]["parts"][0]["text"]
        downstream_succeeded = json.loads(inner_text).get("result", {}).get("artifacts", [{}])[0].get(
            "parts", [{}]
        )[0].get("text") == json.dumps({"reached": True})
        downstream_detail = inner_text
    except Exception as exc:  # noqa: BLE001
        downstream_succeeded, downstream_detail = False, str(exc)

    return {
        "title": "workload identity, this hop authenticates as this exact sandbox, not a shared secret",
        "steps": pathway_steps
        + [
            {
                "label": "the same call, made from outside any sandbox, with no token at all, rejected",
                "command": f"curl -sk -X POST {RETRIEVAL_A2A_INTERNAL_URL} -H 'Content-Type: application/json' -d '{json.dumps(naked_body)}'",
                "output": f"HTTP {naked_status}\n{naked_detail}",
                "ok": naked_status in (401, 403),
            },
            {
                "label": "the real coordinator process makes the identical call, no token of its own, succeeds",
                "command": (
                    f"curl -sk -X POST {SPEAR_COORDINATOR_A2A_URL} -H 'Authorization: Bearer <real token>' "
                    f"-H 'Content-Type: application/json' -d '{json.dumps(downstream_body)}'"
                ),
                "output": downstream_detail,
                "ok": downstream_succeeded,
            },
        ],
    }


def demo_tool_scope_and_assignee():
    bala_token = mint_spear_shield_token("balakrishnan.b", persona_password("balakrishnan.b"))
    sid_token = mint_spear_shield_token("siddhartha.de", persona_password("siddhartha.de"))

    # #38 assigned to balakrishnan.b, #39 to siddhartha.de
    step1 = _mcp_tool_text(mcp_tool_call("openproject_update_work_package", {"id": 38, "status": "in progress"}, bala_token))
    step2 = _mcp_tool_text(mcp_tool_call("openproject_update_work_package", {"id": 38, "status": "in progress"}, sid_token))
    step3 = _mcp_tool_text(mcp_tool_call("openproject_update_work_package", {"id": 39, "status": "in progress"}, sid_token))

    return {
        "title": "two layers, three real callers: gateway tool rbac, then the project's own assignee=self check",
        "steps": [
            {
                "label": "balakrishnan.b (search only role) tries update_work_package on #38, his own item",
                "command": "tools/call openproject_update_work_package {id: 38} as balakrishnan.b",
                "output": step1,
                "ok": "forbidden" in step1.lower() or "insufficient" in step1.lower(),
            },
            {
                "label": "siddhartha.de (holds the update role) tries update_work_package on #38, balakrishnan's own item",
                "command": "tools/call openproject_update_work_package {id: 38} as siddhartha.de",
                "output": step2,
                "ok": "not assigned to siddhartha.de" in step2,
            },
            {
                "label": "siddhartha.de updates #39, her own item, genuinely succeeds",
                "command": "tools/call openproject_update_work_package {id: 39} as siddhartha.de",
                "output": step3,
                "ok": "not assigned" not in step3 and "forbidden" not in step3.lower(),
            },
        ],
    }


DEMO_SCENARIOS = {
    "identity-rejected": demo_identity_rejected,
    "sandbox-compare": demo_sandbox_compare,
    "workload-identity": demo_workload_identity,
    "tool-scope-and-assignee": demo_tool_scope_and_assignee,
}


class Handler(BaseHTTPRequestHandler):
    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
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
            elif path == "/ingestion/source-config":
                # read only, no secret names or credentials, see ingestion_source_config
                self._json(200, ingestion_source_config(self._user_token()))
            elif path == "/spear-shield/status":
                self._json(200, spear_shield_status())
            elif path == "/spear-shield/sandbox-identity":
                self._json(200, demo_sandbox_identity())
            elif path == "/trend/eval-cycles":
                limit = int(query.get("limit", 10))
                self._json(200, {"cycles": recent_eval_cycles(limit)})
            elif path == "/trend/artifact":
                run_id = query.get("run_id", "")
                artifact_path = query.get("path", "")
                # reject ".." outright, don't rely on mlflow's own server side scoping
                if not run_id or not artifact_path or ".." in artifact_path:
                    self._json(400, {"error": "run_id and path query params are required"})
                else:
                    content_type = "text/html" if artifact_path.endswith(".html") else "application/octet-stream"
                    self._raw(200, mlflow_get_artifact(run_id, artifact_path), content_type)
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
                # timestamp keeps auto generated batch_ids unique; a fixed
                # fallback would collide on a second click with the field left blank
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
            elif path == "/trigger/closed-loop":
                # job_id comes from the trend page's leak card, no new scan triggered here
                job_id = (body.get("job_id") or "").strip()
                if not job_id:
                    self._json(400, {"error": "job_id is required"})
                    return
                self._json(200, apply_closed_loop_patterns(job_id))
            elif path == "/spear-shield/ask":
                message_text = (body.get("message") or "").strip()
                if not message_text:
                    self._json(400, {"error": "message is required"})
                    return
                answer, trace = ask_spear_shield(message_text, self._user_token())
                self._json(200, {"answer": answer, "trace": trace})
            elif path == "/spear-shield/demo-exec-both":
                command = (body.get("command") or "").strip()
                if not command:
                    self._json(400, {"error": "command is required"})
                    return
                self._json(200, demo_exec_both_command(command))
            elif path.startswith("/spear-shield/demo/"):
                scenario_id = path[len("/spear-shield/demo/"):]
                scenario_fn = DEMO_SCENARIOS.get(scenario_id)
                if scenario_fn is None:
                    self._json(404, {"error": f"unknown demo scenario: {scenario_id!r}"})
                    return
                if scenario_id == "sandbox-compare":
                    # empty group runs all; a named one runs just that group
                    group_id = (body.get("group") or "").strip() or None
                    if group_id and group_id not in COMPARE_GROUP_IDS:
                        self._json(404, {"error": f"unknown compare group: {group_id!r}"})
                        return
                    self._json(200, scenario_fn(group_id))
                else:
                    self._json(200, scenario_fn())
            else:
                self._json(404, {"error": "Not found"})
        except PermissionError as exc:
            self._json(401, {"error": str(exc)})
        except LookupError as exc:
            self._json(403, {"error": str(exc)})
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

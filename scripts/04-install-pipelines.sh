#!/usr/bin/env bash
set -euo pipefail
# the project pipeline server (autoML/autorag pipelines enabled) plus the
# shared mlflow instance the ingestion pipeline's log-ingestion-report
# step logs to, folded into one script since both live in spear-pipelines
# and the second genuinely depends on the first's own minio-dsp-creds.
# needs minio-dsp-creds in spear-pipelines (created by
# 02-install-storage.sh) and pgvector (03-install-data.sh), run both first.
#
# confirmed live 2026-09-25: the mlflow operator only ever manages one
# cluster wide instance named exactly "mlflow", with its workloads always
# landing in redhat-ods-applications regardless of what namespace the cr
# is created in, see the header comment in
# manifests/spear-pipelines/03-mlflow.yaml for the full explanation. this
# script therefore creates mlflow's own secrets in redhat-ods-applications,
# not spear-pipelines. also confirmed live: this build layers a custom
# multi tenant "workspace" concept on top of upstream mlflow, every api
# call 400s with "Workspace context is required" until the calling
# namespace is labeled to match spec.workspaceLabelSelector and the
# request carries an X-MLflow-Workspace header naming it, both handled
# below.
#
# NAMESPACE_MIGRATION_PLAN.md: the workspace label key itself renamed
# from rag-phase1.io/mlflow-workspace to spear.io/mlflow-workspace, and
# now applies to spear-pipelines, not rag-phase1, since that is the
# namespace whose pipeline-runner sa actually calls mlflow

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

SHARED_NS="redhat-ods-applications"
DSPA_NAME="rag-phase1-dspa"

wait_dspa_ready() {
  local tries=60 status=""
  while (( tries > 0 )); do
    status=$(oc get dspa "${DSPA_NAME}" -n "${PIPELINES_NS}" \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
    if [[ "${status}" == "True" ]]; then
      log "${DSPA_NAME} is Ready"
      return 0
    fi
    sleep 10
    tries=$((tries - 1))
  done
  echo "warning: ${DSPA_NAME} did not report Ready in time." >&2
  echo "warning: inspect with: oc get dspa ${DSPA_NAME} -n ${PIPELINES_NS} -o yaml" >&2
  echo "warning: and: oc get pods -n ${PIPELINES_NS}" >&2
}

ensure_mlflow_db() {
  local exists
  exists=$(oc exec deploy/pgvector -n "${DATA_NS}" -- \
    psql -U raguser -d ragdb -tAc "SELECT 1 FROM pg_database WHERE datname='mlflow'")
  if [[ "${exists}" == "1" ]]; then
    log "mlflow database already exists, leaving it alone"
    return 0
  fi
  log "creating mlflow database for the mlflow instance's own backend store"
  oc exec deploy/pgvector -n "${DATA_NS}" -- psql -U raguser -d ragdb -c "CREATE DATABASE mlflow;"
}

ensure_mlflow_secrets() {
  if oc get secret mlflow-backend-conn -n "${SHARED_NS}" >/dev/null 2>&1; then
    log "secret mlflow-backend-conn in ${SHARED_NS} already exists, leaving it alone"
  else
    local pgpass
    pgpass=$(oc get secret pgvector-creds -n "${DATA_NS}" -o jsonpath='{.data.password}' | base64 -d)
    log "generating secret mlflow-backend-conn in ${SHARED_NS}"
    # sslmode=disable: the pgvector deployment (plain pgvector/pgvector
    # image) has no TLS configured at all, mlflow's postgres client
    # defaults to requiring SSL and fails with "server does not support
    # SSL, but SSL was required" without this, confirmed live 2026-09-25.
    oc create secret generic mlflow-backend-conn -n "${SHARED_NS}" \
      --from-literal=uri="postgresql://raguser:${pgpass}@pgvector.${DATA_NS}.svc.cluster.local:5432/mlflow?sslmode=disable"
  fi

  local user pass
  user=$(oc get secret minio-dsp-creds -n "${PIPELINES_NS}" -o jsonpath='{.data.accesskey}' | base64 -d)
  pass=$(oc get secret minio-dsp-creds -n "${PIPELINES_NS}" -o jsonpath='{.data.secretkey}' | base64 -d)
  log "syncing minio credentials into ${SHARED_NS} as mlflow-s3-creds, for mlflow's artifact store"
  ensure_secret mlflow-s3-creds "${SHARED_NS}" \
    --from-literal=accesskey="${user}" \
    --from-literal=secretkey="${pass}"
}

wait_mlflow_ready() {
  local tries=30 status=""
  while (( tries > 0 )); do
    status=$(oc get mlflow mlflow -n "${SHARED_NS}" \
      -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null || true)
    if [[ "${status}" == "True" ]]; then
      log "mlflow is Ready"
      return 0
    fi
    sleep 10
    tries=$((tries - 1))
  done
  echo "warning: mlflow did not report Ready in time." >&2
  echo "warning: inspect with: oc get mlflow mlflow -n ${SHARED_NS} -o yaml" >&2
}

ensure_workspace_label() {
  oc label namespace "${PIPELINES_NS}" spear.io/mlflow-workspace=true --overwrite
}

ensure_experiment() {
  local url token existing
  url=$(oc get mlflow mlflow -n "${SHARED_NS}" -o jsonpath='{.status.url}' 2>/dev/null || true)
  if [[ -z "${url}" ]]; then
    echo "warning: no mlflow url yet, skipping experiment bootstrap, rerun this script once mlflow is Ready" >&2
    return 0
  fi
  token="$(oc whoami -t)"
  existing=$(curl -ksS -H "Authorization: Bearer ${token}" -H "X-MLflow-Workspace: ${PIPELINES_NS}" \
    -X POST -H "Content-Type: application/json" -d '{"max_results": 100}' \
    "${url}/api/2.0/mlflow/experiments/search" | python3 -c \
    'import json,sys; d=json.load(sys.stdin); print(len(d.get("experiments",[])))' 2>/dev/null || echo 0)
  if [[ "${existing}" != "0" ]]; then
    log "phase1-ingestion experiment already exists in the ${PIPELINES_NS} workspace, leaving it alone"
    return 0
  fi
  log "creating the phase1-ingestion experiment in the ${PIPELINES_NS} workspace"
  curl -ksS -H "Authorization: Bearer ${token}" -H "X-MLflow-Workspace: ${PIPELINES_NS}" \
    -X POST -H "Content-Type: application/json" -d '{"name": "phase1-ingestion"}' \
    "${url}/api/2.0/mlflow/experiments/create" >/dev/null
}

# registers phase1-apply-pattern-pipeline on the dspa, compile + upload
# only, --register-only, no run triggered, a real run still needs a real
# batch_id and a real winning pattern from a completed autorag run, see
# pipelines/phase1_apply_pattern/compile_and_run.py's own usage comment,
# that part stays manual. added so scripts/90-verify.sh's own check for
# this pipeline being registered passes on a plain, scripted install,
# same registered-but-not-run state documents-rag-optimization-pipeline
# is already in right after rhoai auto registers it
register_apply_pattern_pipeline() {
  local venv="${ROOT_DIR}/.venv-kfp"
  if [[ ! -x "${venv}/bin/python3" ]]; then
    echo "warning: ${venv} not found, skipping phase1-apply-pattern-pipeline registration, create that venv (kfp sdk) yourself and rerun this script" >&2
    return 0
  fi
  log "registering phase1-apply-pattern-pipeline on ${DSPA_NAME}"
  "${venv}/bin/python3" "${ROOT_DIR}/pipelines/phase1_apply_pattern/compile_and_run.py" --register-only
}

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }

  if ! oc get secret minio-dsp-creds -n "${PIPELINES_NS}" >/dev/null 2>&1; then
    echo "minio-dsp-creds not found in ${PIPELINES_NS}, run ./scripts/02-install-storage.sh first" >&2
    exit 1
  fi

  # phase1-apply-pattern-pipeline's own tasks (pipeline.py's
  # _use_pgvector_creds, running as pods in spear-pipelines) read
  # pgvector-creds via a secretKeyRef, which only ever resolves within
  # the pod's own namespace. pgvector-creds is only ever created in
  # spear-data by 03-install-data.sh, same cross namespace gap already
  # fixed for ogxserver's pod in spear-inference, see
  # NAMESPACE_MIGRATION_PLAN.md. mirrored here too, confirmed live via
  # "Error: secret pgvector-creds not found" on a real apply-pattern run.
  log "mirroring pgvector-creds into ${PIPELINES_NS}, phase1-apply-pattern-pipeline's own tasks need their own local copy"
  mirror_secret pgvector-creds "${DATA_NS}" "${PIPELINES_NS}"

  log "data science pipelines application (autoML/autoRAG pipelines enabled)"
  oc apply -f "${ROOT_DIR}/manifests/spear-pipelines/01-pipeline-server.yaml"
  wait_dspa_ready

  ensure_mlflow_db
  ensure_mlflow_secrets
  ensure_workspace_label

  log "applying the shared mlflow instance (workspaceLabelSelector included, safe if it already exists)"
  oc apply -f "${ROOT_DIR}/manifests/spear-pipelines/03-mlflow.yaml"
  wait_mlflow_ready

  # mlflow does its own SubjectAccessReview per request against a
  # fictitious mlflow.kubeflow.org resource group, confirmed live via its
  # own server logs, see the manifest's header comment for the full
  # story. without this the pipeline's log-ingestion-report step 403s.
  log "granting pipeline-runner-${DSPA_NAME} the rbac mlflow itself checks for on create/update experiment calls"
  oc apply -f "${ROOT_DIR}/manifests/spear-pipelines/02-mlflow-rbac.yaml"

  # mlflow's SubjectAccessReview checks rbac in the namespace matching the
  # caller's own X-MLflow-Workspace header value, spear-pipelines, not
  # wherever the calling service account actually lives, confirmed live:
  # the console backend's own sa (spear-console) kept getting back an
  # empty experiment list without this, even with its own namespace's
  # rbac granted. applied here, not in scripts/08-install-console.sh,
  # since the subject sa does not need to exist yet for a rolebinding to
  # reference it
  log "granting spear-console's own backend sa the same mlflow rbac, cross namespace"
  oc apply -f "${ROOT_DIR}/manifests/spear-pipelines/04-console-backend-mlflow-rbac.yaml"

  ensure_experiment
  register_apply_pattern_pipeline

  local url=""
  url=$(oc get mlflow mlflow -n "${SHARED_NS}" -o jsonpath='{.status.url}' 2>/dev/null || true)
  cat <<EOF

mlflow url: ${url:-pending}
next step:
  ./scripts/05-install-inference.sh
EOF
}

main "$@"

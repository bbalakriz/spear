#!/usr/bin/env bash
set -euo pipefail
# dspa pipeline server and mlflow workspace setup in spear-pipelines.
# mlflow cr secrets live in redhat-ods-applications (operator singleton).
# needs 02 and 03 first.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

SHARED_NS="redhat-ods-applications"
DSPA_NAME="spear-dspa"

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
    # pgvector has no TLS; mlflow needs sslmode=disable
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
    log "spear-corpus-prepare experiment already exists in the ${PIPELINES_NS} workspace, leaving it alone"
    return 0
  fi
  log "creating the spear-corpus-prepare experiment in the ${PIPELINES_NS} workspace"
  curl -ksS -H "Authorization: Bearer ${token}" -H "X-MLflow-Workspace: ${PIPELINES_NS}" \
    -X POST -H "Content-Type: application/json" -d '{"name": "spear-corpus-prepare"}' \
    "${url}/api/2.0/mlflow/experiments/create" >/dev/null
}

# register only; apply pattern run stays manual (batch_id + autorag winner)
register_apply_pattern_pipeline() {
  local venv="${ROOT_DIR}/.venv-kfp"
  if [[ ! -x "${venv}/bin/python3" ]]; then
    echo "warning: ${venv} not found, skipping spear-corpus-index-pipeline registration, create that venv (kfp sdk) yourself and rerun this script" >&2
    return 0
  fi
  log "registering spear-corpus-index-pipeline on ${DSPA_NAME}"
  "${venv}/bin/python3" "${ROOT_DIR}/pipelines/corpus_index/compile_and_run.py" --register-only
}

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }

  if ! oc get secret minio-dsp-creds -n "${PIPELINES_NS}" >/dev/null 2>&1; then
    echo "minio-dsp-creds not found in ${PIPELINES_NS}, run ./scripts/02-install-storage.sh first" >&2
    exit 1
  fi

  # pipeline pods need pgvector-creds in spear-pipelines
  log "mirroring pgvector-creds into ${PIPELINES_NS}, spear-corpus-index-pipeline's own tasks need their own local copy"
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

  # mlflow checks mlflow.kubeflow.org rbac on experiment calls
  log "granting pipeline-runner-${DSPA_NAME} the rbac mlflow itself checks for on create/update experiment calls"
  oc apply -f "${ROOT_DIR}/manifests/spear-pipelines/02-mlflow-rbac.yaml"

  # mlflow rbac is checked in workspace namespace (spear-pipelines), not sa namespace
  log "granting spear-console's own backend sa the same mlflow rbac, cross namespace"
  oc apply -f "${ROOT_DIR}/manifests/spear-pipelines/04-console-backend-mlflow-rbac.yaml"

  # console users need edit on dspa route (kube-rbac-proxy checks token)
  log "granting real console users edit in ${PIPELINES_NS}, the rbac the dspa route's own kube-rbac-proxy checks"
  oc apply -f "${ROOT_DIR}/manifests/spear-pipelines/05-human-user-access.yaml"

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

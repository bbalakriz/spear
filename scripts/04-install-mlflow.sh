#!/usr/bin/env bash
set -euo pipefail
# mlflow instance the ingestion pipeline's log-ingestion-report step logs
# to. needs pgvector (00-install-platform.sh) for its backend store
# database and minio (01-install-minio.sh) for its artifact bucket, run
# those first.
#
# confirmed live 2026-09-25: the mlflow operator only ever manages one
# cluster wide instance named exactly "mlflow", with its workloads always
# landing in redhat-ods-applications regardless of what namespace the cr
# is created in, see the header comment in
# manifests/00-platform/06-mlflow.yaml for the full explanation. this
# script therefore creates its secrets in redhat-ods-applications, not
# rag-phase1. also confirmed live: this build layers a custom multi
# tenant "workspace" concept on top of upstream mlflow, every api call
# 400s with "Workspace context is required" until the calling namespace
# is labeled to match spec.workspaceLabelSelector and the request carries
# an X-MLflow-Workspace header naming it, both handled below.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

RAG_NS="rag-phase1"
SHARED_NS="redhat-ods-applications"

ensure_mlflow_db() {
  local exists
  exists=$(oc exec deploy/pgvector -n "${RAG_NS}" -- \
    psql -U raguser -d ragdb -tAc "SELECT 1 FROM pg_database WHERE datname='mlflow'")
  if [[ "${exists}" == "1" ]]; then
    log "mlflow database already exists, leaving it alone"
    return 0
  fi
  log "creating mlflow database for the mlflow instance's own backend store"
  oc exec deploy/pgvector -n "${RAG_NS}" -- psql -U raguser -d ragdb -c "CREATE DATABASE mlflow;"
}

ensure_mlflow_secrets() {
  if oc get secret mlflow-backend-conn -n "${SHARED_NS}" >/dev/null 2>&1; then
    log "secret mlflow-backend-conn in ${SHARED_NS} already exists, leaving it alone"
  else
    local pgpass
    pgpass=$(oc get secret pgvector-creds -n "${RAG_NS}" -o jsonpath='{.data.password}' | base64 -d)
    log "generating secret mlflow-backend-conn in ${SHARED_NS}"
    # sslmode=disable: the pgvector deployment (plain pgvector/pgvector
    # image) has no TLS configured at all, mlflow's postgres client
    # defaults to requiring SSL and fails with "server does not support
    # SSL, but SSL was required" without this, confirmed live 2026-09-25.
    oc create secret generic mlflow-backend-conn -n "${SHARED_NS}" \
      --from-literal=uri="postgresql://raguser:${pgpass}@pgvector.${RAG_NS}.svc.cluster.local:5432/mlflow?sslmode=disable"
  fi

  local user pass
  user=$(oc get secret minio-dsp-creds -n "${RAG_NS}" -o jsonpath='{.data.accesskey}' | base64 -d)
  pass=$(oc get secret minio-dsp-creds -n "${RAG_NS}" -o jsonpath='{.data.secretkey}' | base64 -d)
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

# this build layers a custom multi tenant "workspace" concept on top of
# upstream mlflow (KubernetesWorkspaceProvider), confirmed live
# 2026-09-25: every api call 400s with "Workspace context is required"
# until the calling namespace matches spec.workspaceLabelSelector and the
# request carries an X-MLflow-Workspace header naming it. this label is
# what makes rag-phase1 a selectable workspace at all.
ensure_workspace_label() {
  oc label namespace "${RAG_NS}" rag-phase1.io/mlflow-workspace=true --overwrite
}

ensure_experiment() {
  local url token existing
  url=$(oc get mlflow mlflow -n "${SHARED_NS}" -o jsonpath='{.status.url}' 2>/dev/null || true)
  if [[ -z "${url}" ]]; then
    echo "warning: no mlflow url yet, skipping experiment bootstrap, rerun this script once mlflow is Ready" >&2
    return 0
  fi
  token="$(oc whoami -t)"
  existing=$(curl -ksS -H "Authorization: Bearer ${token}" -H "X-MLflow-Workspace: ${RAG_NS}" \
    -X POST -H "Content-Type: application/json" -d '{"max_results": 100}' \
    "${url}/api/2.0/mlflow/experiments/search" | python3 -c \
    'import json,sys; d=json.load(sys.stdin); print(len(d.get("experiments",[])))' 2>/dev/null || echo 0)
  if [[ "${existing}" != "0" ]]; then
    log "phase1-ingestion experiment already exists in the ${RAG_NS} workspace, leaving it alone"
    return 0
  fi
  log "creating the phase1-ingestion experiment in the ${RAG_NS} workspace"
  curl -ksS -H "Authorization: Bearer ${token}" -H "X-MLflow-Workspace: ${RAG_NS}" \
    -X POST -H "Content-Type: application/json" -d '{"name": "phase1-ingestion"}' \
    "${url}/api/2.0/mlflow/experiments/create" >/dev/null
}

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }

  if ! oc get secret minio-dsp-creds -n "${RAG_NS}" >/dev/null 2>&1; then
    echo "minio-dsp-creds not found in ${RAG_NS}, run ./scripts/01-install-minio.sh first" >&2
    exit 1
  fi

  ensure_mlflow_db
  ensure_mlflow_secrets
  ensure_workspace_label

  log "applying the shared mlflow instance (workspaceLabelSelector included, safe if it already exists)"
  oc apply -f "${ROOT_DIR}/manifests/00-platform/06-mlflow.yaml"
  wait_mlflow_ready

  # mlflow does its own SubjectAccessReview per request against a
  # fictitious mlflow.kubeflow.org resource group, confirmed live via its
  # own server logs, see the manifest's header comment for the full
  # story. without this the pipeline's log-ingestion-report step 403s.
  log "granting pipeline-runner-rag-phase1-dspa the rbac mlflow itself checks for on create/update experiment calls"
  oc apply -f "${ROOT_DIR}/manifests/00-platform/07-mlflow-pipeline-rbac.yaml"

  ensure_experiment

  local url=""
  url=$(oc get mlflow mlflow -n "${SHARED_NS}" -o jsonpath='{.status.url}' 2>/dev/null || true)
  cat <<EOF

mlflow url: ${url:-pending}
next step: manifest driven ingestion pipeline (kfp), see PHASE1_PLAN.md section 3
EOF
}

main "$@"

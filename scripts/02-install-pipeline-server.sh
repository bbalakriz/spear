#!/usr/bin/env bash
set -euo pipefail
# the project pipeline server, autoML/autorag pipelines enabled. needs
# minio-dsp-creds in rag-phase1, created by 01-install-minio.sh, run that
# first. confirmed live 2026-09-25: no DataSciencePipelinesApplication
# exists anywhere on this cluster, this is a plain create.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

RAG_NS="rag-phase1"
DSPA_NAME="rag-phase1-dspa"

wait_dspa_ready() {
  local tries=60 status=""
  while (( tries > 0 )); do
    status=$(oc get dspa "${DSPA_NAME}" -n "${RAG_NS}" \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
    if [[ "${status}" == "True" ]]; then
      log "${DSPA_NAME} is Ready"
      return 0
    fi
    sleep 10
    tries=$((tries - 1))
  done
  echo "warning: ${DSPA_NAME} did not report Ready in time." >&2
  echo "warning: inspect with: oc get dspa ${DSPA_NAME} -n ${RAG_NS} -o yaml" >&2
  echo "warning: and: oc get pods -n ${RAG_NS}" >&2
}

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }

  if ! oc get secret minio-dsp-creds -n "${RAG_NS}" >/dev/null 2>&1; then
    echo "minio-dsp-creds not found in ${RAG_NS}, run ./scripts/01-install-minio.sh first" >&2
    exit 1
  fi

  log "data science pipelines application (autoML/autoRAG pipelines enabled)"
  oc apply -f "${ROOT_DIR}/manifests/00-platform/05-pipeline-server.yaml"
  wait_dspa_ready

  log "pipeline server complete"
  cat <<EOF

next step:
  ./scripts/10-install-guardrails.sh
EOF
}

main "$@"

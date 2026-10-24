#!/usr/bin/env bash
set -euo pipefail
# ogx server, embed server, judge proxy. needs 03-install-data.sh first.
#
# create api key secrets locally (do not paste keys into chat):
#
#   oc create secret generic ogx-glm-key -n spear-inference \
#     --from-literal=apiKey='<the maas gateway key for glm-53-flash>'
#   oc label secret ogx-glm-key -n spear-inference ogx.io/watch=true
#
#   oc create secret generic ogx-litemaas-key -n spear-inference \
#     --from-literal=apiKey='<the litemaas gateway key, scoped to the chat model>'
#   oc label secret ogx-litemaas-key -n spear-inference ogx.io/watch=true
#
#   oc create secret generic ogx-embedding-key -n spear-inference \
#     --from-literal=apiKey='not-required-auth-disabled-on-tei'
#   oc label secret ogx-embedding-key -n spear-inference ogx.io/watch=true
#
# litemaas keys are per model; embedding uses tei with no auth (placeholder key ok)

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

CR_NAME="spear-ogx"

ensure_ogx_metadata_db() {
  local exists
  exists=$(oc exec deploy/pgvector -n "${DATA_NS}" -- \
    psql -U raguser -d ragdb -tAc "SELECT 1 FROM pg_database WHERE datname='ogx_metadata'")
  if [[ "${exists}" == "1" ]]; then
    log "ogx_metadata database already exists, leaving it alone"
    return 0
  fi
  log "creating ogx_metadata database for the ogx server's own metadata store"
  oc exec deploy/pgvector -n "${DATA_NS}" -- psql -U raguser -d ragdb -c "CREATE DATABASE ogx_metadata;"
}

require_secret() {
  local name="$1"
  if ! oc get secret "${name}" -n "${INFERENCE_NS}" >/dev/null 2>&1; then
    cat <<EOF >&2
missing secret ${name} in ${INFERENCE_NS}. create it yourself first, see the
comment at the top of this script for the exact commands, then rerun.
EOF
    exit 1
  fi
  oc label secret "${name}" -n "${INFERENCE_NS}" ogx.io/watch=true --overwrite
}

wait_ogx_ready() {
  local tries=30 phase=""
  while (( tries > 0 )); do
    phase=$(oc get ogxserver "${CR_NAME}" -n "${INFERENCE_NS}" -o jsonpath='{.status.phase}' 2>/dev/null || true)
    if [[ "${phase}" == "Ready" ]]; then
      log "${CR_NAME} is Ready"
      return 0
    fi
    sleep 10
    tries=$((tries - 1))
  done
  echo "warning: ${CR_NAME} did not report Ready in time (last phase: '${phase}')." >&2
  echo "warning: inspect with: oc get ogxserver ${CR_NAME} -n ${INFERENCE_NS} -o yaml" >&2
}

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }

  ensure_ogx_metadata_db
  oc label secret pgvector-creds -n "${DATA_NS}" ogx.io/watch=true --overwrite
  log "mirroring pgvector-creds into ${INFERENCE_NS}, ogxserver's own pod can only resolve a secretKeyRef within its own namespace"
  mirror_secret pgvector-creds "${DATA_NS}" "${INFERENCE_NS}"
  oc label secret pgvector-creds -n "${INFERENCE_NS}" ogx.io/watch=true --overwrite
  require_secret ogx-glm-key
  require_secret ogx-litemaas-key
  require_secret ogx-embedding-key
  # corpus_prepare pods in spear-pipelines need ogx-glm-key locally
  log "mirroring ogx-glm-key into ${PIPELINES_NS}, corpus_prepare's own eval dataset task needs its own local copy"
  mirror_secret ogx-glm-key "${INFERENCE_NS}" "${PIPELINES_NS}"

  log "applying the dashboard flags patch and the self hosted embedding server (02-embed-server.yaml), the ogxserver cr below needs it up first"
  oc apply -f "${ROOT_DIR}/manifests/spear-inference/02-embed-server.yaml"
  oc rollout status deployment/spear-embed-server -n "${INFERENCE_NS}" --timeout=300s

  log "applying the judge proxy (03-judge-proxy.yaml), used by scripts/06-setup-autorag-secrets.sh"
  oc apply -f "${ROOT_DIR}/manifests/spear-inference/03-judge-proxy.yaml"

  log "applying ${CR_NAME}"
  oc apply -f "${ROOT_DIR}/manifests/spear-inference/01-ogxserver.yaml"
  wait_ogx_ready

  log "confirming both foundation models plus the embedding model list under /v1/models"
  local out
  out=$(oc run ogx-models-probe --rm -i --restart=Never --command \
    --image=registry.access.redhat.com/ubi9/ubi-minimal:latest -n "${INFERENCE_NS}" -- \
    curl -sf "http://${CR_NAME}-service.${INFERENCE_NS}.svc.cluster.local:8321/v1/models" 2>&1)
  echo "${out}"
  if echo "${out}" | grep -q "glm-53-flash" && echo "${out}" | grep -q "vllm-inference/" \
    && echo "${out}" | grep -q "nomic-embed"; then
    log "foundation and embedding models listed under /v1/models"
  else
    echo "warning: could not confirm all three model ids in /v1/models output above, inspect manually" >&2
  fi

  cat <<EOF

next steps:
  ./scripts/06-setup-autorag-secrets.sh
  ./scripts/07-install-guardrails.sh
EOF
}

main "$@"

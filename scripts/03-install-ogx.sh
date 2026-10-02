#!/usr/bin/env bash
set -euo pipefail
# ogx server, foundation models plus embedding model wired in from the
# start (manifests/30-ogx/01-ogxserver.yaml). used to be a two stage
# deploy, a provider free probe cr applied first, then a second cr
# replacing it once the api key secrets existed, consolidated into one
# step on 2026-09-28: that split only existed while the real fix (the
# image's own generic env var driven provider slots) was still being
# found, it was never actually required. see 01-ogxserver.yaml's header
# comment for the full history of the three operator bugs that shaped
# this.
#
# create the three api key secrets yourself first, in your own
# terminal, so the raw keys never have to be shared with an assistant
# or land in a chat transcript:
#
#   oc create secret generic ogx-glm-key -n rag-phase1 \
#     --from-literal=apiKey='<the maas gateway key for glm-53-flash>'
#   oc label secret ogx-glm-key -n rag-phase1 ogx.io/watch=true
#
#   oc create secret generic ogx-litemaas-key -n rag-phase1 \
#     --from-literal=apiKey='<the litemaas gateway key, scoped to the chat model>'
#   oc label secret ogx-litemaas-key -n rag-phase1 ogx.io/watch=true
#
#   oc create secret generic ogx-embedding-key -n rag-phase1 \
#     --from-literal=apiKey='not-required-auth-disabled-on-tei'
#   oc label secret ogx-embedding-key -n rag-phase1 ogx.io/watch=true
#
# litemaas scopes keys per model server side, confirmed live via a 401
# when the chat model's key was reused for the embedding model, the
# embedding key above is only a placeholder since the self hosted tei
# server has no auth of its own, see 04-embed-server.yaml.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

RAG_NS="rag-phase1"
CR_NAME="rag-phase1-ogx"

ensure_ogx_metadata_db() {
  local exists
  exists=$(oc exec deploy/pgvector -n "${RAG_NS}" -- \
    psql -U raguser -d ragdb -tAc "SELECT 1 FROM pg_database WHERE datname='ogx_metadata'")
  if [[ "${exists}" == "1" ]]; then
    log "ogx_metadata database already exists, leaving it alone"
    return 0
  fi
  log "creating ogx_metadata database for the ogx server's own metadata store"
  oc exec deploy/pgvector -n "${RAG_NS}" -- psql -U raguser -d ragdb -c "CREATE DATABASE ogx_metadata;"
}

require_secret() {
  local name="$1"
  if ! oc get secret "${name}" -n "${RAG_NS}" >/dev/null 2>&1; then
    cat <<EOF >&2
missing secret ${name} in ${RAG_NS}. create it yourself first, see the
comment at the top of this script for the exact commands, then rerun.
EOF
    exit 1
  fi
  oc label secret "${name}" -n "${RAG_NS}" ogx.io/watch=true --overwrite
}

wait_ogx_ready() {
  local tries=30 phase=""
  while (( tries > 0 )); do
    phase=$(oc get ogxserver "${CR_NAME}" -n "${RAG_NS}" -o jsonpath='{.status.phase}' 2>/dev/null || true)
    if [[ "${phase}" == "Ready" ]]; then
      log "${CR_NAME} is Ready"
      return 0
    fi
    sleep 10
    tries=$((tries - 1))
  done
  echo "warning: ${CR_NAME} did not report Ready in time (last phase: '${phase}')." >&2
  echo "warning: inspect with: oc get ogxserver ${CR_NAME} -n ${RAG_NS} -o yaml" >&2
}

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }

  ensure_ogx_metadata_db
  oc label secret pgvector-creds -n "${RAG_NS}" ogx.io/watch=true --overwrite
  require_secret ogx-glm-key
  require_secret ogx-litemaas-key
  require_secret ogx-embedding-key

  log "applying the self hosted embedding server (04-embed-server.yaml), the ogxserver cr below needs it up first"
  oc apply -f "${ROOT_DIR}/manifests/30-ogx/04-embed-server.yaml"
  oc rollout status deployment/rag-phase1-embed-server -n "${RAG_NS}" --timeout=300s

  log "applying ${CR_NAME}"
  oc apply -f "${ROOT_DIR}/manifests/30-ogx/01-ogxserver.yaml"
  wait_ogx_ready

  log "confirming both foundation models plus the embedding model list under /v1/models"
  local out
  out=$(oc run ogx-models-probe --rm -i --restart=Never --command \
    --image=registry.access.redhat.com/ubi9/ubi-minimal:latest -n "${RAG_NS}" -- \
    curl -sf "http://${CR_NAME}-service.${RAG_NS}.svc.cluster.local:8321/v1/models" 2>&1)
  echo "${out}"
  if echo "${out}" | grep -q "glm-53-flash" && echo "${out}" | grep -q "vllm-inference/" \
    && echo "${out}" | grep -q "nomic-embed"; then
    log "both foundation models and the embedding model confirmed live"
  else
    echo "warning: could not confirm all three model ids in /v1/models output above, inspect manually" >&2
  fi
}

main "$@"

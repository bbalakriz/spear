#!/usr/bin/env bash
set -euo pipefail
# pgvector, the one real shared backing store across ogx metadata, the
# rag corpus, and abac filtered direct queries. run after
# 01-install-namespaces.sh, which already creates spear-data itself.
#
# rag-query-relay (manifests/spear-data/02-rag-query-relay.yaml) is NOT
# applied here despite living in this same namespace: its own image is
# spear-shield-agents' own spear-retrieval-agent build, which does not
# exist until scripts/09-install-worktracker-and-agents.sh builds it.
# applied there instead, once that image is real, not here where it
# would just crash loop on an image pull that cannot succeed yet

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

ensure_pgvector_extension() {
  local tries=30
  while (( tries > 0 )); do
    if oc exec deploy/pgvector -n "${DATA_NS}" -- \
        psql -U raguser -d ragdb -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null 2>&1; then
      log "pgvector extension enabled on ragdb"
      return 0
    fi
    sleep 5
    tries=$((tries - 1))
  done
  echo "could not enable the pgvector extension on ragdb, is the pod actually ready?" >&2
  return 1
}

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }
  command -v openssl >/dev/null 2>&1 || { echo "openssl is required" >&2; exit 1; }

  log "pgvector postgres"
  ensure_secret pgvector-creds "${DATA_NS}" \
    --from-literal=user=raguser \
    --from-literal=password="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
  oc apply -f "${ROOT_DIR}/manifests/spear-data/01-pgvector.yaml"
  wait_deploy_ready pgvector "${DATA_NS}"
  ensure_pgvector_extension

  log "data complete"
  cat <<EOF

next steps:
  ./scripts/04-install-pipelines.sh
  ./scripts/05-install-inference.sh
EOF
}

main "$@"

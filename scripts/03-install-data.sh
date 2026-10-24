#!/usr/bin/env bash
set -euo pipefail
# pgvector for shared postgres. rag-query-relay is applied in 09 (image not built yet).

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

# rag_reader without bypassrls so row level security on rag_chunks applies
ensure_rag_reader_role() {
  local password
  password="$(oc get secret rag-reader-creds -n "${DATA_NS}" -o jsonpath='{.data.password}' | base64 -d)"
  oc exec deploy/pgvector -n "${DATA_NS}" -- \
    psql -U raguser -d ragdb -v ON_ERROR_STOP=1 -c "
      DO \$\$
      BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_reader') THEN
          CREATE ROLE rag_reader LOGIN;
        END IF;
      END
      \$\$;
      ALTER ROLE rag_reader PASSWORD '${password}';
    "
  log "rag_reader role present, password synced to rag-reader-creds"
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

  log "rag_reader, the restricted role rag-query-relay connects as for real row level security"
  ensure_secret rag-reader-creds "${DATA_NS}" \
    --from-literal=user=rag_reader \
    --from-literal=password="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
  ensure_rag_reader_role

  log "data complete"
  cat <<EOF

next steps:
  ./scripts/04-install-pipelines.sh
  ./scripts/05-install-inference.sh
EOF
}

main "$@"

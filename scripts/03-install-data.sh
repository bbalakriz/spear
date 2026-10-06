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

# rag_reader: the one role rag-query-relay ever connects as now, real abac
# defense in depth, not just raguser's own superuser session reused
# everywhere. no superuser, no createdb, no bypassrls, confirmed this is
# what makes the row level security policy on rag_chunks
# (pipelines/phase1_apply_pattern/pipeline.py, created once that table
# itself exists) actually mean something: a role with bypassrls set, which
# raguser has since it is this database's own bootstrap postgres superuser,
# ignores every policy regardless of what the policy says. password always
# re-synced against whatever rag-reader-creds already holds, same create
# once never regenerate secret, but the role itself is cheap to re align
# on every rerun, unlike regenerating the secret, which would lock out
# whatever already has the old password cached
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

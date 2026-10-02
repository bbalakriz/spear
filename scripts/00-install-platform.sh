#!/usr/bin/env bash
set -euo pipefail
# phase 1 platform prerequisites, everything except minio (own script,
# 01-install-minio.sh) and the pipeline server (needs minio's credentials
# first, 02-install-pipeline-server.sh). confirmed live against this
# cluster 2026-09-25: rhods-operator 3.5.0 is installed, the DataScienceCluster
# is Ready with trustyai, ogx, mlflowoperator, aipipelines, mcplifecycleoperator
# all Managed already, so unlike the sibling agent-pack project's
# 00-install-platform.sh, there is no operator subscription to approve here,
# nothing in this script touches olm.
#
# what this script actually does: create the rag-phase1 namespace, turn on
# the two odh-dashboard-config feature flags autorag needs (additive merge
# patch, only two boolean keys, not the higher blast radius dsc/dsci
# replacement the sibling script is cautious about, see
# ensure_dashboard_flags below for why that distinction matters here), and
# stand up the pgvector-backed postgres instance since no postgres operator
# exists on this cluster to do it for us.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

RAG_NS="rag-phase1"

# odh-dashboard-config is a cluster wide singleton other teams' dashboard
# customizations may already sit on. unlike the dsc/dsci case in the
# sibling project (a full platform install those scripts refuse to touch
# once it exists), this patch only ever adds two boolean keys under
# spec.dashboardConfig and never removes or overwrites anything else there,
# confirmed against the live cr's current content before writing this
# script (it had neither key set). still checked and skipped if already
# true, so a rerun is a no-op rather than a repeated patch call.
ensure_dashboard_flags() {
  local genai autorag
  genai=$(oc get odhdashboardconfig odh-dashboard-config -n redhat-ods-applications \
    -o jsonpath='{.spec.dashboardConfig.genAiStudio}' 2>/dev/null || true)
  autorag=$(oc get odhdashboardconfig odh-dashboard-config -n redhat-ods-applications \
    -o jsonpath='{.spec.dashboardConfig.autorag}' 2>/dev/null || true)
  if [[ "${genai}" == "true" && "${autorag}" == "true" ]]; then
    log "odh-dashboard-config already has genAiStudio and autorag enabled, leaving it alone"
    return 0
  fi
  log "patching odh-dashboard-config: genAiStudio=${genai:-unset} -> true, autorag=${autorag:-unset} -> true"
  oc patch odhdashboardconfig odh-dashboard-config -n redhat-ods-applications \
    --type merge --patch-file "${ROOT_DIR}/manifests/00-platform/02-dashboard-flags-patch.yaml"
}

ensure_pgvector_extension() {
  local tries=30
  while (( tries > 0 )); do
    if oc exec deploy/pgvector -n "${RAG_NS}" -- \
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

  log "rag-phase1 namespace"
  oc apply -f "${ROOT_DIR}/manifests/00-platform/01-namespace.yaml"

  log "odh dashboard config flags (gen ai studio, autorag)"
  ensure_dashboard_flags

  log "pgvector postgres"
  ensure_secret pgvector-creds "${RAG_NS}" \
    --from-literal=user=raguser \
    --from-literal=password="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
  oc apply -f "${ROOT_DIR}/manifests/00-platform/04-pgvector-postgres.yaml"
  wait_deploy_ready pgvector "${RAG_NS}"
  ensure_pgvector_extension

  log "platform prerequisites complete"
  cat <<EOF

next steps:
  ./scripts/01-install-minio.sh
  ./scripts/02-install-pipeline-server.sh
  ./scripts/10-install-guardrails.sh
EOF
}

main "$@"

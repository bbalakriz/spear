#!/usr/bin/env bash
set -uo pipefail
# tears down everything phase 1's scripts create. does not touch anything
# that was already on the cluster before we got here: rhoai itself, the
# dsc/dsci, the odh-dashboard-config cr (only the two feature flags it
# added are worth mentioning, and turning them back off is deliberately
# left as a manual step since other projects on this cluster may have
# started depending on gen ai studio/autorag being on in the meantime).

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

RAG_NS="rag-phase1"
MINIO_NS="minio"

confirm() {
  local reply
  read -r -p "this deletes the ${RAG_NS} and ${MINIO_NS} namespaces and everything in them. type 'yes' to continue: " reply
  [[ "${reply}" == "yes" ]]
}

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }

  if [[ "${1:-}" != "--force" ]]; then
    confirm || { echo "aborted"; exit 1; }
  fi

  log "deleting guardrails cr and configmaps"
  oc delete nemoguardrails rag-phase1-guardrails -n "${RAG_NS}" --ignore-not-found=true
  oc delete configmap internal-docs-config vendor-submission-config -n "${RAG_NS}" --ignore-not-found=true

  log "deleting pipeline server"
  oc delete dspa rag-phase1-dspa -n "${RAG_NS}" --ignore-not-found=true

  log "deleting rag-phase1 namespace (pgvector, secrets, everything else in it)"
  oc delete namespace "${RAG_NS}" --ignore-not-found=true

  log "deleting minio namespace (deployment, pvc data, seed configmap, bootstrap job)"
  oc delete namespace "${MINIO_NS}" --ignore-not-found=true

  cat <<EOF

left untouched, deliberately:
  - the rhoai operator, DataScienceCluster, DSCInitialization
  - odh-dashboard-config's genAiStudio/autorag flags (turn off manually if needed:
    oc patch odhdashboardconfig odh-dashboard-config -n redhat-ods-applications --type merge \\
      -p '{"spec":{"dashboardConfig":{"genAiStudio":false,"autorag":false}}}')
EOF
  log "cleanup complete"
}

main "$@"

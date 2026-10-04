#!/usr/bin/env bash
set -uo pipefail
# tears down everything scripts 01 through 09 create. does not touch
# anything that was already on the cluster before we got here: rhoai
# itself, the dsc/dsci, the odh-dashboard-config cr (only the two feature
# flags it added are worth mentioning, and turning them back off is
# deliberately left as a manual step since other projects on this cluster
# may have started depending on gen ai studio/autorag being on in the
# meantime), mlflow itself (a shared, cluster wide instance that predates
# this project, only this project's own rbac grant on it is ours to
# remove, done implicitly by deleting spear-pipelines below).
#
# NAMESPACE_MIGRATION_PLAN.md section 3's own teardown order: delete
# consumers before the things they depend on, so nothing errors out
# waiting on a resource that is already gone. namespace deletion below
# follows that same order, sandboxes and gateways first, pgvector and
# minio last.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

confirm() {
  local reply
  read -r -p "this deletes every namespace this project owns and everything in them. type 'yes' to continue: " reply
  [[ "${reply}" == "yes" ]]
}

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }

  if [[ "${1:-}" != "--force" ]]; then
    confirm || { echo "aborted"; exit 1; }
  fi

  log "deleting the two sandbox openshell releases/sandboxes (manual, see PHASE2_PLAN.md section 7, not scripted here)"

  log "deleting ${AGENTS_NS} and ${WORKTRACKER_NS} (gateways, guard proxy, openproject, spear-openproject-mcp, both sandboxes' k8s side)"
  oc delete namespace "${AGENTS_NS}" --ignore-not-found=true
  oc delete namespace "${WORKTRACKER_NS}" --ignore-not-found=true
  oc delete keycloakrealmimport spear-shield-agents-realm -n keycloak --ignore-not-found=true

  log "deleting ${CONSOLE_NS} (console frontend and backend)"
  oc delete namespace "${CONSOLE_NS}" --ignore-not-found=true

  log "deleting ${PIPELINES_NS} (dspa pipeline server, mariadb, this project's own mlflow rbac grant)"
  oc delete namespace "${PIPELINES_NS}" --ignore-not-found=true

  log "deleting guardrails cr and configmaps in ${GUARDRAILS_NS}"
  oc delete nemoguardrails rag-phase1-guardrails -n "${GUARDRAILS_NS}" --ignore-not-found=true
  oc delete namespace "${GUARDRAILS_NS}" --ignore-not-found=true

  log "deleting ${INFERENCE_NS} (ogx, embed server, judge proxy)"
  oc delete namespace "${INFERENCE_NS}" --ignore-not-found=true

  log "deleting ${DATA_NS} (pgvector, rag-query-relay)"
  oc delete namespace "${DATA_NS}" --ignore-not-found=true

  log "deleting ${STORAGE_NS} (minio deployment, pvc data, seed configmap, bootstrap job)"
  oc delete namespace "${STORAGE_NS}" --ignore-not-found=true

  cat <<EOF

left untouched, deliberately:
  - the rhoai operator, DataScienceCluster, DSCInitialization
  - the shared mlflow instance itself, in redhat-ods-applications
  - odh-dashboard-config's genAiStudio/autorag flags (turn off manually if needed:
    oc patch odhdashboardconfig odh-dashboard-config -n redhat-ods-applications --type merge \\
      -p '{"spec":{"dashboardConfig":{"genAiStudio":false,"autorag":false}}}')
EOF
  log "cleanup complete"
}

main "$@"

#!/usr/bin/env bash
set -euo pipefail
# NAMESPACE_MIGRATION_PLAN.md: creates every namespace this project owns
# up front, before anything that lives inside any of them, plus the one
# shared, cluster wide piece of state every namespace below ultimately
# depends on, the odh-dashboard-config feature flags autorag needs.
# minio gets its own namespace create here too even though it is not
# listed in manifests/00-namespaces.yaml (that file is this project's
# own eight namespaces, minio is a plain generic deployment with no
# project specific logic, same reasoning as keeping its own name, see
# the plan's own section 8).

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

# odh-dashboard-config is a cluster wide singleton other teams' dashboard
# customizations may already sit on. this patch only ever adds two boolean
# keys under spec.dashboardConfig and never removes or overwrites anything
# else there, confirmed against the live cr's current content before this
# was first written (it had neither key set). still checked and skipped if
# already true, so a rerun is a no-op rather than a repeated patch call.
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
    --type merge --patch-file "${ROOT_DIR}/manifests/spear-inference/00-dashboard-flags-patch.yaml"
}

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }

  log "this project's own eight namespaces"
  oc apply -f "${ROOT_DIR}/manifests/00-namespaces.yaml"

  log "minio namespace, own generic deployment, own script (02-install-storage.sh)"
  oc get namespace "${STORAGE_NS}" >/dev/null 2>&1 || oc create namespace "${STORAGE_NS}"

  log "odh dashboard config flags (gen ai studio, autorag)"
  ensure_dashboard_flags

  log "namespaces complete"
  cat <<EOF

next steps:
  ./scripts/02-install-storage.sh
  ./scripts/03-install-data.sh
EOF
}

main "$@"

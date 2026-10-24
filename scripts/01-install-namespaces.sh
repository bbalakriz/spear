#!/usr/bin/env bash
set -euo pipefail
# project namespaces plus minio and odh dashboard autorag flags

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

# patch only genAiStudio and autorag flags; skip if already true
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

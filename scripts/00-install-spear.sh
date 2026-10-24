#!/usr/bin/env bash
set -euo pipefail
# runs install scripts 01 through 09, then 90-verify; stops on first failure.
# autorag apply pattern still needs a manual batch_id and winning pattern.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

SCRIPTS=(
  01-install-namespaces.sh
  02-install-storage.sh
  03-install-data.sh
  04-install-pipelines.sh
  05-install-inference.sh
  06-setup-autorag-secrets.sh
  07-install-guardrails.sh
  08-install-console.sh
  09-install-worktracker-and-agents.sh
)

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }

  log "full install chain: ${#SCRIPTS[@]} scripts, stopping at the first failure"
  local started
  started="$(date +%s)"
  for s in "${SCRIPTS[@]}"; do
    log "=== ${s} ==="
    bash "${ROOT_DIR}/scripts/${s}"
  done

  log "install chain complete in $(( $(date +%s) - started ))s, running 90-verify"
  # verify keeps going on purpose and reports its own summary, so its exit
  # code here only reflects the summary, not an early stop
  bash "${ROOT_DIR}/scripts/90-verify.sh" || true
}

main "$@"

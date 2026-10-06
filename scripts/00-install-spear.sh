#!/usr/bin/env bash
set -euo pipefail
# thin wrapper that runs the whole install chain in order, 01 through 09, then
# 90-verify. every script in the chain is idempotent by design (safe to rerun),
# so this is too: a rerun against an already installed cluster re-applies and
# moves on. stops at the first failing script, unlike 90-verify's own deliberate
# keep-going behavior, a broken prerequisite should not let the chain continue.
# the manual steps this does NOT cover: a real ingestion run still needs a real
# batch_id and a winning pattern from a completed autorag run, see
# NAMESPACE_MIGRATION_PLAN.md section 10's own note.

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

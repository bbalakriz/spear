#!/usr/bin/env bash
# shared by every numbered script below, not meant to run on its own.
# small, phase 1 only needs a fraction of what the sibling agent-pack
# project's lib-olm.sh does since every operator subscription phase 1
# depends on (rhods-operator, trustyai via the dsc, mcplifecycleoperator,
# mlflowoperator) is already installed and Managed on this cluster,
# confirmed live 2026-09-25, there is no new operator subscription to
# approve here.

log() { printf '== %s ==\n' "$*"; }

# generates a random secret once, never regenerates against an already
# provisioned one. same pattern as agent-pack's ensure_keycloak_db_secret,
# reused here for minio and pgvector so a rerun of these scripts never
# locks anyone out of an already running instance.
ensure_secret() {
  local name="$1" ns="$2"; shift 2
  if oc get secret "${name}" -n "${ns}" >/dev/null 2>&1; then
    log "secret ${name} in ${ns} already exists, leaving it alone"
    return 0
  fi
  log "generating secret ${name} in ${ns}"
  oc create secret generic "${name}" -n "${ns}" "$@"
}

wait_deploy_ready() {
  local name="$1" ns="$2" timeout="${3:-300s}"
  oc wait --for=condition=Available "deployment/${name}" -n "${ns}" --timeout="${timeout}"
}

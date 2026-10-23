#!/usr/bin/env bash
# shared by every numbered script below, not meant to run on its own.
# small, phase 1 only needs a fraction of what the sibling agent-pack
# project's lib-olm.sh does since every operator subscription phase 1
# depends on (rhods-operator, trustyai via the dsc, mcplifecycleoperator,
# mlflowoperator) is already installed and Managed on this cluster,
# confirmed live 2026-09-25, there is no new operator subscription to
# approve here.

# stderr, not stdout: scripts/11-submit-evalhub-jobs.sh pipes a function's
# own stdout straight into python3 -m json.tool as real data, a log line
# mixed into that same stream breaks the parse, confirmed live,
# "Expecting value: line 1 column 1"
log() { printf '== %s ==\n' "$*" >&2; }

# NAMESPACE_MIGRATION_PLAN.md: the one place every namespace name this
# project owns is defined, replacing the 11 separate copies of
# RAG_NS="rag-phase1" every script used to carry on its own. rag-phase1
# itself is retired, nothing left needs it to exist. minio keeps its own
# plain name rather than becoming spear-storage, see the plan's own
# section 8 for why.
DATA_NS="spear-data"
STORAGE_NS="minio"
INFERENCE_NS="spear-inference"
GUARDRAILS_NS="spear-guardrails"
PIPELINES_NS="spear-pipelines"
CONSOLE_NS="spear-console"
WORKTRACKER_NS="spear-worktracker"
AGENTS_NS="spear-shield-agents"
# evalhub's own namespace, a dedicated tenant rather than redhat-ods-applications,
# per that namespace's own restrictive NetworkPolicy, confirmed live against
# this cluster's real rhods-operator 3.5 docs, see scripts/10-install-evalhub.sh
EVAL_NS="spear-shield-eval"

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

# one shot curl/bash probe under the restricted scc. openshift's
# restricted-v2 (and any namespace with pod-security restricted warn)
# rejects pods that omit container seccompProfile, and the moment
# --overrides sets containers[] oc run's own --command is dropped, so
# both the securityContext and the bash -c script have to live inside
# the override. stdout of the probe is printed; exit status matches the
# container's. sa may be empty to keep the namespace default.
run_restricted_probe() {
  local name="$1" ns="$2" sa="$3" script="$4"
  local img="registry.access.redhat.com/ubi9/ubi-minimal:latest"
  local overrides phase tries=60 exit_code
  oc delete pod "${name}" -n "${ns}" --ignore-not-found >/dev/null 2>&1 || true
  overrides=$(jq -nc \
    --arg name "${name}" \
    --arg img "${img}" \
    --arg sa "${sa}" \
    --arg script "${script}" \
    '{
      spec: {
        securityContext: {runAsNonRoot: true, seccompProfile: {type: "RuntimeDefault"}},
        containers: [{
          name: $name,
          image: $img,
          command: ["bash", "-c", $script],
          securityContext: {
            allowPrivilegeEscalation: false,
            capabilities: {drop: ["ALL"]},
            runAsNonRoot: true,
            seccompProfile: {type: "RuntimeDefault"}
          }
        }]
      }
    } | if $sa == "" then . else .spec.serviceAccountName = $sa end')
  oc run "${name}" --restart=Never --image="${img}" -n "${ns}" --overrides="${overrides}" >/dev/null
  while (( tries > 0 )); do
    phase=$(oc get pod "${name}" -n "${ns}" -o jsonpath='{.status.phase}' 2>/dev/null || true)
    if [[ "${phase}" == "Succeeded" || "${phase}" == "Failed" ]]; then
      break
    fi
    sleep 2
    tries=$((tries - 1))
  done
  oc logs "${name}" -n "${ns}" 2>/dev/null || true
  exit_code=$(oc get pod "${name}" -n "${ns}" -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}' 2>/dev/null || echo 1)
  oc delete pod "${name}" -n "${ns}" --ignore-not-found >/dev/null 2>&1 || true
  [[ "${exit_code}" == "0" ]]
}

# a secretKeyRef only resolves within the pod's own namespace, so a
# consumer in one namespace reading a secret owned by another (ogxserver
# in spear-inference reading pgvector-creds, owned by spear-data) needs
# its own local copy. found live during the 2026-10-04 rebuild, see
# NAMESPACE_MIGRATION_PLAN.md, this never came up pre migration since
# everything lived in 00-platform together. always re-synced on rerun,
# unlike ensure_secret, since this is a derived copy of the source, not
# a secret owned/generated here.
mirror_secret() {
  local name="$1" src_ns="$2" dest_ns="$3"
  oc get secret "${name}" -n "${src_ns}" -o json \
    | jq 'del(.metadata.namespace, .metadata.resourceVersion, .metadata.uid, .metadata.creationTimestamp, .metadata.ownerReferences, .metadata.annotations, .metadata.managedFields, .metadata.selfLink)' \
    | oc apply -n "${dest_ns}" -f -
}

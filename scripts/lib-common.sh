#!/usr/bin/env bash
# shared helpers for numbered install scripts (do not run alone).

# stderr only: 11-submit-evalhub-jobs.sh pipes stdout to json.tool
log() { printf '== %s ==\n' "$*" >&2; }

# canonical namespace names for this project
DATA_NS="spear-data"
STORAGE_NS="minio"
INFERENCE_NS="spear-inference"
GUARDRAILS_NS="spear-guardrails"
PIPELINES_NS="spear-pipelines"
CONSOLE_NS="spear-console"
WORKTRACKER_NS="spear-worktracker"
AGENTS_NS="spear-agents"
# evalhub tenant namespace (see 10-install-evalhub.sh)
EVAL_NS="spear-eval"

# create secret once; reruns must not rotate existing credentials
ensure_secret() {
  local name="$1" ns="$2"; shift 2
  if oc get secret "${name}" -n "${ns}" >/dev/null 2>&1; then
    log "secret ${name} in ${ns} already exists, leaving it alone"
    return 0
  fi
  log "generating secret ${name} in ${ns}"
  oc create secret generic "${name}" -n "${ns}" "$@"
}

# poll until deployment exists, then oc wait for Available
wait_deploy_ready() {
  local name="$1" ns="$2" timeout="${3:-300s}" tries=30
  while ! oc get "deployment/${name}" -n "${ns}" >/dev/null 2>&1; do
    if (( tries <= 0 )); then
      echo "deployment/${name} in ${ns} never showed up" >&2
      return 1
    fi
    sleep 2
    tries=$((tries - 1))
  done
  oc wait --for=condition=Available "deployment/${name}" -n "${ns}" --timeout="${timeout}"
}

# bump annotation so operators re reconcile stuck crs
nudge_reconcile() {
  local kind="$1" name="$2" ns="$3"
  oc annotate "${kind}" "${name}" -n "${ns}" "spear.io/reconcile-nonce=$(date +%s)" --overwrite >/dev/null
}

# one shot probe pod under restricted scc; script lives in --overrides
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

# copy secret into consumer namespace (secretKeyRef is namespace local)
mirror_secret() {
  local name="$1" src_ns="$2" dest_ns="$3"
  oc get secret "${name}" -n "${src_ns}" -o json \
    | jq 'del(.metadata.namespace, .metadata.resourceVersion, .metadata.uid, .metadata.creationTimestamp, .metadata.ownerReferences, .metadata.annotations, .metadata.managedFields, .metadata.selfLink)' \
    | oc apply -n "${dest_ns}" -f -
}

#!/usr/bin/env bash
set -euo pipefail
# build console backend + openshift plugin; inject cluster routes via oc set env.
# needs 02, 04, 05, 07 first.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

SHARED_NS="redhat-ods-applications"
BACKEND_BC="spear-console-backend"
FRONTEND_BC="spear-console"
BACKEND_DEPLOY="spear-console-backend"
HELM_RELEASE="spear-console"

build_image() {
  local bc="$1" context_dir="$2"
  if ! oc get bc "${bc}" -n "${CONSOLE_NS}" >/dev/null 2>&1; then
    log "creating buildconfig ${bc}"
    oc new-build --binary --strategy=docker --name="${bc}" -n "${CONSOLE_NS}"
  fi
  log "building and pushing ${bc} from ${context_dir}"
  oc start-build "${bc}" -n "${CONSOLE_NS}" --from-dir="${context_dir}" --follow
}

# pin deployment to image digest, not floating :latest
image_digest_ref() {
  local bc="$1" repo digest
  repo=$(oc get is "${bc}" -n "${CONSOLE_NS}" -o jsonpath='{.status.dockerImageRepository}')
  digest=$(oc get is "${bc}" -n "${CONSOLE_NS}" -o jsonpath='{.status.tags[0].items[0].image}')
  echo "${repo}@${digest}"
}

# demo chat users live in sso realm (KeycloakRealmImport cannot add them)
ensure_sso_realm_demo_users() {
  local kc_host admin_user admin_pass admin_token
  kc_host="$(oc get route keycloak -n keycloak -o jsonpath='{.spec.host}')"
  admin_user="$(oc get secret keycloak-initial-admin -n keycloak -o jsonpath='{.data.username}' | base64 -d)"
  admin_pass="$(oc get secret keycloak-initial-admin -n keycloak -o jsonpath='{.data.password}' | base64 -d)"
  admin_token="$(curl -sk -X POST "https://${kc_host}/realms/master/protocol/openid-connect/token" \
    -d 'grant_type=password' -d 'client_id=admin-cli' \
    --data-urlencode "username=${admin_user}" --data-urlencode "password=${admin_pass}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null)"
  if [[ -z "${admin_token}" ]]; then
    echo "warning: could not get a keycloak admin token, skipping sso realm demo user bootstrap" >&2
    return 0
  fi

  local username first last email existing password
  for spec in "balakrishnan.b:Balakrishnan:B" "siddhartha.de:Siddhartha:De"; do
    username="${spec%%:*}"
    first="$(echo "${spec}" | cut -d: -f2)"
    last="$(echo "${spec}" | cut -d: -f3)"
    email="${username}@spear-shield.demo"
    # go-template index: secret keys contain dots (jsonpath breaks)
    password="$(oc get secret spear-shield-console-creds -n "${CONSOLE_NS}" \
      -o go-template="{{index .data \"${username}\"}}" | base64 -d)"
    if [[ -z "${password}" ]]; then
      echo "warning: no ${username} key in spear-shield-console-creds, skipping that user" >&2
      continue
    fi
    existing="$(curl -sk -H "Authorization: Bearer ${admin_token}" \
      "https://${kc_host}/admin/realms/sso/users?username=${username}&exact=true" \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["id"] if d else "")' 2>/dev/null)"
    if [[ -n "${existing}" ]]; then
      log "sso realm already has ${username}, leaving the account alone, only syncing its password"
      curl -sk -o /dev/null -X PUT "https://${kc_host}/admin/realms/sso/users/${existing}/reset-password" \
        -H "Authorization: Bearer ${admin_token}" -H "Content-Type: application/json" \
        -d "{\"type\":\"password\",\"value\":\"${password}\",\"temporary\":false}"
      continue
    fi
    log "creating ${username} in the sso realm"
    curl -sk -o /dev/null -X POST "https://${kc_host}/admin/realms/sso/users" \
      -H "Authorization: Bearer ${admin_token}" -H "Content-Type: application/json" \
      -d "{\"username\":\"${username}\",\"firstName\":\"${first}\",\"lastName\":\"${last}\",\"email\":\"${email}\",\"emailVerified\":true,\"enabled\":true,\"requiredActions\":[],\"credentials\":[{\"type\":\"password\",\"value\":\"${password}\",\"temporary\":false}]}"
  done
}

resolve_routes() {
  log "resolving this cluster's own route hostnames, never hardcoded"
  DSPA_ROUTE="https://$(oc get route ds-pipeline-spear-dspa -n "${PIPELINES_NS}" -o jsonpath='{.spec.host}')"
  GUARDRAILS_ROUTE="https://$(oc get route spear-guardrails -n "${GUARDRAILS_NS}" -o jsonpath='{.spec.host}')"
  MLFLOW_URL="$(oc get mlflow mlflow -n "${SHARED_NS}" -o jsonpath='{.status.url}')"
  MINIO_CONSOLE_URL="https://$(oc get route minio-console -n "${STORAGE_NS}" -o jsonpath='{.spec.host}')"
  RHOAI_DASHBOARD_URL="https://$(oc get route rhods-dashboard -n "${SHARED_NS}" -o jsonpath='{.spec.host}')"
  # spear shield chat (see 07-a2a-gateway.yaml)
  SPEAR_SHIELD_ISSUER_URL="https://$(oc get route keycloak -n keycloak -o jsonpath='{.spec.host}')/realms/${AGENTS_NS}"
  SPEAR_COORDINATOR_A2A_URL="http://spear-coordinator-a2a-gateway.${AGENTS_NS}.svc.cluster.local:80"
  # mcp gateway public host (same as 09 resolve_hosts)
  local cluster_domain
  cluster_domain="$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')"
  SPEAR_MCP_GATEWAY_HOST_HEADER="mcp-${AGENTS_NS}.${cluster_domain}"
  if [[ -z "${DSPA_ROUTE}" || "${DSPA_ROUTE}" == "https://" ]]; then
    echo "could not resolve the dspa route, run scripts/04-install-pipelines.sh first" >&2
    exit 1
  fi
  if [[ -z "${GUARDRAILS_ROUTE}" || "${GUARDRAILS_ROUTE}" == "https://" ]]; then
    echo "could not resolve the guardrails route, run scripts/07-install-guardrails.sh first" >&2
    exit 1
  fi
  if [[ -z "${MLFLOW_URL}" ]]; then
    echo "could not resolve mlflow's url, run scripts/04-install-pipelines.sh first" >&2
    exit 1
  fi
  if [[ -z "${MINIO_CONSOLE_URL}" || "${MINIO_CONSOLE_URL}" == "https://" ]]; then
    echo "could not resolve the minio console route, run scripts/02-install-storage.sh first" >&2
    exit 1
  fi
  if [[ -z "${RHOAI_DASHBOARD_URL}" || "${RHOAI_DASHBOARD_URL}" == "https://" ]]; then
    echo "could not resolve the rhods-dashboard route, is this a rhoai cluster?" >&2
    exit 1
  fi
  log "dspa route:         ${DSPA_ROUTE}"
  log "guardrails route:   ${GUARDRAILS_ROUTE}"
  log "mlflow url:         ${MLFLOW_URL}"
  log "minio console:      ${MINIO_CONSOLE_URL}"
  log "rhoai dashboard:    ${RHOAI_DASHBOARD_URL}"
  log "spear shield issuer:${SPEAR_SHIELD_ISSUER_URL}"
  log "coordinator a2a url:${SPEAR_COORDINATOR_A2A_URL}"
  log "mcp gateway host:   ${SPEAR_MCP_GATEWAY_HOST_HEADER}"
}

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }
  command -v helm >/dev/null 2>&1 || { echo "helm is required" >&2; exit 1; }

  if ! oc get secret minio-dsp-creds -n "${PIPELINES_NS}" >/dev/null 2>&1; then
    echo "minio-dsp-creds not found in ${PIPELINES_NS}, run ./scripts/02-install-storage.sh first" >&2
    exit 1
  fi
  # backend pod needs minio-dsp-creds in spear-console
  log "mirroring minio-dsp-creds into ${CONSOLE_NS}"
  mirror_secret minio-dsp-creds "${PIPELINES_NS}" "${CONSOLE_NS}"

  build_image "${BACKEND_BC}" "${ROOT_DIR}/console-backend"

  log "applying chat creds"
  oc apply -f "${ROOT_DIR}/manifests/spear-console/01-chat-creds.yaml"
  ensure_sso_realm_demo_users
  oc apply -f "${ROOT_DIR}/manifests/spear-agents/08-console-backend-rbac.yaml"
  # closed loop patch rbac in spear-guardrails
  oc apply -f "${ROOT_DIR}/manifests/spear-guardrails/08-console-backend-closed-loop-rbac.yaml"
  log "applying backend service account, node reader rbac and deployment"
  oc apply -f "${ROOT_DIR}/manifests/spear-console/02-backend.yaml"

  local backend_image
  backend_image="$(image_digest_ref "${BACKEND_BC}")"
  log "pinning ${BACKEND_DEPLOY} to the digest just pushed: ${backend_image}"
  oc set image "deployment/${BACKEND_DEPLOY}" -n "${CONSOLE_NS}" "backend=${backend_image}"

  resolve_routes
  log "injecting the resolved routes into ${BACKEND_DEPLOY}, safe to rerun"
  oc set env "deployment/${BACKEND_DEPLOY}" -n "${CONSOLE_NS}" \
    "MLFLOW_URL=${MLFLOW_URL}" \
    "DSPA_ROUTE=${DSPA_ROUTE}" \
    "GUARDRAILS_ROUTE=${GUARDRAILS_ROUTE}" \
    "MINIO_CONSOLE_URL=${MINIO_CONSOLE_URL}" \
    "RHOAI_DASHBOARD_URL=${RHOAI_DASHBOARD_URL}" \
    "SPEAR_SHIELD_ISSUER_URL=${SPEAR_SHIELD_ISSUER_URL}" \
    "SPEAR_COORDINATOR_A2A_URL=${SPEAR_COORDINATOR_A2A_URL}" \
    "SPEAR_MCP_GATEWAY_HOST_HEADER=${SPEAR_MCP_GATEWAY_HOST_HEADER}"

  # pipeline/autorag env knobs (override server.py defaults without rebuild)
  log "injecting pipeline/experiment names and autorag knobs into ${BACKEND_DEPLOY}, safe to rerun"
  oc set env "deployment/${BACKEND_DEPLOY}" -n "${CONSOLE_NS}" \
    "RAG_WORKSPACE=${PIPELINES_NS}" \
    "INGESTION_EXPERIMENT_NAME=spear-corpus-prepare" \
    "INGESTION_PIPELINE_NAME=spear-corpus-prepare-pipeline" \
    "INGESTION_KFP_EXPERIMENT_NAME=spear-corpus-prepare" \
    "AUTORAG_PIPELINE_NAME=documents-rag-optimization-pipeline" \
    "AUTORAG_KFP_EXPERIMENT_NAME=phase1-autorag" \
    "APPLY_PATTERN_PIPELINE_NAME=spear-corpus-index-pipeline" \
    "APPLY_PATTERN_KFP_EXPERIMENT_NAME=spear-corpus-index" \
    "AUTORAG_RAW_BUCKET=rag-documents" \
    "AUTORAG_S3_SECRET_NAME=autorag-s3-creds" \
    "AUTORAG_OGX_SECRET_NAME=autorag-ogx-creds" \
    "AUTORAG_VECTOR_IO_PROVIDER_ID=pgvector" \
    "AUTORAG_EMBEDDING_MODELS=vllm-embedding/nomic-embed-text-v1.5" \
    "AUTORAG_GENERATION_MODELS=vllm-inference/Qwen3.6-35B-A3B,openai/publishers/prelude-maas/models/glm-53-flash" \
    "AUTORAG_MAX_RAG_PATTERNS=8" \
    "AUTORAG_PRESET=balanced" \
    "AUTORAG_OPTIMIZATION_METRIC=faithfulness" \
    "MINIO_ENDPOINT=http://minio.${STORAGE_NS}.svc.cluster.local:9000" \
    "ARTIFACT_BUCKET=dsp-pipeline-artifacts"
  wait_deploy_ready "${BACKEND_DEPLOY}" "${CONSOLE_NS}"

  log "building the frontend plugin bundle"
  ( cd "${ROOT_DIR}/console" && yarn build )
  build_image "${FRONTEND_BC}" "${ROOT_DIR}/console"

  local frontend_image
  frontend_image="$(image_digest_ref "${FRONTEND_BC}")"
  log "deploying the console plugin chart with helm, image ${frontend_image}"
  helm upgrade --install "${HELM_RELEASE}" "${ROOT_DIR}/console/charts/openshift-console-plugin" \
    -n "${CONSOLE_NS}" \
    --set "plugin.image=${frontend_image}"

  cat <<EOF

console deployed. resolved for this cluster, nothing hardcoded:
  mlflow url:       ${MLFLOW_URL}
  dspa route:       ${DSPA_ROUTE}
  guardrails route: ${GUARDRAILS_ROUTE}
  minio console:    ${MINIO_CONSOLE_URL}
  rhoai dashboard:  ${RHOAI_DASHBOARD_URL}
check the console navigation under SpearShield > Knowledge Base once the
console operator finishes rolling the plugin in (watch with
oc get consoleplugin ${HELM_RELEASE}).
EOF
}

main "$@"

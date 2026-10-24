#!/usr/bin/env bash
set -euo pipefail
# openproject (spear-worktracker) plus agents, gateways, and openshell sandboxes (spear-agents).
# hostnames come from cluster routes or ingress domain; manifests use PLACEHOLDER substitution.
# openshell deploy runs as an in cluster job (see deploy-agents-job.yaml). coordinator stays on runc.
# seed_openproject_demo_data seeds demo users, project, work packages, and api tokens via rails.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

WORKTRACKER_MANIFESTS="${ROOT_DIR}/manifests/spear-worktracker"
AGENTS_MANIFESTS="${ROOT_DIR}/manifests/spear-agents"
SHARED_NS="redhat-ods-applications"

# openproject requires lower, upper, numeric, and a special character.
# a fixed prefix plus random alnum plus fixed suffix guarantees all four
# without a lookup table, same spirit as the openssl one liners already
# used below for the other generated secrets in this script.
gen_openproject_password() {
  echo "Sp$(openssl rand -base64 18 | tr -dc 'a-zA-Z0-9' | head -c 16)#7"
}

# spire idp and clients-federated flow (not in KeycloakRealmImport); see 01-keycloak-realm.yaml
ensure_spiffe_federated_auth() {
  local kc_host admin_user admin_pass admin_token kc spire_oidc_host bundle_endpoint
  kc_host="$(oc get route keycloak -n keycloak -o jsonpath='{.spec.host}')"
  admin_user="$(oc get secret keycloak-initial-admin -n keycloak -o jsonpath='{.data.username}' | base64 -d)"
  admin_pass="$(oc get secret keycloak-initial-admin -n keycloak -o jsonpath='{.data.password}' | base64 -d)"
  admin_token="$(curl -sk -X POST "https://${kc_host}/realms/master/protocol/openid-connect/token" \
    -d 'grant_type=password' -d 'client_id=admin-cli' \
    --data-urlencode "username=${admin_user}" --data-urlencode "password=${admin_pass}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null)"
  if [[ -z "${admin_token}" ]]; then
    echo "warning: could not get a keycloak admin token, skipping spiffe federated auth setup" >&2
    return 0
  fi
  kc="https://${kc_host}/admin/realms/${AGENTS_NS}"

  # realm import job runs async, poll with a bounded wait before using the admin sub api
  local tries=30
  while (( tries > 0 )); do
    curl -sk -o /dev/null -H "Authorization: Bearer ${admin_token}" "${kc}" && break
    sleep 2
    tries=$((tries - 1))
  done
  if (( tries == 0 )); then
    echo "warning: ${AGENTS_NS} realm never became reachable, skipping spiffe federated auth setup" >&2
    return 0
  fi

  spire_oidc_host="$(oc get route spire-oidc-discovery-provider -n zero-trust-workload-identity-manager -o jsonpath='{.spec.host}' 2>/dev/null || true)"
  if [[ -z "${spire_oidc_host}" ]]; then
    echo "warning: could not resolve spire's own oidc discovery route, skipping spiffe federated auth setup" >&2
    return 0
  fi
  bundle_endpoint="https://${spire_oidc_host}/keys"

  # cluster spire trust domain (see 01-keycloak-realm.yaml)
  local idp_body idp_status
  idp_body="{\"alias\":\"spire\",\"providerId\":\"spiffe\",\"enabled\":true,\"config\":{\"trustDomain\":\"spiffe://agent-pack.internal\",\"bundleEndpoint\":\"${bundle_endpoint}\",\"supportsClientAssertions\":\"true\"}}"
  idp_status="$(curl -sk -o /dev/null -w '%{http_code}' "${kc}/identity-provider/instances/spire" -H "Authorization: Bearer ${admin_token}")"
  if [[ "${idp_status}" == "404" ]]; then
    log "creating spiffe identity provider spire on ${AGENTS_NS}"
    curl -sk -o /dev/null -X POST "${kc}/identity-provider/instances" \
      -H "Authorization: Bearer ${admin_token}" -H "Content-Type: application/json" -d "${idp_body}"
  else
    log "spire identity provider already exists on ${AGENTS_NS}, re-syncing its bundle endpoint"
    curl -sk -o /dev/null -X PUT "${kc}/identity-provider/instances/spire" \
      -H "Authorization: Bearer ${admin_token}" -H "Content-Type: application/json" -d "${idp_body}"
  fi

  # bind clients-federated flow so federated-jwt authenticator runs
  local flow_alias="clients-federated" existing
  existing="$(curl -sk "${kc}/authentication/flows/${flow_alias}/executions" -H "Authorization: Bearer ${admin_token}")"
  if ! printf '%s' "${existing}" | grep -q '"federated-jwt"'; then
    log "copying builtin clients flow to ${flow_alias}"
    curl -sk -o /dev/null -X POST "${kc}/authentication/flows/clients/copy" \
      -H "Authorization: Bearer ${admin_token}" -H "Content-Type: application/json" \
      -d "{\"newName\": \"${flow_alias}\"}"
    existing="$(curl -sk "${kc}/authentication/flows/${flow_alias}/executions" -H "Authorization: Bearer ${admin_token}")"
    if ! printf '%s' "${existing}" | grep -q '"federated-jwt"'; then
      log "this keycloak version does not ship federated-jwt in the builtin flow, adding it explicitly"
      curl -sk -o /dev/null -X POST "${kc}/authentication/flows/${flow_alias}/executions/execution" \
        -H "Authorization: Bearer ${admin_token}" -H "Content-Type: application/json" \
        -d '{"provider": "federated-jwt"}'
    fi
  fi
  log "binding ${AGENTS_NS} realm's clientAuthenticationFlow to ${flow_alias}"
  curl -sk -o /dev/null -X PUT "${kc}" \
    -H "Authorization: Bearer ${admin_token}" -H "Content-Type: application/json" \
    -d "{\"clientAuthenticationFlow\": \"${flow_alias}\"}"
}

resolve_hosts() {
  log "resolving this cluster's own hostnames, never hardcoded"
  CLUSTER_DOMAIN="$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')"
  if [[ -z "${CLUSTER_DOMAIN}" ]]; then
    echo "could not resolve this cluster's apps domain" >&2
    exit 1
  fi
  SSO_HOST="$(oc get route keycloak -n keycloak -o jsonpath='{.spec.host}' 2>/dev/null || true)"
  if [[ -z "${SSO_HOST}" ]]; then
    echo "could not resolve the sso realm's own route (route 'keycloak' in namespace 'keycloak')" >&2
    exit 1
  fi
  GUARDRAILS_HOST="$(oc get route spear-guardrails -n "${GUARDRAILS_NS}" -o jsonpath='{.spec.host}' 2>/dev/null || true)"
  if [[ -z "${GUARDRAILS_HOST}" ]]; then
    echo "could not resolve the guardrails route, run scripts/07-install-guardrails.sh first" >&2
    exit 1
  fi
  GUARDRAILS_ROUTE="https://${GUARDRAILS_HOST}"
  SPIRE_OIDC_ROUTE="https://$(oc get route spire-oidc-discovery-provider -n zero-trust-workload-identity-manager -o jsonpath='{.spec.host}' 2>/dev/null || true)"
  if [[ -z "${SPIRE_OIDC_ROUTE}" || "${SPIRE_OIDC_ROUTE}" == "https://" ]]; then
    echo "could not resolve spire's own oidc discovery route (route 'spire-oidc-discovery-provider' in namespace 'zero-trust-workload-identity-manager')" >&2
    exit 1
  fi
  # cluster and service network cidrs for sandbox network policies
  CLUSTER_NETWORK_CIDR="$(oc get network.config.openshift.io cluster -o jsonpath='{.spec.clusterNetwork[0].cidr}')"
  SERVICE_NETWORK_CIDR="$(oc get network.config.openshift.io cluster -o jsonpath='{.spec.serviceNetwork[0]}')"
  if [[ -z "${CLUSTER_NETWORK_CIDR}" || -z "${SERVICE_NETWORK_CIDR}" ]]; then
    echo "could not resolve this cluster's own network/service cidrs" >&2
    exit 1
  fi

  # these two are ours, not pre-existing, constructed the same way the
  # reference's own deploy-agent-pack.sh builds MCP_PUBLIC_HOST, not
  # resolved after the fact, since the route/gateway hostname has to be
  # known before the Route/Gateway that will serve it is even applied
  OPENPROJECT_HOST="spear-openproject.${CLUSTER_DOMAIN}"
  MCP_PUBLIC_HOST="mcp-${AGENTS_NS}.${CLUSTER_DOMAIN}"
  ISSUER_URL="https://${SSO_HOST}/realms/${AGENTS_NS}"

  # mlflow url from Mlflow CR status
  MLFLOW_URL="$(oc get mlflow mlflow -n "${SHARED_NS}" -o jsonpath='{.status.url}' 2>/dev/null || true)"
  if [[ -z "${MLFLOW_URL}" ]]; then
    echo "could not resolve the mlflow cr's own route (mlflow 'mlflow' in namespace '${SHARED_NS}'), run scripts/08-install-console.sh first" >&2
    exit 1
  fi
  MLFLOW_HOST="${MLFLOW_URL#https://}"
  MLFLOW_HOST="${MLFLOW_HOST#http://}"
  MLFLOW_HOST="${MLFLOW_HOST%%/*}"

  log "cluster domain:    ${CLUSTER_DOMAIN}"
  log "sso issuer:        ${ISSUER_URL}"
  log "openproject host:  ${OPENPROJECT_HOST}"
  log "mcp public host:   ${MCP_PUBLIC_HOST}"
  log "guardrails route:  ${GUARDRAILS_ROUTE}"
  log "spire oidc route:  ${SPIRE_OIDC_ROUTE}"
  log "mlflow url:        ${MLFLOW_URL}"
}

apply_templated() {
  local file="$1"
  sed \
    -e "s|OPENPROJECT_HOST_PLACEHOLDER|${OPENPROJECT_HOST}|g" \
    -e "s|MCP_PUBLIC_HOST_PLACEHOLDER|${MCP_PUBLIC_HOST}|g" \
    -e "s|ISSUER_URL_PLACEHOLDER|${ISSUER_URL}|g" \
    -e "s|GUARDRAILS_ROUTE_PLACEHOLDER|${GUARDRAILS_ROUTE}|g" \
    -e "s|SPIRE_OIDC_ROUTE_PLACEHOLDER|${SPIRE_OIDC_ROUTE}|g" \
    -e "s|MCP_BACKEND_SECRET_PLACEHOLDER|${MCP_BACKEND_SECRET}|g" \
    "${file}" | oc apply -f -
}

# same placeholder convention as apply_templated, but for files the
# openshell cli consumes directly from a local path rather than oc apply
# (policies/coordinator-policy.yaml, providers/coordinator-a2a-token-grant.yaml).
# only substitutes; deploy_sandboxed_agents below runs the actual sandbox
# create / provider import / policy set sequence
render_openshell_file() {
  local src="$1" dest="$2"
  sed \
    -e "s|SSO_HOST_PLACEHOLDER|${SSO_HOST}|g" \
    -e "s|ISSUER_URL_PLACEHOLDER|${ISSUER_URL}|g" \
    -e "s|GUARDRAILS_HOST_PLACEHOLDER|${GUARDRAILS_HOST}|g" \
    -e "s|MLFLOW_HOST_PLACEHOLDER|${MLFLOW_HOST}|g" \
    -e "s|CLUSTER_NETWORK_CIDR_PLACEHOLDER|${CLUSTER_NETWORK_CIDR}|g" \
    -e "s|SERVICE_NETWORK_CIDR_PLACEHOLDER|${SERVICE_NETWORK_CIDR}|g" \
    "${src}" > "${dest}"
  log "rendered ${src} -> ${dest}"
}

# mcp gateway ext_proc defaults to STREAMED; pause controller and patch to BUFFERED
patch_mcp_ext_proc_buffered() {
  local filter="mcp-ext-proc-${AGENTS_NS}-gateway"
  log "unpausing mcp-gateway-controller just long enough to reconcile registrations"
  oc scale deployment/mcp-gateway-controller -n openshift-operators --replicas=1
  oc rollout status deployment/mcp-gateway-controller -n openshift-operators --timeout=90s

  for ns in "${WORKTRACKER_NS}" agent-pack; do
    for reg in $(oc get mcpserverregistration -n "${ns}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
      oc wait mcpserverregistration "${reg}" -n "${ns}" --for=condition=Ready --timeout=90s 2>&1 \
        || log "warning: ${reg} in ${ns} not Ready yet, tools/call for its tools may 404 until it is"
    done
  done

  log "pausing mcp-gateway-controller again and patching both ext_proc filters to BUFFERED"
  oc scale deployment/mcp-gateway-controller -n openshift-operators --replicas=0
  oc wait --for=delete pod -l app.kubernetes.io/name=mcp-gateway-controller -n openshift-operators --timeout=60s 2>/dev/null || true

  if oc get envoyfilter mcp-ext-proc-agent-pack-gateway -n agent-pack >/dev/null 2>&1; then
    oc patch envoyfilter mcp-ext-proc-agent-pack-gateway -n agent-pack --type=json \
      -p='[{"op":"replace","path":"/spec/configPatches/0/patch/value/typed_config/processing_mode/request_body_mode","value":"BUFFERED"}]'
  fi
  if oc get envoyfilter "${filter}" -n "${AGENTS_NS}" >/dev/null 2>&1; then
    oc patch envoyfilter "${filter}" -n "${AGENTS_NS}" --type=json \
      -p='[{"op":"replace","path":"/spec/configPatches/0/patch/value/typed_config/processing_mode/request_body_mode","value":"BUFFERED"}]'
  else
    log "warning: ${filter} not found yet, rerun this script once the gateway/registration have reconciled"
  fi
}

# recreates everything a wiped database needs beyond the admin account
# (OPENPROJECT_SEED_ADMIN_USER_* env vars handle that): both personas,
# the data-governance project with work_package_tracking actually
# enabled (plain Project.new.save! skips that), its three work packages,
# and each persona's api token. every step is find_by guarded, reruns are a no-op
seed_openproject_demo_data() {
  local pod bala_pw sidde_pw script_file out_file bala_token sidde_token
  pod="$(oc get pod -n "${WORKTRACKER_NS}" -l app=spear-openproject -o jsonpath='{.items[0].metadata.name}')"
  # generated fresh every run, cheap; only used below if ensure_user decides the persona is new
  bala_pw="$(gen_openproject_password)"
  sidde_pw="$(gen_openproject_password)"

  script_file="$(mktemp)"
  cat >"${script_file}" <<'RUBY'
def ensure_user(login:, firstname:, lastname:, mail:, password:)
  user = User.find_by(login: login)
  return user if user

  user = User.new(login: login, firstname: firstname, lastname: lastname, mail: mail, admin: false, language: "en")
  user.password = password
  user.password_confirmation = password
  user.status = 1
  user.force_password_change = false
  user.first_login = false
  user.save!
  puts "created user #{login}, id=#{user.id}"
  user
end

balakrishnan = ensure_user(login: "balakrishnan.b", firstname: "Balakrishnan", lastname: "B",
                            mail: "balakrishnan.b@spear-shield.demo", password: ENV.fetch("BALA_PW"))
siddhartha = ensure_user(login: "siddhartha.de", firstname: "Siddhartha", lastname: "De",
                          mail: "siddhartha.de@spear-shield.demo", password: ENV.fetch("SIDDE_PW"))

task_type = Type.find(1)
project = Project.find_by(identifier: "data-governance")
if project.nil?
  project = Project.new(
    name: "Data Governance", identifier: "data-governance", workspace_type: "project", public: false,
    description: "spear shield phase 2 demo project. real work packages backing the openproject mcp tool calls in architecture.html."
  )
  project.save!
  puts "created project data-governance, id=#{project.id}"
end
if project.enabled_module_names.exclude?("work_package_tracking")
  project.enabled_module_names += ["work_package_tracking"]
  project.save!
  puts "enabled work_package_tracking on data-governance"
end
ProjectType.find_or_create_by!(project_id: project.id, type_id: task_type.id)

member_role = Role.find_by!(name: "Member")
[balakrishnan, siddhartha].each do |user|
  next if Member.exists?(project_id: project.id, user_id: user.id)

  Member.create!(project_id: project.id, user_id: user.id, roles: [member_role])
  puts "added #{user.login} as Member on data-governance"
end

in_progress = Status.find(7)
closed = Status.find(12)
normal_priority = IssuePriority.find(8)
admin_user = User.find_by!(login: "admin")

def ensure_wp(subject:, project:, type:, status:, priority:, author:, assignee:)
  return if WorkPackage.exists?(subject: subject, project_id: project.id)

  wp = WorkPackage.new(subject: subject, project: project, type: type, status: status,
                        priority: priority, author: author, assigned_to: assignee)
  wp.save!(validate: false)
  puts "created work package '#{subject}', id=#{wp.id}"
end

ensure_wp(subject: "Review Q3 customer data retention policy compliance", project: project, type: task_type,
          status: in_progress, priority: normal_priority, author: admin_user, assignee: balakrishnan)
ensure_wp(subject: "Approve elevated data export access for analytics pipeline", project: project, type: task_type,
          status: in_progress, priority: normal_priority, author: admin_user, assignee: siddhartha)
ensure_wp(subject: "Audit trail for previously approved data governance exception", project: project, type: task_type,
          status: closed, priority: normal_priority, author: admin_user, assignee: siddhartha)

unless Token::API.exists?(user: balakrishnan)
  puts "TOKEN_FOR_balakrishnan.b=#{Token::API.create_and_return_value(balakrishnan)}"
end
unless Token::API.exists?(user: siddhartha)
  puts "TOKEN_FOR_siddhartha.de=#{Token::API.create_and_return_value(siddhartha)}"
end
RUBY

  oc cp "${script_file}" "${WORKTRACKER_NS}/${pod}:/tmp/spear-openproject-seed.rb"
  rm -f "${script_file}"

  out_file="$(mktemp)"
  oc exec -n "${WORKTRACKER_NS}" "${pod}" -- env BALA_PW="${bala_pw}" SIDDE_PW="${sidde_pw}" \
    bundle exec rails runner /tmp/spear-openproject-seed.rb >"${out_file}" 2>&1
  oc exec -n "${WORKTRACKER_NS}" "${pod}" -- rm -f /tmp/spear-openproject-seed.rb

  sed 's/^/  /' "${out_file}"

  bala_token="$(grep -o 'TOKEN_FOR_balakrishnan\.b=.*' "${out_file}" | cut -d= -f2- || true)"
  sidde_token="$(grep -o 'TOKEN_FOR_siddhartha\.de=.*' "${out_file}" | cut -d= -f2- || true)"

  # only persist a persona password if ensure_user actually created that
  # user this run, same create-once-never-touch rule as every other
  # generated secret in this script, an already existing persona's real
  # password (which may differ from this run's freshly generated one if
  # it was ever changed by hand) must never be overwritten
  if grep -q "created user balakrishnan.b," "${out_file}"; then
    log "persisting newly generated balakrishnan.b password to spear-openproject-login-creds"
    oc patch secret spear-openproject-login-creds -n "${WORKTRACKER_NS}" --type=merge \
      -p "{\"stringData\":{\"balakrishnan.b-password\":\"${bala_pw}\"}}"
  fi
  if grep -q "created user siddhartha.de," "${out_file}"; then
    log "persisting newly generated siddhartha.de password to spear-openproject-login-creds"
    oc patch secret spear-openproject-login-creds -n "${WORKTRACKER_NS}" --type=merge \
      -p "{\"stringData\":{\"siddhartha.de-password\":\"${sidde_pw}\"}}"
  fi
  rm -f "${out_file}"

  if [[ -n "${bala_token}" || -n "${sidde_token}" ]]; then
    log "persisting newly minted api token(s) to spear-openproject-api-tokens"
    if oc get secret spear-openproject-api-tokens -n "${WORKTRACKER_NS}" >/dev/null 2>&1; then
      local patch="{\"stringData\":{"
      [[ -n "${bala_token}" ]] && patch+="\"balakrishnan.b\":\"${bala_token}\","
      [[ -n "${sidde_token}" ]] && patch+="\"siddhartha.de\":\"${sidde_token}\","
      patch="${patch%,}}}"
      oc patch secret spear-openproject-api-tokens -n "${WORKTRACKER_NS}" --type=merge -p "${patch}"
    else
      oc create secret generic spear-openproject-api-tokens -n "${WORKTRACKER_NS}" \
        --from-literal=balakrishnan.b="${bala_token}" \
        --from-literal=siddhartha.de="${sidde_token}"
    fi
  fi
}

# use kata only when a kata-oc node has /dev/kvm (retrieval agent only)
detect_agent_runtime_class() {
  local node
  node="$(oc get nodes -l node-role.kubernetes.io/kata-oc -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -z "${node}" ]]; then
    echo ""
    return
  fi
  if oc debug node/"${node}" -- chroot /host test -e /dev/kvm >/dev/null 2>&1; then
    echo "kata"
  else
    echo ""
  fi
}

# default sandbox restartPolicy Never does not survive node reboot; patch Always
patch_sandbox_restart_policy() {
  local sandbox="$1"
  if oc patch sandbox "${sandbox}" -n "${AGENTS_NS}" \
    --type=merge \
    -p '{"spec":{"podTemplate":{"spec":{"restartPolicy":"Always"}}}}' 2>&1; then
    log "restartPolicy=Always patched onto sandbox ${sandbox}, kubelet now self-heals node reboots"
  else
    echo "warning: could not patch restartPolicy on sandbox ${sandbox}, node reboots will still need a manual re-run" >&2
  fi
}

# jobs are immutable once created, oc apply on an existing one just
# errors on the first changed field, so a rerun always deletes first.
# --wait blocks until the old pod is actually gone, otherwise the new
# job's own pod could start before the old one finished terminating
delete_job_if_exists() {
  local name="$1" ns="$2"
  if oc get "job/${name}" -n "${ns}" >/dev/null 2>&1; then
    log "deleting existing job ${name} in ${ns} before recreating it"
    oc delete "job/${name}" -n "${ns}" --wait=true --timeout=60s
  fi
}

# oc wait --for=condition=complete alone just times out on a job that
# already permanently failed, condition=Failed never becomes true for
# that flag. polls both terminal conditions instead
wait_agents_job() {
  local name="$1" ns="$2" timeout="${3:-600}"
  local elapsed=0 status
  while (( elapsed < timeout )); do
    status="$(oc get "job/${name}" -n "${ns}" -o jsonpath='{.status.conditions[?(@.status=="True")].type}' 2>/dev/null || true)"
    case "${status}" in
      *Complete*) return 0 ;;
      *Failed*) return 1 ;;
    esac
    sleep 5
    elapsed=$((elapsed + 5))
  done
  echo "job ${name} in ${ns} did not reach a terminal state within ${timeout}s" >&2
  return 1
}

# builds the openshell cli image, renders this project's own three
# openshell config files (same render_openshell_file as before, pure
# sed, no openshell binary needed on the installer's own machine for
# this part), hands them to the job as a configmap, and runs the real
# sandbox create / provider import / policy set sequence as a Job
# against the shared gateway service's own in cluster dns name. nothing
# here touches the installer's own machine's network beyond oc itself
run_agents_deploy_job() {
  log "building the openshell cli image in cluster"
  oc apply -f "${AGENTS_MANIFESTS}/openshell/cli-build.yaml"
  oc start-build openshell-cli --from-dir="${ROOT_DIR}/agents" --wait -n "${AGENTS_NS}"

  local tmp
  tmp="$(mktemp -d)"
  render_openshell_file "${AGENTS_MANIFESTS}/openshell/providers/coordinator-a2a-token-grant.yaml" "${tmp}/coordinator-a2a-token-grant.yaml"
  render_openshell_file "${AGENTS_MANIFESTS}/openshell/policies/retrieval-policy.yaml" "${tmp}/retrieval-policy.yaml"
  render_openshell_file "${AGENTS_MANIFESTS}/openshell/policies/coordinator-policy.yaml" "${tmp}/coordinator-policy.yaml"
  oc create configmap spear-shield-openshell-agent-configs -n "${AGENTS_NS}" \
    --from-file="${tmp}" \
    --dry-run=client -o yaml | oc apply -f -
  rm -rf "${tmp}"

  log "checking kata-oc nodes for /dev/kvm before deciding the retrieval agent's runtime class"
  local runtime_class
  runtime_class="$(detect_agent_runtime_class)"
  if [[ -n "${runtime_class}" ]]; then
    log "kata nodes have /dev/kvm, deploying spear-retrieval under kata"
  else
    log "no usable kata node found, spear-retrieval falls back to runc"
  fi

  # not apply_templated, that helper pipes straight to oc apply with no
  # way to add this job's own extra placeholder on top, so this does
  # its own sed pass with the same two tokens plus AGENT_RUNTIME_CLASS
  delete_job_if_exists deploy-spear-sandboxed-agents "${AGENTS_NS}"
  sed \
    -e "s|MCP_PUBLIC_HOST_PLACEHOLDER|${MCP_PUBLIC_HOST}|g" \
    -e "s|GUARDRAILS_ROUTE_PLACEHOLDER|${GUARDRAILS_ROUTE}|g" \
    -e "s|MLFLOW_URL_PLACEHOLDER|${MLFLOW_URL}|g" \
    -e "s|AGENT_RUNTIME_CLASS_PLACEHOLDER|${runtime_class}|g" \
    "${AGENTS_MANIFESTS}/openshell/deploy-agents-job.yaml" | oc apply -f -

  log "waiting on job/deploy-spear-sandboxed-agents"
  if ! wait_agents_job deploy-spear-sandboxed-agents "${AGENTS_NS}" 600; then
    echo "deploy-spear-sandboxed-agents job failed, its own log:" >&2
    oc logs "job/deploy-spear-sandboxed-agents" -n "${AGENTS_NS}" --all-containers || true
    exit 1
  fi
  oc logs "job/deploy-spear-sandboxed-agents" -n "${AGENTS_NS}" --all-containers || true

  patch_sandbox_restart_policy default--spear-retrieval
  patch_sandbox_restart_policy default--spear-coordinator
}

# build agent images and openshell sandboxes; names must match 07-a2a-gateway.yaml
deploy_sandboxed_agents() {
  log "building spear-coordinator-agent and spear-retrieval-agent images in cluster"
  oc apply -f "${AGENTS_MANIFESTS}/openshell/agent-builds.yaml"
  oc start-build spear-coordinator-agent --from-dir="${ROOT_DIR}/agents" --wait -n "${AGENTS_NS}"
  oc start-build spear-retrieval-agent --from-dir="${ROOT_DIR}/agents" --wait -n "${AGENTS_NS}"

  # openshift refuses cross namespace pulls from the internal registry
  # without this grant; without it the relay in spear-data sits in
  # ImagePullBackOff against spear-agents/spear-retrieval-agent
  log "allow spear-data's default sa to pull spear-retrieval-agent from ${AGENTS_NS}"
  oc apply -f "${AGENTS_MANIFESTS}/09-rag-query-relay-image-puller-rbac.yaml"

  log "rag-query-relay, same image as spear-retrieval-agent, now that it actually exists"
  oc apply -f "${ROOT_DIR}/manifests/spear-data/02-rag-query-relay.yaml"
  wait_deploy_ready spear-shield-rag-query-relay "${DATA_NS}"

  log "deploying the two sandboxed agents, as a real in cluster job, see run_agents_deploy_job"
  run_agents_deploy_job

  log "sandboxed agents deployed"
  oc get sandbox -n "${AGENTS_NS}"
}

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }
  command -v openssl >/dev/null 2>&1 || { echo "openssl is required" >&2; exit 1; }

  # already applied once by 07-install-guardrails.sh's ensure_early_gateway;
  # harmless no-op here, kept so this script stands alone if run in isolation
  log "spear-agents namespace, keycloak realm import"
  oc apply -f "${AGENTS_MANIFESTS}/00-namespace.yaml"
  oc apply -f "${AGENTS_MANIFESTS}/01-keycloak-realm.yaml"

  log "spire idp and clients-federated auth flow, live admin api only, not expressible in the realm import above"
  ensure_spiffe_federated_auth

  log "spear-worktracker namespace"
  oc get namespace "${WORKTRACKER_NS}" >/dev/null 2>&1 || oc create namespace "${WORKTRACKER_NS}"

  log "openproject db and app secrets, generated once, never regenerated against an already running instance"
  ensure_secret spear-openproject-db-creds "${WORKTRACKER_NS}" \
    --from-literal=user=openproject \
    --from-literal=password="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)" \
    --from-literal=dbname=openproject
  ensure_secret spear-openproject-app-secrets "${WORKTRACKER_NS}" \
    --from-literal=secret_key_base="$(openssl rand -hex 32)"
  # admin-password feeds OPENPROJECT_SEED_ADMIN_USER_PASSWORD below, must
  # exist first; persona passwords are owned by seed_openproject_demo_data instead
  ensure_secret spear-openproject-login-creds "${WORKTRACKER_NS}" \
    --from-literal=admin-username=admin \
    --from-literal=admin-password="$(gen_openproject_password)"
  ensure_secret spear-openproject-mcp-backend-secret "${WORKTRACKER_NS}" \
    --from-literal=token="$(openssl rand -hex 24)"
  oc label secret spear-openproject-mcp-backend-secret -n "${WORKTRACKER_NS}" mcp.kuadrant.io/secret=true --overwrite
  MCP_BACKEND_SECRET="$(oc get secret spear-openproject-mcp-backend-secret -n "${WORKTRACKER_NS}" -o jsonpath='{.data.token}' | base64 -d)"

  resolve_hosts

  log "openproject db, scc, and the app itself"
  oc apply -f "${WORKTRACKER_MANIFESTS}/01-openproject-db.yaml"
  wait_deploy_ready spear-openproject-db "${WORKTRACKER_NS}"
  oc apply -f "${WORKTRACKER_MANIFESTS}/03-openproject-scc.yaml"
  apply_templated "${WORKTRACKER_MANIFESTS}/02-openproject.yaml"
  wait_deploy_ready spear-openproject "${WORKTRACKER_NS}" 300s

  log "openproject demo data: personas, data-governance project, work packages, api tokens"
  seed_openproject_demo_data

  log "spear-mcp-gateway plus its own auth policies"
  apply_templated "${AGENTS_MANIFESTS}/02-mcp-gateway.yaml"

  log "spear-openproject-mcp code configmap"
  oc create configmap spear-openproject-mcp-code -n "${WORKTRACKER_NS}" \
    --from-file=server.py="${ROOT_DIR}/mcp-servers/spear-openproject-mcp/server.py" \
    --dry-run=client -o yaml | oc apply -f -

  log "spear-openproject-mcp: MCPServer, HTTPRoute, MCPServerRegistration, MCPVirtualServer"
  apply_templated "${WORKTRACKER_MANIFESTS}/04-openproject-mcp.yaml"
  oc wait mcpserver spear-openproject-mcp -n "${WORKTRACKER_NS}" --for=condition=Ready --timeout=120s

  # guardrails rbac for both spear-openproject-mcp's and spear-coordinator's
  # own service accounts already applied by scripts/07-install-guardrails.sh
  # (manifests/spear-guardrails/07-caller-rbac.yaml), not repeated here

  log "spear-mcp-guard-proxy code configmap, deployment, service, referencegrant for its new cross namespace backendref"
  oc create configmap spear-mcp-guard-proxy-code -n "${AGENTS_NS}" \
    --from-file=server.py="${ROOT_DIR}/mcp-servers/spear-mcp-guard-proxy/server.py" \
    --dry-run=client -o yaml | oc apply -f -
  apply_templated "${AGENTS_MANIFESTS}/04-mcp-guard-proxy.yaml"
  # a configmap update alone never reaches an already running pod, only a
  # fresh pod mounts the new content, so force one on every run rather
  # than silently serving stale server.py against a cluster that is not
  # a brand new install
  oc rollout restart deployment/spear-mcp-guard-proxy -n "${AGENTS_NS}"

  log "coordinator guardrails sa and long lived token (sandboxes do not automount sa tokens)"
  oc apply -f "${AGENTS_MANIFESTS}/openshell/coordinator-guardrails-sa.yaml"

  log "mlflow tracing credentials for sandbox agents"
  oc apply -f "${AGENTS_MANIFESTS}/openshell/agents-mlflow-sa.yaml"
  oc apply -f "${ROOT_DIR}/manifests/spear-pipelines/06-agents-mlflow-rbac.yaml"

  log "spear-a2a-gateway, cluster spiffeid for the two sandboxed agents"
  oc apply -f "${AGENTS_MANIFESTS}/06-cluster-spiffeid.yaml"
  apply_templated "${AGENTS_MANIFESTS}/07-a2a-gateway.yaml"

  log "console backend's own cross namespace rbac to read/exec into the two sandbox pods, applied here too since this is where the target pods live"
  oc apply -f "${AGENTS_MANIFESTS}/08-console-backend-rbac.yaml"

  log "console backend's own read only rbac to name the workload identity pathway live, applied here too since the clusterspiffeid/authpolicy it reads were just created above"
  oc apply -f "${AGENTS_MANIFESTS}/10-console-backend-pathway-rbac.yaml"

  log "ext_proc body mode fix (see comment on patch_mcp_ext_proc_buffered)"
  patch_mcp_ext_proc_buffered

  log "sandboxed agents: image builds, rag-query-relay, openshell sandbox create/policy/provider"
  deploy_sandboxed_agents

  # keep this block to commands the user actually runs next, nothing
  # else. narrative/caveat text belongs in a source comment, not here,
  # see git history on this block for the longer reasoning if ever
  # needed again.
  cat <<EOF

spear-agents and spear-worktracker deployed.
  openproject:  https://${OPENPROJECT_HOST}
  mcp gateway:  https://${MCP_PUBLIC_HOST}/mcp
  sso issuer:   ${ISSUER_URL}

credentials (cluster secrets, none in this repo):
  openproject admin login:          oc get secret spear-openproject-login-creds -n ${WORKTRACKER_NS} -o jsonpath='{.data.admin-password}' | base64 -d
  balakrishnan.b openproject login: oc get secret spear-openproject-login-creds -n ${WORKTRACKER_NS} -o jsonpath='{.data.balakrishnan\.b-password}' | base64 -d
  siddhartha.de openproject login:  oc get secret spear-openproject-login-creds -n ${WORKTRACKER_NS} -o jsonpath='{.data.siddhartha\.de-password}' | base64 -d
  balakrishnan.b api token:         oc get secret spear-openproject-api-tokens -n ${WORKTRACKER_NS} -o jsonpath='{.data.balakrishnan\.b}' | base64 -d
  siddhartha.de api token:          oc get secret spear-openproject-api-tokens -n ${WORKTRACKER_NS} -o jsonpath='{.data.siddhartha\.de}' | base64 -d
  spear-agents realm users:  oc get keycloakrealmimport spear-agents-realm -n keycloak -o jsonpath='{range .spec.realm.users[*]}{.username}{" "}{.credentials}{"\n"}{end}'

sandbox status:  oc get sandbox -n ${AGENTS_NS}

if a sandbox was ever recreated under a new name, confirm the coordinator's spiffe identity still resolves:
  oc get pod -n ${AGENTS_NS} -l openshell.ai/managed-by=openshell -o jsonpath='{.items[0].metadata.labels.agents\.x-k8s\.io/sandbox-name-hash}{"\n"}'
  (compare against manifests/spear-agents/06-cluster-spiffeid.yaml's podSelector value, patch if it differs)
EOF
}

main "$@"

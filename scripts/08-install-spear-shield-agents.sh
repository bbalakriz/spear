#!/usr/bin/env bash
set -euo pipefail
# phase 2's spear-shield-agents namespace: the realm, the self hosted
# openproject work tracker, spear-openproject-mcp, the gateway/auth
# policy chain in front of it, the guard proxy, and the a2a gateway plus
# cluster spiffeid the two sandboxed agents depend on. see PHASE2_PLAN.md
# sections 2, 4, 5, 6, 7. the sandboxed agents themselves (image builds,
# sandbox create, policy, provider) are still a manual command sequence,
# not scripted end to end here, see the comment above the agents block
# in main() below
#
# nothing in here is cluster specific. every hostname (this cluster's own
# apps domain, the sso route, the guardrails route) is either constructed
# from `oc get ingresses.config.openshift.io cluster` or resolved from an
# already live Route, same convention scripts/07-install-owner-console.sh
# already uses, never typed in literally. every manifest under
# manifests/50-spear-shield-agents/ that needs one of these carries a
# PLACEHOLDER token instead of a real value, substituted here before
# apply, same pattern the sibling rhoai-agentic-demo project's own
# deploy-agent-pack.sh uses.
#
# admin login, both personas' native accounts, the data-governance
# project, its three work packages, and both personas' api tokens are
# all seeded here too, see seed_openproject_demo_data(). an earlier
# version of this comment claimed personal api tokens need each persona
# to log in themselves, there is no admin api for it. found live during
# this build's schema drift recovery (PHASE2_PLAN.md section 4): that is
# true of openproject's own rest api, but not of rails console, which an
# admin already has full access to via oc exec, so this is scripted now
# too, the whole openproject side of this namespace is recoverable from
# a wiped database with a single rerun of this script.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

NS="spear-shield-agents"
RAG_NS="rag-phase1"
MANIFESTS="${ROOT_DIR}/manifests/50-spear-shield-agents"

# openproject requires lower, upper, numeric, and a special character.
# a fixed prefix plus random alnum plus fixed suffix guarantees all four
# without a lookup table, same spirit as the openssl one liners already
# used below for the other generated secrets in this script.
gen_openproject_password() {
  echo "Sp$(openssl rand -base64 18 | tr -dc 'a-zA-Z0-9' | head -c 16)#7"
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
  GUARDRAILS_ROUTE="https://$(oc get route rag-phase1-guardrails -n "${RAG_NS}" -o jsonpath='{.spec.host}' 2>/dev/null || true)"
  if [[ -z "${GUARDRAILS_ROUTE}" || "${GUARDRAILS_ROUTE}" == "https://" ]]; then
    echo "could not resolve the guardrails route, run scripts/10-install-guardrails.sh first" >&2
    exit 1
  fi
  SPIRE_OIDC_ROUTE="https://$(oc get route spire-oidc-discovery-provider -n zero-trust-workload-identity-manager -o jsonpath='{.spec.host}' 2>/dev/null || true)"
  if [[ -z "${SPIRE_OIDC_ROUTE}" || "${SPIRE_OIDC_ROUTE}" == "https://" ]]; then
    echo "could not resolve spire's own oidc discovery route (route 'spire-oidc-discovery-provider' in namespace 'zero-trust-workload-identity-manager')" >&2
    exit 1
  fi
  # the two cidrs every openshell sandbox network policy in this project
  # allowlists, read from the real cluster config, not typed in, same
  # values confirmed live earlier this session via this exact command
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
  MCP_PUBLIC_HOST="mcp-spear-shield-agents.${CLUSTER_DOMAIN}"
  ISSUER_URL="https://${SSO_HOST}/realms/spear-shield-agents"

  log "cluster domain:    ${CLUSTER_DOMAIN}"
  log "sso issuer:        ${ISSUER_URL}"
  log "openproject host:  ${OPENPROJECT_HOST}"
  log "mcp public host:   ${MCP_PUBLIC_HOST}"
  log "guardrails route:  ${GUARDRAILS_ROUTE}"
  log "spire oidc route:  ${SPIRE_OIDC_ROUTE}"
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

# same placeholder convention as apply_templated above, but for files the
# openshell cli consumes directly from a local path rather than oc apply,
# manifests/50-spear-shield-agents/openshell/policies/coordinator-policy.yaml
# and openshell/providers/coordinator-a2a-token-grant.yaml. writes the
# substituted copy to a tmp path and prints it, caller passes that path to
# the relevant openshell command. SSO_HOST_PLACEHOLDER is the bare host
# (no scheme), the policy's own network_policies entries need a bare host,
# not a url. this only substitutes, it does not itself run openshell, the
# sandbox create / provider import / policy set sequence for the two
# agents is still a manual, documented sequence, not scripted end to end
# yet, see PHASE2_PLAN.md section 7 for the real commands actually run
render_openshell_file() {
  local src="$1" dest="$2"
  sed \
    -e "s|SSO_HOST_PLACEHOLDER|${SSO_HOST}|g" \
    -e "s|ISSUER_URL_PLACEHOLDER|${ISSUER_URL}|g" \
    -e "s|CLUSTER_NETWORK_CIDR_PLACEHOLDER|${CLUSTER_NETWORK_CIDR}|g" \
    -e "s|SERVICE_NETWORK_CIDR_PLACEHOLDER|${SERVICE_NETWORK_CIDR}|g" \
    "${src}" > "${dest}"
  log "rendered ${src} -> ${dest}"
}

# same known limitation as the reference project's own
# patch_mcp_ext_proc_buffered (see its ARCHITECTURE.md, "Known operator
# limitation: ext_proc body mode"): the installed mcp-gateway-controller
# (v0.7.1 preview) always regenerates its ext_proc EnvoyFilter with
# request_body_mode: STREAMED, which a plain stdlib http backend's
# tools/call can't parse ("Bad request syntax"), no CRD field exists to
# change it. fix found and confirmed live during this build, section 4:
# let the controller run once to reconcile every MCPServerRegistration to
# Ready, pause it, then hand patch both EnvoyFilters (this gateway's own,
# and agent-pack's, since the controller is shared cluster wide and briefly
# reverts agent-pack's back to STREAMED too while it runs) to BUFFERED,
# and leave the controller paused.
patch_mcp_ext_proc_buffered() {
  local filter="mcp-ext-proc-${NS}-gateway"
  log "unpausing mcp-gateway-controller just long enough to reconcile registrations"
  oc scale deployment/mcp-gateway-controller -n openshift-operators --replicas=1
  oc rollout status deployment/mcp-gateway-controller -n openshift-operators --timeout=90s

  for ns in "${NS}" agent-pack; do
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
  if oc get envoyfilter "${filter}" -n "${NS}" >/dev/null 2>&1; then
    oc patch envoyfilter "${filter}" -n "${NS}" --type=json \
      -p='[{"op":"replace","path":"/spec/configPatches/0/patch/value/typed_config/processing_mode/request_body_mode","value":"BUFFERED"}]'
  else
    log "warning: ${filter} not found yet, rerun this script once the gateway/registration have reconciled"
  fi
}

# recreates everything a wiped openproject database needs beyond the
# admin account itself (that part is handled by OPENPROJECT_SEED_ADMIN_USER_*
# env vars on the deployment): both personas' native accounts, the
# data-governance project with its work_package_tracking module actually
# enabled (a plain Project.new.save! skips that, found live, left
# view_work_packages silently false for every member until fixed), its
# three work packages, and each persona's personal api token. every step
# is guarded by a find_by check so reruns against an already seeded
# instance are a no-op, and an existing api token is never destroyed and
# reissued, only minted the first time.
seed_openproject_demo_data() {
  local pod bala_pw sidde_pw script_file out_file bala_token sidde_token
  pod="$(oc get pod -n "${NS}" -l app=spear-openproject -o jsonpath='{.items[0].metadata.name}')"
  # generated fresh every run, cheap, only ever actually used below if
  # ensure_user decides that persona doesn't exist yet, never read back
  # out of a secret that may not have these keys yet
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

  oc cp "${script_file}" "${NS}/${pod}:/tmp/spear-openproject-seed.rb"
  rm -f "${script_file}"

  out_file="$(mktemp)"
  oc exec -n "${NS}" "${pod}" -- env BALA_PW="${bala_pw}" SIDDE_PW="${sidde_pw}" \
    bundle exec rails runner /tmp/spear-openproject-seed.rb >"${out_file}" 2>&1
  oc exec -n "${NS}" "${pod}" -- rm -f /tmp/spear-openproject-seed.rb

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
    oc patch secret spear-openproject-login-creds -n "${NS}" --type=merge \
      -p "{\"stringData\":{\"balakrishnan.b-password\":\"${bala_pw}\"}}"
  fi
  if grep -q "created user siddhartha.de," "${out_file}"; then
    log "persisting newly generated siddhartha.de password to spear-openproject-login-creds"
    oc patch secret spear-openproject-login-creds -n "${NS}" --type=merge \
      -p "{\"stringData\":{\"siddhartha.de-password\":\"${sidde_pw}\"}}"
  fi
  rm -f "${out_file}"

  if [[ -n "${bala_token}" || -n "${sidde_token}" ]]; then
    log "persisting newly minted api token(s) to spear-openproject-api-tokens"
    if oc get secret spear-openproject-api-tokens -n "${NS}" >/dev/null 2>&1; then
      local patch="{\"stringData\":{"
      [[ -n "${bala_token}" ]] && patch+="\"balakrishnan.b\":\"${bala_token}\","
      [[ -n "${sidde_token}" ]] && patch+="\"siddhartha.de\":\"${sidde_token}\","
      patch="${patch%,}}}"
      oc patch secret spear-openproject-api-tokens -n "${NS}" --type=merge -p "${patch}"
    else
      oc create secret generic spear-openproject-api-tokens -n "${NS}" \
        --from-literal=balakrishnan.b="${bala_token}" \
        --from-literal=siddhartha.de="${sidde_token}"
    fi
  fi
}

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }
  command -v openssl >/dev/null 2>&1 || { echo "openssl is required" >&2; exit 1; }

  log "namespace, keycloak realm import"
  oc apply -f "${MANIFESTS}/00-namespace.yaml"
  oc apply -f "${MANIFESTS}/01-keycloak-realm.yaml"

  log "openproject db and app secrets, generated once, never regenerated against an already running instance"
  ensure_secret spear-openproject-db-creds "${NS}" \
    --from-literal=user=openproject \
    --from-literal=password="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)" \
    --from-literal=dbname=openproject
  ensure_secret spear-openproject-app-secrets "${NS}" \
    --from-literal=secret_key_base="$(openssl rand -hex 32)"
  # admin-password is read straight into OPENPROJECT_SEED_ADMIN_USER_PASSWORD
  # by 03-openproject.yaml below, has to exist before that manifest applies.
  # the two persona passwords are not generated here on purpose, see
  # seed_openproject_demo_data, that step owns generating and persisting
  # those itself, the same way it already owns minting api tokens, so it
  # never depends on a key existing in this secret ahead of it
  ensure_secret spear-openproject-login-creds "${NS}" \
    --from-literal=admin-username=admin \
    --from-literal=admin-password="$(gen_openproject_password)"
  ensure_secret spear-openproject-mcp-backend-secret "${NS}" \
    --from-literal=token="$(openssl rand -hex 24)"
  oc label secret spear-openproject-mcp-backend-secret -n "${NS}" mcp.kuadrant.io/secret=true --overwrite
  MCP_BACKEND_SECRET="$(oc get secret spear-openproject-mcp-backend-secret -n "${NS}" -o jsonpath='{.data.token}' | base64 -d)"

  resolve_hosts

  log "openproject db, scc, and the app itself"
  oc apply -f "${MANIFESTS}/02-openproject-db.yaml"
  wait_deploy_ready spear-openproject-db "${NS}"
  oc apply -f "${MANIFESTS}/04-openproject-scc.yaml"
  apply_templated "${MANIFESTS}/03-openproject.yaml"
  wait_deploy_ready spear-openproject "${NS}" 300s

  log "openproject demo data: personas, data-governance project, work packages, api tokens"
  seed_openproject_demo_data

  log "spear-shield-gateway, auth policies"
  apply_templated "${MANIFESTS}/05-gateway.yaml"
  apply_templated "${MANIFESTS}/06-auth-policies.yaml"

  log "spear-openproject-mcp code configmap"
  oc create configmap spear-openproject-mcp-code -n "${NS}" \
    --from-file=server.py="${ROOT_DIR}/mcp-servers/spear-openproject-mcp/server.py" \
    --dry-run=client -o yaml | oc apply -f -

  log "spear-openproject-mcp: MCPServer, HTTPRoute, MCPServerRegistration, MCPVirtualServer"
  apply_templated "${MANIFESTS}/07-openproject-mcp.yaml"
  oc wait mcpserver spear-openproject-mcp -n "${NS}" --for=condition=Ready --timeout=120s

  log "guardrails rbac for spear-openproject-mcp's own service account"
  oc apply -f "${MANIFESTS}/09-guardrails-rbac.yaml"

  log "ext_proc body mode fix (see comment on patch_mcp_ext_proc_buffered)"
  patch_mcp_ext_proc_buffered

  log "spear-shield-mcp-guard-proxy code configmap, deployment, service"
  oc create configmap spear-shield-mcp-guard-proxy-code -n "${NS}" \
    --from-file=server.py="${ROOT_DIR}/mcp-servers/spear-shield-mcp-guard-proxy/server.py" \
    --dry-run=client -o yaml | oc apply -f -
  apply_templated "${MANIFESTS}/08-mcp-guard-proxy.yaml"

  log "spear-shield-a2a-gateway, cluster spiffeid for the two sandboxed agents"
  oc apply -f "${MANIFESTS}/10-cluster-spiffeid.yaml"
  apply_templated "${MANIFESTS}/11-a2a-gateway.yaml"

  # the sandboxed agents themselves, build/create/policy/provider, are
  # still a manual, documented command sequence, not scripted end to end
  # here, see PHASE2_PLAN.md section 7 for the real commands. the two
  # files those commands consume, openshell/policies/coordinator-policy.yaml
  # and openshell/providers/coordinator-a2a-token-grant.yaml, now carry
  # the same placeholder convention as everything else in this script,
  # render_openshell_file above substitutes them the same way, run it
  # before passing either file to an openshell command

  cat <<EOF

spear-shield-agents deployed. resolved for this cluster, nothing hardcoded:
  openproject:  https://${OPENPROJECT_HOST}
  mcp gateway:  https://${MCP_PUBLIC_HOST}/mcp
  sso issuer:   ${ISSUER_URL}

credentials, none of them in this repo, all in cluster secrets:
  openproject admin login:         oc get secret spear-openproject-login-creds -n ${NS} -o jsonpath='{.data.admin-password}' | base64 -d
  balakrishnan.b openproject login: oc get secret spear-openproject-login-creds -n ${NS} -o jsonpath='{.data.balakrishnan\.b-password}' | base64 -d
  siddhartha.de openproject login:  oc get secret spear-openproject-login-creds -n ${NS} -o jsonpath='{.data.siddhartha\.de-password}' | base64 -d
  balakrishnan.b api token:         oc get secret spear-openproject-api-tokens -n ${NS} -o jsonpath='{.data.balakrishnan\.b}' | base64 -d
  siddhartha.de api token:          oc get secret spear-openproject-api-tokens -n ${NS} -o jsonpath='{.data.siddhartha\.de}' | base64 -d
  spear-shield-agents realm users:  oc get keycloakrealmimport spear-shield-agents-realm -n keycloak -o jsonpath='{range .spec.realm.users[*]}{.username}{" "}{.credentials}{"\n"}{end}'
EOF
}

main "$@"

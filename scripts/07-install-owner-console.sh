#!/usr/bin/env bash
set -euo pipefail
# builds and deploys the owner console: the backend (manifests/40-owner-console)
# plus the frontend openshift console plugin (owner-console/charts/openshift-console-plugin).
# both images are built binary, straight from source, into this namespace's
# own internal registry, same pattern every other custom image in this
# project already uses.
#
# MLFLOW_URL, DSPA_ROUTE and GUARDRAILS_ROUTE are never hardcoded anywhere in
# owner-console-backend/server.py: all three are openshift route hostnames,
# unique per cluster, so this script resolves them live and injects them
# into the backend deployment with `oc set env`, not the applied manifest
# itself, so a later `oc apply -f 01-backend.yaml` never wipes them out
# (they are absent from the manifest's own last applied configuration, so
# strategic merge leaves them alone, confirmed against how minio-dsp-creds
# style secrets are already handled in this project).
#
# run scripts/01-install-minio.sh, 02-install-pipeline-server.sh,
# 03-install-ogx.sh and 04-install-mlflow.sh first, this script assumes all
# of rag-phase1's own routes (dspa, guardrails) and the shared mlflow
# instance already exist.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

RAG_NS="rag-phase1"
SHARED_NS="redhat-ods-applications"
BACKEND_BC="phase1-ingestion-console-backend"
FRONTEND_BC="phase1-ingestion-console"
BACKEND_DEPLOY="phase1-ingestion-console-backend"
HELM_RELEASE="phase1-ingestion-console"

build_image() {
  local bc="$1" context_dir="$2"
  if ! oc get bc "${bc}" -n "${RAG_NS}" >/dev/null 2>&1; then
    log "creating buildconfig ${bc}"
    oc new-build --binary --strategy=docker --name="${bc}" -n "${RAG_NS}"
  fi
  log "building and pushing ${bc} from ${context_dir}"
  oc start-build "${bc}" -n "${RAG_NS}" --from-dir="${context_dir}" --follow
}

# returns the exact digest just pushed, never the floating :latest tag:
# a deployment referencing :latest with imagePullPolicy IfNotPresent can
# silently keep serving an old cached image after a fresh push, a real
# bug hit and worked around by hand earlier this project, see
# PHASE1_PLAN.md. pinning to the digest here instead makes every apply
# of this script actually roll the pods it just built.
image_digest_ref() {
  local bc="$1" repo digest
  repo=$(oc get is "${bc}" -n "${RAG_NS}" -o jsonpath='{.status.dockerImageRepository}')
  digest=$(oc get is "${bc}" -n "${RAG_NS}" -o jsonpath='{.status.tags[0].items[0].image}')
  echo "${repo}@${digest}"
}

resolve_routes() {
  log "resolving this cluster's own route hostnames, never hardcoded"
  DSPA_ROUTE="https://$(oc get route ds-pipeline-rag-phase1-dspa -n "${RAG_NS}" -o jsonpath='{.spec.host}')"
  GUARDRAILS_ROUTE="https://$(oc get route rag-phase1-guardrails -n "${RAG_NS}" -o jsonpath='{.spec.host}')"
  MLFLOW_URL="$(oc get mlflow mlflow -n "${SHARED_NS}" -o jsonpath='{.status.url}')"
  MINIO_CONSOLE_URL="https://$(oc get route minio-console -n minio -o jsonpath='{.spec.host}')"
  RHOAI_DASHBOARD_URL="https://$(oc get route rhods-dashboard -n "${SHARED_NS}" -o jsonpath='{.spec.host}')"
  if [[ -z "${DSPA_ROUTE}" || "${DSPA_ROUTE}" == "https://" ]]; then
    echo "could not resolve the dspa route, run scripts/02-install-pipeline-server.sh first" >&2
    exit 1
  fi
  if [[ -z "${GUARDRAILS_ROUTE}" || "${GUARDRAILS_ROUTE}" == "https://" ]]; then
    echo "could not resolve the guardrails route, run scripts/10-install-guardrails.sh first" >&2
    exit 1
  fi
  if [[ -z "${MLFLOW_URL}" ]]; then
    echo "could not resolve mlflow's url, run scripts/04-install-mlflow.sh first" >&2
    exit 1
  fi
  if [[ -z "${MINIO_CONSOLE_URL}" || "${MINIO_CONSOLE_URL}" == "https://" ]]; then
    echo "could not resolve the minio console route, run scripts/01-install-minio.sh first" >&2
    exit 1
  fi
  if [[ -z "${RHOAI_DASHBOARD_URL}" || "${RHOAI_DASHBOARD_URL}" == "https://" ]]; then
    echo "could not resolve the rhods-dashboard route, is this a rhoai cluster?" >&2
    exit 1
  fi
  log "dspa route:       ${DSPA_ROUTE}"
  log "guardrails route: ${GUARDRAILS_ROUTE}"
  log "mlflow url:       ${MLFLOW_URL}"
  log "minio console:    ${MINIO_CONSOLE_URL}"
  log "rhoai dashboard:  ${RHOAI_DASHBOARD_URL}"
}

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }
  command -v helm >/dev/null 2>&1 || { echo "helm is required" >&2; exit 1; }

  if ! oc get secret minio-dsp-creds -n "${RAG_NS}" >/dev/null 2>&1; then
    echo "minio-dsp-creds not found in ${RAG_NS}, run ./scripts/01-install-minio.sh first" >&2
    exit 1
  fi

  build_image "${BACKEND_BC}" "${ROOT_DIR}/owner-console-backend"

  log "applying backend rbac and deployment"
  oc apply -f "${ROOT_DIR}/manifests/40-owner-console/00-backend-rbac.yaml"
  oc apply -f "${ROOT_DIR}/manifests/40-owner-console/01-backend.yaml"

  local backend_image
  backend_image="$(image_digest_ref "${BACKEND_BC}")"
  log "pinning ${BACKEND_DEPLOY} to the digest just pushed: ${backend_image}"
  oc set image "deployment/${BACKEND_DEPLOY}" -n "${RAG_NS}" "backend=${backend_image}"

  resolve_routes
  log "injecting the resolved routes into ${BACKEND_DEPLOY}, safe to rerun"
  oc set env "deployment/${BACKEND_DEPLOY}" -n "${RAG_NS}" \
    "MLFLOW_URL=${MLFLOW_URL}" \
    "DSPA_ROUTE=${DSPA_ROUTE}" \
    "GUARDRAILS_ROUTE=${GUARDRAILS_ROUTE}" \
    "MINIO_CONSOLE_URL=${MINIO_CONSOLE_URL}" \
    "RHOAI_DASHBOARD_URL=${RHOAI_DASHBOARD_URL}"

  # everything below used to be a python constant baked into server.py, see
  # its own module level defaults for the values this reproduces. setting
  # them explicitly here (rather than leaving it to those defaults) means
  # this script stays the one place an operator looks to retune a live
  # deployment, e.g. swapping AUTORAG_GENERATION_MODELS to bring
  # glm-53-flash back once it is confirmed live again in ogx's own
  # /v1/models, without touching server.py or rebuilding the image.
  log "injecting pipeline/experiment names and autorag knobs into ${BACKEND_DEPLOY}, safe to rerun"
  oc set env "deployment/${BACKEND_DEPLOY}" -n "${RAG_NS}" \
    "RAG_WORKSPACE=rag-phase1" \
    "INGESTION_EXPERIMENT_NAME=phase1-ingestion" \
    "INGESTION_PIPELINE_NAME=phase1-ingestion-pipeline" \
    "INGESTION_KFP_EXPERIMENT_NAME=phase1-ingestion" \
    "AUTORAG_PIPELINE_NAME=documents-rag-optimization-pipeline" \
    "AUTORAG_KFP_EXPERIMENT_NAME=phase1-autorag" \
    "APPLY_PATTERN_PIPELINE_NAME=phase1-apply-pattern-pipeline" \
    "APPLY_PATTERN_KFP_EXPERIMENT_NAME=phase1-apply-pattern" \
    "AUTORAG_RAW_BUCKET=rag-documents" \
    "AUTORAG_S3_SECRET_NAME=autorag-s3-creds" \
    "AUTORAG_OGX_SECRET_NAME=autorag-ogx-creds" \
    "AUTORAG_VECTOR_IO_PROVIDER_ID=pgvector" \
    "AUTORAG_EMBEDDING_MODELS=vllm-embedding/nomic-embed-text-v1.5" \
    "AUTORAG_GENERATION_MODELS=vllm-inference/Qwen3.6-35B-A3B,openai/publishers/prelude-maas/models/qwen38-27b" \
    "AUTORAG_MAX_RAG_PATTERNS=8" \
    "AUTORAG_PRESET=balanced" \
    "AUTORAG_OPTIMIZATION_METRIC=faithfulness" \
    "MINIO_ENDPOINT=http://minio.minio.svc.cluster.local:9000" \
    "ARTIFACT_BUCKET=dsp-pipeline-artifacts"
  wait_deploy_ready "${BACKEND_DEPLOY}" "${RAG_NS}"

  log "building the frontend plugin bundle"
  ( cd "${ROOT_DIR}/owner-console" && yarn build )
  build_image "${FRONTEND_BC}" "${ROOT_DIR}/owner-console"

  local frontend_image
  frontend_image="$(image_digest_ref "${FRONTEND_BC}")"
  log "deploying the console plugin chart with helm, image ${frontend_image}"
  helm upgrade --install "${HELM_RELEASE}" "${ROOT_DIR}/owner-console/charts/openshift-console-plugin" \
    -n "${RAG_NS}" \
    --set "plugin.image=${frontend_image}"

  cat <<EOF

owner console deployed. resolved for this cluster, nothing hardcoded:
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

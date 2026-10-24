#!/usr/bin/env bash
set -euo pipefail
# evalhub with ragas-v062, ibm-clear-v060, garak (not stock v0.5 providers).
# after 03 and 09; job submit is 11-submit-evalhub-jobs.sh.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

EVAL_DB_NAME="evalhub"
EVAL_DB_ROLE="evalhub_app"
RAGAS_BUCKET="evalhub-ragas-data"

# second database on the same running pgvector server
# (manifests/spear-data/01-pgvector.yaml), not a dedicated instance.
# raguser is that server's bootstrap superuser, same idiom as
# 03-install-data.sh's ensure_rag_reader_role, plus a real CREATE DATABASE
ensure_evalhub_database() {
  local password
  password="$(oc get secret evalhub-pg-creds -n "${EVAL_NS}" -o jsonpath='{.data.password}' | base64 -d)"
  oc exec deploy/pgvector -n "${DATA_NS}" -- \
    psql -U raguser -d postgres -v ON_ERROR_STOP=1 -c "
      DO \$\$
      BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${EVAL_DB_ROLE}') THEN
          CREATE ROLE ${EVAL_DB_ROLE} LOGIN;
        END IF;
      END
      \$\$;
      ALTER ROLE ${EVAL_DB_ROLE} PASSWORD '${password}';
    "
  if ! oc exec deploy/pgvector -n "${DATA_NS}" -- \
      psql -U raguser -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '${EVAL_DB_NAME}'" | grep -q 1; then
    oc exec deploy/pgvector -n "${DATA_NS}" -- \
      psql -U raguser -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE ${EVAL_DB_NAME} OWNER ${EVAL_DB_ROLE};"
  fi
  log "${EVAL_DB_NAME} database present on pgvector, owned by ${EVAL_DB_ROLE}"
}

# evalhub job pods download ragas test data from s3, expects
# AWS_ACCESS_KEY_ID style keys, different from the accesskey/secretkey
# shape minio-root-creds uses elsewhere; this is a re keyed copy, not a new minio user
ensure_ragas_bucket_and_s3_secret() {
  local user pass
  user=$(oc get secret minio-root-creds -n "${STORAGE_NS}" -o jsonpath='{.data.MINIO_ROOT_USER}' | base64 -d)
  pass=$(oc get secret minio-root-creds -n "${STORAGE_NS}" -o jsonpath='{.data.MINIO_ROOT_PASSWORD}' | base64 -d)

  ensure_secret evalhub-s3-credentials "${EVAL_NS}" \
    --from-literal=AWS_ACCESS_KEY_ID="${user}" \
    --from-literal=AWS_SECRET_ACCESS_KEY="${pass}" \
    --from-literal=AWS_DEFAULT_REGION=us-east-1 \
    --from-literal=AWS_S3_ENDPOINT="http://minio.${STORAGE_NS}.svc.cluster.local:9000"

  log "creating the ${RAGAS_BUCKET} bucket if it does not exist yet"
  oc delete job evalhub-bucket-bootstrap -n "${STORAGE_NS}" --ignore-not-found=true --wait=true
  cat <<EOF | oc apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: evalhub-bucket-bootstrap
  namespace: ${STORAGE_NS}
spec:
  backoffLimit: 3
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: mc
          image: quay.io/eformat/mc:RELEASE.2025-08-13T08-35-41Z
          env:
            - name: MC_CONFIG_DIR
              value: /tmp/.mc
          envFrom:
            - secretRef:
                name: minio-root-creds
          command:
            - sh
            - -c
            - |
              set -eu
              mc alias set local http://minio.${STORAGE_NS}.svc.cluster.local:9000 "\${MINIO_ROOT_USER}" "\${MINIO_ROOT_PASSWORD}"
              mc mb --ignore-existing local/${RAGAS_BUCKET}
EOF
  oc wait --for=condition=Complete job/evalhub-bucket-bootstrap -n "${STORAGE_NS}" --timeout=120s
}

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }
  command -v openssl >/dev/null 2>&1 || { echo "openssl is required" >&2; exit 1; }

  log "namespace, tenant label"
  oc apply -f "${ROOT_DIR}/manifests/spear-eval/00-namespace.yaml"

  log "evalhub's own postgres database, on the already running pgvector server in ${DATA_NS}"
  ensure_secret evalhub-pg-creds "${EVAL_NS}" \
    --from-literal=user="${EVAL_DB_ROLE}" \
    --from-literal=password="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
  ensure_evalhub_database
  ensure_secret evalhub-db-credentials "${EVAL_NS}" \
    --from-literal=db-url="postgres://${EVAL_DB_ROLE}:$(oc get secret evalhub-pg-creds -n "${EVAL_NS}" -o jsonpath='{.data.password}' | base64 -d)@pgvector.${DATA_NS}.svc.cluster.local:5432/${EVAL_DB_NAME}"

  log "s3 bucket and credentials for ragas's test data, on the already running minio in ${STORAGE_NS}"
  ensure_ragas_bucket_and_s3_secret

  # custom provider names avoid operator overwriting stock ragas/ibm-clear
  log "custom provider configmaps: ragas-v062, ibm-clear-v060, on the operator's own newer quay.io tags"
  oc apply -f "${ROOT_DIR}/manifests/spear-eval/03-custom-providers.yaml"

  # build spear-confidential-data image before evalhub cr reconciles
  log "spear-confidential-data provider: imagestream/buildconfig, build, provider configmap"
  oc apply -f "${ROOT_DIR}/manifests/spear-eval/04-confidential-data-provider-build.yaml"
  oc start-build spear-confidential-data-provider --from-dir="${ROOT_DIR}/evalhub" --wait -n "${EVAL_NS}"
  local image_digest
  image_digest="$(oc get istag spear-confidential-data-provider:latest -n "${EVAL_NS}" -o jsonpath='{.image.metadata.name}')"
  sed "s|IMAGE_DIGEST_PLACEHOLDER|${image_digest}|g" \
    "${ROOT_DIR}/manifests/spear-eval/05-confidential-data-provider-config.yaml" | oc apply -f -

  log "the real evalhub cr, providers: ragas-v062, ibm-clear-v060, garak"
  oc apply -f "${ROOT_DIR}/manifests/spear-eval/01-evalhub.yaml"
  # nudge cr if a prior reconcile failed before provider configmap existed
  nudge_reconcile evalhub evalhub "${EVAL_NS}"
  wait_deploy_ready evalhub "${EVAL_NS}"

  log "caller serviceaccount + rbac for submitting jobs"
  oc apply -f "${ROOT_DIR}/manifests/spear-eval/02-caller-rbac.yaml"

  # second tenant for spear-pipelines mlflow workspace (security trace experiment)
  # includes dummy model auth key for litellm client
  log "registering ${PIPELINES_NS} as a second evalhub tenant, where spear-shield-security-trace's own mlflow workspace actually lives, plus its dummy model auth secret"
  oc apply -f "${ROOT_DIR}/manifests/spear-pipelines/08-evalhub-tenant.yaml"

  log "evalhub ready"
  local route
  route="$(oc get route evalhub -n "${EVAL_NS}" -o jsonpath='{.spec.host}' 2>/dev/null || true)"
  cat <<EOF

evalhub url: https://${route:-pending}
providers: ragas-v062, ibm-clear-v060, garak
next step:
  ./scripts/11-submit-evalhub-jobs.sh
EOF
}

main "$@"

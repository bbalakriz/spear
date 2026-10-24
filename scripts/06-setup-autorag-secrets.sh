#!/usr/bin/env bash
set -euo pipefail
# autorag-s3-creds and autorag-ogx-creds in spear-pipelines for documents-rag-optimization-pipeline.
# s3 creds repackage minio-dsp-creds as AWS_* (base64 copy, never decoded here).
# ogx points at judge proxy; auth disabled on this ogx instance.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

log "copying minio-dsp-creds into autorag-s3-creds (AWS_* key names, values never decoded by this script)"
ACCESS_KEY_B64="$(oc get secret minio-dsp-creds -n "${PIPELINES_NS}" -o jsonpath='{.data.accesskey}')"
SECRET_KEY_B64="$(oc get secret minio-dsp-creds -n "${PIPELINES_NS}" -o jsonpath='{.data.secretkey}')"

cat <<EOF | oc apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: autorag-s3-creds
  namespace: ${PIPELINES_NS}
type: Opaque
data:
  AWS_ACCESS_KEY_ID: ${ACCESS_KEY_B64}
  AWS_SECRET_ACCESS_KEY: ${SECRET_KEY_B64}
stringData:
  AWS_S3_ENDPOINT: "http://minio.${STORAGE_NS}.svc.cluster.local:9000"
  AWS_DEFAULT_REGION: "us-east-1"
EOF

log "creating autorag-ogx-creds (spear-judge-proxy, not ogx direct)"
# judge proxy fixes autorag answer_relevance json_schema calls; see 03-judge-proxy.yaml
oc create secret generic autorag-ogx-creds -n "${PIPELINES_NS}" \
  --from-literal=OGX_CLIENT_BASE_URL="http://spear-judge-proxy.${INFERENCE_NS}.svc.cluster.local:8321" \
  --from-literal=OGX_CLIENT_API_KEY="not-required-auth-disabled-on-this-instance" \
  --dry-run=client -o yaml | oc apply -f -

log "done: autorag-s3-creds and autorag-ogx-creds ready in ${PIPELINES_NS}"

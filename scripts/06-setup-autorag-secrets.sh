#!/usr/bin/env bash
set -euo pipefail
# creates the two secrets the real, already-registered
# documents-rag-optimization-pipeline (AutoRAG's kfp pipeline, auto
# registered on every dspa pipeline server per official rhoai docs, see
# PHASE1_PLAN.md) needs to run: autorag-s3-creds (input/test data bucket
# access) and autorag-ogx-creds (points it at rag-phase1-ogx). lives in
# spear-pipelines, the dspa's own namespace, since this is what the
# pipeline run itself actually reads.
#
# note the exact key names below (OGX_CLIENT_API_KEY / OGX_CLIENT_BASE_URL)
# came from reading this pipeline's actual live registered input
# definitions on this cluster (`ogx_secret_name`'s description), not from
# the compiled ir pulled from github earlier, which described an older
# `llama_stack_secret_name` param this cluster's registered version no
# longer has.
#
# autorag-s3-creds reuses the same minio-dsp-creds access key pair this
# whole project already uses, just repackaged under the AWS_* key names
# that pipeline's secretAsEnv blocks expect. the copy happens entirely in
# base64 form, oc get returns the secret's data field already base64
# encoded and this script never decodes it, so the raw key material
# never passes through this script's own output or context, same as an
# ordinary `oc get secret -o yaml | oc apply -f -` secret copy.
#
# autorag-ogx-creds just needs rag-phase1-ogx's in cluster url. no real
# api key is needed: ogx's server.auth block only activates when
# AUTH_ISSUER is set (confirmed by reading /opt/app-root/config.yaml
# live), and this instance never sets it, so the placeholder value below
# is not a real secret, it is just satisfying a required field.

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

log "creating autorag-ogx-creds (points at rag-phase1-judge-proxy, not rag-phase1-ogx directly, as of 2026-09-27)"
# the judge proxy (manifests/spear-inference/03-judge-proxy.yaml) forwards
# every request byte for byte unchanged except the llm judge's own
# structured json_schema calls, which is where the answer_relevance
# metric was failing on every question, see that manifest's header
# comment and PHASE1_PLAN.md for the full root cause. no real auth
# enabled on either this instance or the proxy. cross namespace now,
# spear-pipelines calling into spear-inference, re verify the ogx
# networkpolicy still allows it, see manifests/spear-inference/01-ogxserver.yaml
oc create secret generic autorag-ogx-creds -n "${PIPELINES_NS}" \
  --from-literal=OGX_CLIENT_BASE_URL="http://rag-phase1-judge-proxy.${INFERENCE_NS}.svc.cluster.local:8321" \
  --from-literal=OGX_CLIENT_API_KEY="not-required-auth-disabled-on-this-instance" \
  --dry-run=client -o yaml | oc apply -f -

log "done: autorag-s3-creds and autorag-ogx-creds ready in ${PIPELINES_NS}"

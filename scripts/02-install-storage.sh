#!/usr/bin/env bash
set -euo pipefail
# minio, buckets, seed corpus, minio-dsp-creds in spear-pipelines.
# rerun wipes raw-intake/ then re seeds from seed-data/. after 01-install-namespaces.sh

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

DOC_BUCKET="rag-documents"
DSP_BUCKET="dsp-pipeline-artifacts"

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }

  log "pvc, deployment, service, console route"
  ensure_secret minio-root-creds "${STORAGE_NS}" \
    --from-literal=MINIO_ROOT_USER=raguser \
    --from-literal=MINIO_ROOT_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
  oc apply -f "${ROOT_DIR}/manifests/minio/01-minio.yaml"
  wait_deploy_ready minio "${STORAGE_NS}"

  log "syncing minio credentials into ${PIPELINES_NS} as minio-dsp-creds, for the pipeline server"
  local user pass
  user=$(oc get secret minio-root-creds -n "${STORAGE_NS}" -o jsonpath='{.data.MINIO_ROOT_USER}' | base64 -d)
  pass=$(oc get secret minio-root-creds -n "${STORAGE_NS}" -o jsonpath='{.data.MINIO_ROOT_PASSWORD}' | base64 -d)
  oc get namespace "${PIPELINES_NS}" >/dev/null 2>&1 || oc create namespace "${PIPELINES_NS}"
  ensure_secret minio-dsp-creds "${PIPELINES_NS}" \
    --from-literal=accesskey="${user}" \
    --from-literal=secretkey="${pass}"

  log "seeding the sample document corpus into a configmap for the mc job to mount"
  oc create configmap spear-seed-docs -n "${STORAGE_NS}" \
    --from-file="${ROOT_DIR}/seed-data/raw-intake/" \
    --dry-run=client -o yaml | oc apply -f -

  log "running the mc job: create buckets, upload the seed corpus under raw-intake/"
  oc delete job minio-bootstrap -n "${STORAGE_NS}" --ignore-not-found=true --wait=true
  cat <<EOF | oc apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: minio-bootstrap
  namespace: ${STORAGE_NS}
spec:
  backoffLimit: 3
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: mc
          # public mirror; upstream minio/mc image is gated
          image: quay.io/eformat/mc:RELEASE.2025-08-13T08-35-41Z
          env:
            # restricted scc: HOME is /; mc needs a writable config dir
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
              mc mb --ignore-existing local/${DOC_BUCKET}
              mc mb --ignore-existing local/${DSP_BUCKET}
              # wipe stale objects before re seed
              mc rm --recursive --force --dangerous local/${DOC_BUCKET}/raw-intake/ || true
              # exclude configmap ..data/ symlinks or files upload twice
              mc mirror --overwrite --exclude "..*/**" /seed/ local/${DOC_BUCKET}/raw-intake/
              mc ls --recursive local/${DOC_BUCKET}
          volumeMounts:
            - name: seed
              mountPath: /seed
      volumes:
        - name: seed
          configMap:
            name: spear-seed-docs
EOF
  oc wait --for=condition=Complete job/minio-bootstrap -n "${STORAGE_NS}" --timeout=180s

  log "minio ready"
  cat <<EOF

minio console: https://$(oc get route minio-console -n "${STORAGE_NS}" -o jsonpath='{.spec.host}' 2>/dev/null || echo 'pending')/
buckets: ${DOC_BUCKET} (raw-intake/ seeded with $(oc get configmap spear-seed-docs -n "${STORAGE_NS}" -o jsonpath='{.data}' | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))') files), ${DSP_BUCKET} (empty, for the pipeline server)
next step:
  ./scripts/03-install-data.sh
EOF
}

main "$@"

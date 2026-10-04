#!/usr/bin/env bash
set -euo pipefail
# installs the standalone minio instance from manifests/minio/01-minio.yaml,
# creates the two buckets this project needs (rag-documents for the document
# corpus, dsp-pipeline-artifacts for the pipeline server from
# 04-install-pipelines.sh), seeds rag-documents/raw-intake/ with the sample
# corpus and its abac manifest from seed-data/raw-intake/, and syncs a copy
# of the root credentials into spear-pipelines as minio-dsp-creds for the
# pipeline server to consume. safe to rerun: the root secret and the
# buckets are only ever created once, and the seed upload wipes
# raw-intake/ before copying, so a rerun always leaves it matching
# seed-data/raw-intake/ exactly, never a mix of old and new files.
# seed-data/ is the one and only intake batch this project seeds
# anywhere, there is deliberately no second, script embedded corpus.
#
# NAMESPACE_MIGRATION_PLAN.md: minio-dsp-creds now syncs into
# spear-pipelines, not rag-phase1, since that is where the dspa pipeline
# server now lives. run after 01-install-namespaces.sh, which already
# creates the minio namespace itself

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
          # same story as the server image, minio/mc on quay.io/docker.io
          # is fully gated now, quay.io/eformat mirrors the real upstream
          # build and stays public.
          image: quay.io/eformat/mc:RELEASE.2025-08-13T08-35-41Z
          env:
            # mc writes its config under \$MC_CONFIG_DIR, defaults under
            # \$HOME which is unwritable here: this cluster's restricted
            # scc runs the container as an arbitrary uid with no matching
            # /etc/passwd entry, so HOME resolves to / and mc fails with
            # "mkdir /.mc: permission denied", confirmed live 2026-09-28
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
              # wipe raw-intake/ first: seed-data/ is the single source of
              # truth for this batch, an old rerun's stale files (or an
              # old, now retired seeder script's files) must not silently
              # linger alongside whatever is seeded today, confirmed live
              # 2026-09-28 that a plain cp on top never cleaned these up
              mc rm --recursive --force --dangerous local/${DOC_BUCKET}/raw-intake/ || true
              # kubernetes configmap volumes mount their real files under
              # a hidden ..<timestamp>/ directory, then symlink the flat
              # names to it, this is the atomic update mechanism, not a
              # bug. confirmed live 2026-09-28: a plain recursive
              # cp/mirror over /seed/ walks into that hidden directory
              # too and uploads every file twice, once at its real flat
              # path, once again nested under ..<timestamp>/. excluding
              # it here is what actually fixes that, not switching
              # cp -> mirror on its own.
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

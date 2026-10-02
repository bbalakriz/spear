#!/usr/bin/env bash
set -uo pipefail
# read only health check across everything the 00/01/02/10 scripts install.
# doesn't set -e on purpose: it keeps checking every component and prints a
# pass/fail summary at the end, rather than stopping at the first failure.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

RAG_NS="rag-phase1"
MINIO_NS="minio"
FAILURES=0

check() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf '  [ok]   %s\n' "${desc}"
  else
    printf '  [FAIL] %s\n' "${desc}"
    FAILURES=$((FAILURES + 1))
  fi
}

main() {
  log "namespaces"
  check "rag-phase1 namespace exists" oc get namespace "${RAG_NS}"
  check "minio namespace exists" oc get namespace "${MINIO_NS}"

  log "odh dashboard config"
  check "genAiStudio flag is true" bash -c \
    "[[ \"\$(oc get odhdashboardconfig odh-dashboard-config -n redhat-ods-applications -o jsonpath='{.spec.dashboardConfig.genAiStudio}')\" == 'true' ]]"
  check "autorag flag is true" bash -c \
    "[[ \"\$(oc get odhdashboardconfig odh-dashboard-config -n redhat-ods-applications -o jsonpath='{.spec.dashboardConfig.autorag}')\" == 'true' ]]"

  log "pgvector"
  check "pgvector deployment available" oc wait --for=condition=Available deployment/pgvector -n "${RAG_NS}" --timeout=5s
  check "vector extension enabled on ragdb" oc exec deploy/pgvector -n "${RAG_NS}" -- \
    psql -U raguser -d ragdb -tAc "select 1 from pg_extension where extname='vector'"

  log "minio"
  check "minio deployment available" oc wait --for=condition=Available deployment/minio -n "${MINIO_NS}" --timeout=5s
  check "minio-dsp-creds synced into rag-phase1" oc get secret minio-dsp-creds -n "${RAG_NS}"
  check "seed corpus configmap present" oc get configmap rag-phase1-seed-docs -n "${MINIO_NS}"

  log "pipeline server"
  check "dspa reports Ready" bash -c \
    "[[ \"\$(oc get dspa rag-phase1-dspa -n ${RAG_NS} -o jsonpath='{.status.conditions[?(@.type==\"Ready\")].status}')\" == 'True' ]]"

  log "guardrails"
  check "nemoguardrails reports Ready" bash -c \
    "[[ \"\$(oc get nemoguardrails rag-phase1-guardrails -n ${RAG_NS} -o jsonpath='{.status.phase}')\" == 'Ready' ]]"
  check "guardrails route exists" oc get route rag-phase1-guardrails -n "${RAG_NS}"

  log "ogx"
  check "ogxserver reports Ready" bash -c \
    "[[ \"\$(oc get ogxserver rag-phase1-ogx -n ${RAG_NS} -o jsonpath='{.status.phase}')\" == 'Ready' ]]"
  check "ogxserver service responds healthy" oc run ogx-verify-probe --rm -i --restart=Never \
    --image=registry.access.redhat.com/ubi9/ubi-minimal:latest -n "${RAG_NS}" --command -- \
    bash -c "curl -sf http://rag-phase1-ogx-service.${RAG_NS}.svc.cluster.local:8321/v1/health | grep -q '\"OK\"'"
  # the vllm foundation model and the embedding model, wired via the
  # image's own generic env var driven remote::vllm slots, see
  # manifests/30-ogx/01-ogxserver.yaml, only meaningful once
  # scripts/03-install-ogx.sh has been run.
  check "ogx lists the vllm foundation model plus the embedding model" oc run ogx-models-verify-probe --rm -i \
    --restart=Never --image=registry.access.redhat.com/ubi9/ubi-minimal:latest -n "${RAG_NS}" --command -- \
    bash -c "out=\$(curl -sf http://rag-phase1-ogx-service.${RAG_NS}.svc.cluster.local:8321/v1/models); \
      echo \"\${out}\" | grep -q vllm-inference/ && echo \"\${out}\" | grep -q nomic-embed"
  # a real failed autorag run on 2026-09-28 (see PHASE1_PLAN.md) proved
  # this is the check that actually matters for run_autorag.py's own
  # GENERATION_MODELS: ai4rag's search space preparation step only ever
  # checks /v1/models presence, it never calls /v1/chat/completions
  # itself, so a model missing from this listing fails every autorag run
  # with a SearchSpaceValueError regardless of whether it would have
  # routed a real completion correctly. check exactly the models
  # run_autorag.py configures, not a stand in.
  check "ogx registers both autorag generation models" oc run ogx-models-registered-verify-probe --rm -i \
    --restart=Never --image=registry.access.redhat.com/ubi9/ubi-minimal:latest -n "${RAG_NS}" --command -- \
    bash -c "out=\$(curl -sf http://rag-phase1-ogx-service.${RAG_NS}.svc.cluster.local:8321/v1/models); \
      echo \"\${out}\" | grep -q 'vllm-inference/Qwen3.6-35B-A3B' && \
      echo \"\${out}\" | grep -q 'openai/publishers/prelude-maas/models/qwen38-27b'"
  # glm-53-flash genuinely does still route a real completion, confirmed
  # both on 2026-09-27 and again while diagnosing the failure above. it is
  # also still present and ready on the upstream maas gateway's own
  # /v1/models right now, confirmed live 2026-09-28, so it was never truly
  # deregistered anywhere: ogx's own /v1/models is just missing it because
  # ogx only discovers the upstream's model list once, at pod startup, and
  # this pod's last boot got an incomplete response (9 of 10 models). a
  # restart would very likely fix it, see PHASE1_PLAN.md. kept as its own,
  # honestly labeled check so a future listing only regression here does
  # not get confused with an autorag capable model going down, they are
  # not the same signal.
  check "ogx still routes a real chat completion through openai/glm-53-flash, even though ogx's own /v1/models is missing it" \
    oc run ogx-chat-verify-probe --rm -i \
    --restart=Never --image=registry.access.redhat.com/ubi9/ubi-minimal:latest -n "${RAG_NS}" --command -- \
    bash -c "curl -sf -X POST http://rag-phase1-ogx-service.${RAG_NS}.svc.cluster.local:8321/v1/chat/completions \
      -H 'Content-Type: application/json' \
      -d '{\"model\":\"openai/publishers/prelude-maas/models/glm-53-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":10}' \
      | grep -q chatcmpl"

  log "mlflow"
  check "mlflow reports Available" bash -c \
    "[[ \"\$(oc get mlflow mlflow -n redhat-ods-applications -o jsonpath='{.status.conditions[?(@.type==\"Available\")].status}')\" == 'True' ]]"
  # runs as the same pipeline-runner sa the ingestion pipeline uses, mlflow
  # does its own SubjectAccessReview per request (see manifests/00-platform/
  # 07-mlflow-pipeline-rbac.yaml), so this doubles as an rbac check too.
  check "mlflow api reachable with the rag-phase1 workspace header" oc run mlflow-verify-probe --rm -i \
    --restart=Never --image=registry.access.redhat.com/ubi9/ubi-minimal:latest -n "${RAG_NS}" \
    --overrides="{\"spec\":{\"serviceAccountName\":\"pipeline-runner-rag-phase1-dspa\"}}" --command -- \
    bash -c "curl -sfk -H \"Authorization: Bearer \$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)\" \
      -H 'X-MLflow-Workspace: rag-phase1' -X POST -H 'Content-Type: application/json' -d '{\"max_results\":50}' \
      https://mlflow.redhat-ods-applications.svc:8443/mlflow/api/2.0/mlflow/experiments/search | grep -q phase1-ingestion"

  log "phase1-ingestion-pipeline"
  # rag_chunks is deliberately left empty for a batch until a winning
  # autorag pattern is applied to it, see PHASE1_PLAN.md's sequencing
  # writeup, 2026-09-28: ingestion itself no longer writes to this table
  # at all, that only happens once pipelines/phase1_apply_pattern/
  # pipeline.py runs, so a row count check here would be meaningless on a
  # freshly ingested batch that has not had a pattern applied yet.
  check "rag_chunks table exists (created lazily by either pipeline on first write)" \
    oc exec deploy/pgvector -n "${RAG_NS}" -- \
    psql -U raguser -d ragdb -tAc "select 1 from information_schema.tables where table_name='rag_chunks'"
  # written by pipelines/phase1_ingestion/pipeline.py's generate-eval-dataset
  # step, a real sdg hub run against the sanitized corpus, see
  # PHASE1_PLAN.md's "SDG Hub eval dataset generation and real AutoRAG
  # trigger" section for the run that produced this.
  check "sdg hub generated a real eval dataset with at least one qa pair" oc run eval-dataset-verify-probe \
    --rm -i --restart=Never --image=registry.access.redhat.com/ubi9/python-312:latest -n "${RAG_NS}" \
    --overrides="{\"spec\":{\"containers\":[{\"name\":\"eval-dataset-verify-probe\",\"image\":\"registry.access.redhat.com/ubi9/python-312:latest\",\"stdin\":true,\"env\":[{\"name\":\"MINIO_ACCESS_KEY\",\"valueFrom\":{\"secretKeyRef\":{\"name\":\"minio-dsp-creds\",\"key\":\"accesskey\"}}},{\"name\":\"MINIO_SECRET_KEY\",\"valueFrom\":{\"secretKeyRef\":{\"name\":\"minio-dsp-creds\",\"key\":\"secretkey\"}}}]}]}}" \
    --command -- bash -c \
    "pip install --quiet boto3 >/dev/null 2>&1 && python3 -c \"
import boto3, os
s3 = boto3.client('s3', endpoint_url='http://minio.minio.svc.cluster.local:9000',
                  aws_access_key_id=os.environ['MINIO_ACCESS_KEY'],
                  aws_secret_access_key=os.environ['MINIO_SECRET_KEY'])
body = s3.get_object(Bucket='rag-documents', Key='autorag-eval/seed-batch-001_test_data.json')['Body'].read()
import json
rows = json.loads(body)
assert len(rows) > 0 and 'question' in rows[0] and 'correct_answer_document_ids' in rows[0]
\""

  log "autorag trigger"
  check "autorag-s3-creds and autorag-ogx-creds exist" bash -c \
    "oc get secret autorag-s3-creds -n ${RAG_NS} >/dev/null && oc get secret autorag-ogx-creds -n ${RAG_NS} >/dev/null"
  # confirms the real, rhoai auto registered documents-rag-optimization-
  # pipeline is present on this dspa, the same lookup
  # pipelines/phase1_autorag/run_autorag.py does before triggering a run,
  # using the caller's own oc whoami -t token, no separate credential.
  # the dspa route hostname is resolved live here too, never hardcoded,
  # it is unique per cluster
  local dspa_route
  dspa_route="https://$(oc get route ds-pipeline-rag-phase1-dspa -n "${RAG_NS}" -o jsonpath='{.spec.host}' 2>/dev/null)"
  check "documents-rag-optimization-pipeline is registered on rag-phase1-dspa" bash -c \
    "curl -sk -H \"Authorization: Bearer \$(oc whoami -t)\" \
      '${dspa_route}/apis/v2beta1/pipelines?filter=%7B%22predicates%22%3A%5B%7B%22key%22%3A%22name%22%2C%22operation%22%3A%22EQUALS%22%2C%22stringValue%22%3A%22documents-rag-optimization-pipeline%22%7D%5D%7D' \
      | grep -q documents-rag-optimization-pipeline"

  log "phase1-apply-pattern-pipeline"
  # applies a winning autorag pattern to the production index, see
  # pipelines/phase1_apply_pattern/, registered manually the same way
  # phase1-ingestion-pipeline is (compile_and_run.py), not auto
  # registered like autorag's own pipeline is.
  check "phase1-apply-pattern-pipeline is registered on rag-phase1-dspa" bash -c \
    "curl -sk -H \"Authorization: Bearer \$(oc whoami -t)\" \
      '${dspa_route}/apis/v2beta1/pipelines?filter=%7B%22predicates%22%3A%5B%7B%22key%22%3A%22name%22%2C%22operation%22%3A%22EQUALS%22%2C%22stringValue%22%3A%22phase1-apply-pattern-pipeline%22%7D%5D%7D' \
      | grep -q phase1-apply-pattern-pipeline"

  echo
  if (( FAILURES == 0 )); then
    log "all checks passed"
  else
    log "${FAILURES} check(s) failed, see [FAIL] lines above"
    exit 1
  fi
}

main "$@"

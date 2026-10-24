#!/usr/bin/env bash
set -euo pipefail
# manual one shot evalhub jobs: ragas, ibm-clear, garak, spear-confidential-data.
# judge model gemma4 (glm-53-flash times out on ragas structured output).
# ragas metrics are reference free only; no golden answers for the rest.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

JUDGE_MODEL="openai/publishers/prelude-maas/models/gemma4"
EMBEDDING_MODEL="vllm-embedding/nomic-embed-text-v1.5"
OGX_URL="http://spear-ogx-service.spear-inference.svc.cluster.local:8321/v1"
# coordinator generation model (garak probes ogx directly, not the agent)
GARAK_TARGET_MODEL="openai/publishers/prelude-maas/models/glm-53-flash"
MLFLOW_EXPERIMENT="spear-shield-security-trace"
MLFLOW_INTERNAL_URL="https://mlflow.redhat-ods-applications.svc:8443/mlflow"
# the one real experiment every evalhub job result lands in, native saves
# (ibm-clear) and this project's own log_eval_to_mlflow.py (ragas) alike,
# not a throwaway name
MLFLOW_EVAL_RESULTS_EXPERIMENT="spear-shield-evalhub-results"

evalhub_url() {
  echo "https://$(oc get route evalhub -n "${EVAL_NS}" -o jsonpath='{.spec.host}')"
}

caller_token() {
  oc create token spear-eval-caller -n "${EVAL_NS}" --duration=2h
}

# export mlflow traces to minio for ragas (job in spear-agents; mirror minio creds)
export_and_upload_ragas_dataset() {
  log "exporting a fresh ragas dataset from real ${MLFLOW_EXPERIMENT} traces straight into minio"
  local job_ns="spear-agents"
  local token
  token="$(oc create token spear-coordinator-mlflow-caller -n "${job_ns}" --duration=30m)"

  mirror_secret minio-root-creds "${STORAGE_NS}" "${job_ns}"

  # populated once by the service ca operator on the inject-cabundle
  # annotation below, never needs touching again after that
  if ! oc get configmap evalhub-mlflow-ca -n "${job_ns}" >/dev/null 2>&1; then
    oc create configmap evalhub-mlflow-ca -n "${job_ns}"
    oc annotate configmap evalhub-mlflow-ca -n "${job_ns}" service.beta.openshift.io/inject-cabundle=true --overwrite
  fi

  oc create configmap evalhub-ragas-export-script -n "${job_ns}" \
    --from-file=export_ragas_dataset.py="${ROOT_DIR}/scripts/lib/export_ragas_dataset.py" \
    --dry-run=client -o yaml | oc apply -f -

  oc delete job evalhub-ragas-dataset-export -n "${job_ns}" --ignore-not-found --wait=true
  cat <<EOF | oc apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: evalhub-ragas-dataset-export
  namespace: ${job_ns}
spec:
  backoffLimit: 2
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      initContainers:
        - name: export
          # the coordinator agent's own image, it already carries
          # mlflow-skinny, this step needs nothing else from it, a plain
          # mlflow query is not agent specific
          image: image-registry.openshift-image-registry.svc:5000/spear-agents/spear-coordinator-agent:latest
          command: ["python3", "/scripts/export_ragas_dataset.py"]
          env:
            - name: MLFLOW_URL
              value: "${MLFLOW_INTERNAL_URL}"
            - name: MLFLOW_TRACKING_TOKEN
              value: "${token}"
            - name: MLFLOW_WORKSPACE
              value: "${PIPELINES_NS}"
            - name: MLFLOW_EXPERIMENT
              value: "${MLFLOW_EXPERIMENT}"
            - name: OUTPUT_PATH
              value: /data/ragas_dataset.jsonl
            - name: SSL_CERT_FILE
              value: /etc/mlflow-ca/service-ca.crt
            - name: REQUESTS_CA_BUNDLE
              value: /etc/mlflow-ca/service-ca.crt
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: dataset
              mountPath: /data
            - name: mlflow-ca
              mountPath: /etc/mlflow-ca
            - name: export-script
              mountPath: /scripts
      containers:
        - name: mc
          image: quay.io/eformat/mc:RELEASE.2025-08-13T08-35-41Z
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          envFrom:
            - secretRef:
                name: minio-root-creds
          env:
            - name: HOME
              value: /tmp/mchome
          command:
            - /bin/sh
            - -c
            - |
              mkdir -p /tmp/mchome
              mc alias set local http://minio.${STORAGE_NS}.svc.cluster.local:9000 "\$MINIO_ROOT_USER" "\$MINIO_ROOT_PASSWORD"
              mc cp /data/ragas_dataset.jsonl local/evalhub-ragas-data/ragas_dataset.jsonl
          volumeMounts:
            - name: dataset
              mountPath: /data
            - name: mchome
              mountPath: /tmp/mchome
      volumes:
        - name: dataset
          emptyDir: {}
        - name: mlflow-ca
          configMap:
            name: evalhub-mlflow-ca
        - name: export-script
          configMap:
            name: evalhub-ragas-export-script
        - name: mchome
          emptyDir: {}
EOF
  oc wait --for=condition=Complete job/evalhub-ragas-dataset-export -n "${job_ns}" --timeout=120s
}

submit_ragas_job() {
  local body
  body="$(cat <<EOF
{
  "name": "spear-shield-ragas-rag-eval",
  "model": {
    "url": "${OGX_URL}",
    "name": "${JUDGE_MODEL}"
  },
  "benchmarks": [
    {
      "id": "ragas_rag_default",
      "provider_id": "ragas-v062",
      "parameters": {
        "metrics": ["faithfulness", "answer_relevancy", "context_relevance", "response_groundedness"],
        "max_tokens": 4096,
        "embedding_url": "${OGX_URL}",
        "embedding_model": "${EMBEDDING_MODEL}"
      },
      "test_data_ref": {
        "s3": {
          "bucket": "evalhub-ragas-data",
          "key": "ragas_dataset.jsonl",
          "secret_ref": "evalhub-s3-credentials"
        }
      }
    }
  ]
}
EOF
)"
  log "submitting ragas job (x-tenant ${EVAL_NS})"
  curl -sk -X POST \
    -H "Authorization: Bearer $(caller_token)" \
    -H "X-Tenant: ${EVAL_NS}" \
    -H "Content-Type: application/json" \
    --data "${body}" \
    "$(evalhub_url)/api/v1/evaluations/jobs"
}

submit_ibm_clear_job() {
  local body
  body="$(cat <<EOF
{
  "name": "spear-shield-ibm-clear-agentic-eval",
  "model": {
    "url": "${OGX_URL}",
    "name": "${JUDGE_MODEL}",
    "auth": {"secret_ref": "ogx-dummy-model-auth"}
  },
  "benchmarks": [
    {
      "id": "agentic-evaluation",
      "provider_id": "ibm-clear-v060",
      "benchmark_id": "agentic-evaluation",
      "parameters": {
        "eval_model_name": "${JUDGE_MODEL}",
        "provider": "openai",
        "inference_backend": "litellm",
        "agent_framework": "langgraph",
        "observability_framework": "mlflow",
        "mlflow_traces_experiment_name": "${MLFLOW_EXPERIMENT}",
        "mlflow_traces_max_results": 50,
        "mlflow_experiment_name": "${MLFLOW_EVAL_RESULTS_EXPERIMENT}"
      }
    }
  ]
}
EOF
)"
  # ibm-clear reads mlflow_experiment_name from parameters; x-tenant spear-pipelines
  log "submitting ibm-clear job (x-tenant ${PIPELINES_NS})"
  curl -sk -X POST \
    -H "Authorization: Bearer $(caller_token)" \
    -H "X-Tenant: ${PIPELINES_NS}" \
    -H "Content-Type: application/json" \
    --data "${body}" \
    "$(evalhub_url)/api/v1/evaluations/jobs"
}

# garak quick profile; mlflow logging via log_result_to_mlflow (x-tenant spear-eval)
submit_garak_job() {
  local body
  body="$(cat <<EOF
{
  "name": "spear-shield-garak-quick-scan",
  "model": {
    "url": "${OGX_URL}",
    "name": "${GARAK_TARGET_MODEL}"
  },
  "benchmarks": [
    {
      "id": "quick",
      "provider_id": "garak",
      "parameters": {}
    }
  ]
}
EOF
)"
  log "submitting garak job (x-tenant ${EVAL_NS})"
  curl -sk -X POST \
    -H "Authorization: Bearer $(caller_token)" \
    -H "X-Tenant: ${EVAL_NS}" \
    -H "Content-Type: application/json" \
    --data "${body}" \
    "$(evalhub_url)/api/v1/evaluations/jobs"
}

# scans mlflow traces for confidential data leaks; mlflow creds via benchmark parameters
submit_confidential_data_job() {
  local job_ns="spear-agents"
  local mlflow_token mlflow_ca_cert body

  mlflow_token="$(oc create token spear-coordinator-mlflow-caller -n "${job_ns}" --duration=30m)"

  if ! oc get configmap evalhub-mlflow-ca -n "${job_ns}" >/dev/null 2>&1; then
    oc create configmap evalhub-mlflow-ca -n "${job_ns}"
    oc annotate configmap evalhub-mlflow-ca -n "${job_ns}" service.beta.openshift.io/inject-cabundle=true --overwrite
    # first creation needs a moment for the service ca operator to fill it in
    sleep 5
  fi
  mlflow_ca_cert="$(oc get configmap evalhub-mlflow-ca -n "${job_ns}" -o jsonpath='{.data.service-ca\.crt}')"

  # python builds json so ca cert newlines escape correctly
  body="$(python3 - "${OGX_URL}" "${JUDGE_MODEL}" "${MLFLOW_INTERNAL_URL}" "${mlflow_token}" "${mlflow_ca_cert}" "${PIPELINES_NS}" "${MLFLOW_EXPERIMENT}" <<'PYEOF'
import json
import sys

ogx_url, judge_model, mlflow_url, mlflow_token, mlflow_ca_cert, workspace, experiment = sys.argv[1:8]
body = {
    "name": "spear-shield-confidential-data-leak-scan",
    "model": {"url": ogx_url, "name": judge_model},
    "benchmarks": [
        {
            "id": "confidential_data_leak_scan",
            "provider_id": "spear-confidential-data",
            "parameters": {
                "mlflow_url": mlflow_url,
                "mlflow_token": mlflow_token,
                "mlflow_ca_cert": mlflow_ca_cert,
                "mlflow_workspace": workspace,
                "mlflow_experiment": experiment,
                "max_traces": 50,
            },
        }
    ],
}
print(json.dumps(body))
PYEOF
)"

  log "submitting confidential data leak scan job (x-tenant ${EVAL_NS})"
  curl -sk -X POST \
    -H "Authorization: Bearer $(caller_token)" \
    -H "X-Tenant: ${EVAL_NS}" \
    -H "Content-Type: application/json" \
    --data "${body}" \
    "$(evalhub_url)/api/v1/evaluations/jobs"
}

# poll until completed/failed (ragas/ibm-clear can exceed 90s; use 900s budget)
wait_for_job() {
  local tenant="$1" job_id="$2"
  local deadline=$((SECONDS + 900))
  local state="" body=""
  while [ "${SECONDS}" -lt "${deadline}" ]; do
    body="$(curl -sk -H "Authorization: Bearer $(caller_token)" -H "X-Tenant: ${tenant}" \
      "$(evalhub_url)/api/v1/evaluations/jobs/${job_id}")"
    state="$(echo "${body}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",{}).get("state",""))' 2>/dev/null)"
    if [ "${state}" = "completed" ] || [ "${state}" = "failed" ]; then
      echo "${body}"
      return 0
    fi
    sleep 5
  done
  log "job ${job_id} did not reach a terminal state within 900s, last seen state: ${state}"
  echo "${body}"
}

# one parent run per script invocation, every real benchmark run below
# gets tagged mlflow.parentRunId against it, so mlflow's own run list
# groups ragas, ibm-clear and later garak as one evaluation cycle instead
# of unrelated flat rows, printed run id read back off this pod's own logs,
# same plain pod shape as every other step here
create_eval_cycle_run() {
  local job_ns="spear-agents"
  local token
  token="$(oc create token spear-coordinator-mlflow-caller -n "${job_ns}" --duration=10m)"

  if ! oc get configmap evalhub-mlflow-ca -n "${job_ns}" >/dev/null 2>&1; then
    oc create configmap evalhub-mlflow-ca -n "${job_ns}"
    oc annotate configmap evalhub-mlflow-ca -n "${job_ns}" service.beta.openshift.io/inject-cabundle=true --overwrite
  fi

  oc create configmap evalhub-cycle-run-script -n "${job_ns}" \
    --from-file=create_eval_cycle_run.py="${ROOT_DIR}/scripts/lib/create_eval_cycle_run.py" \
    --dry-run=client -o yaml | oc apply -f - >&2

  oc delete job evalhub-cycle-run -n "${job_ns}" --ignore-not-found --wait=true >&2
  cat <<EOF | oc apply -f - >&2
apiVersion: batch/v1
kind: Job
metadata:
  name: evalhub-cycle-run
  namespace: ${job_ns}
spec:
  backoffLimit: 2
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: cycle-run
          image: image-registry.openshift-image-registry.svc:5000/spear-agents/spear-coordinator-agent:latest
          command: ["python3", "/scripts/create_eval_cycle_run.py"]
          env:
            - name: MLFLOW_URL
              value: "${MLFLOW_INTERNAL_URL}"
            - name: MLFLOW_TRACKING_TOKEN
              value: "${token}"
            - name: MLFLOW_WORKSPACE
              value: "${PIPELINES_NS}"
            - name: MLFLOW_EVAL_EXPERIMENT
              value: "${MLFLOW_EVAL_RESULTS_EXPERIMENT}"
            - name: SSL_CERT_FILE
              value: /etc/mlflow-ca/service-ca.crt
            - name: REQUESTS_CA_BUNDLE
              value: /etc/mlflow-ca/service-ca.crt
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: mlflow-ca
              mountPath: /etc/mlflow-ca
            - name: script
              mountPath: /scripts
      volumes:
        - name: mlflow-ca
          configMap:
            name: evalhub-mlflow-ca
        - name: script
          configMap:
            name: evalhub-cycle-run-script
EOF
  oc wait --for=condition=Complete job/evalhub-cycle-run -n "${job_ns}" --timeout=60s >&2
  local pod
  pod="$(oc get pods -n "${job_ns}" -l job-name=evalhub-cycle-run -o jsonpath='{.items[0].metadata.name}')"
  # mlflow's own client prints its "View run"/"View experiment" lines
  # after this script's own output, so the real run id is not reliably
  # the last line, grep the fixed marker instead
  oc logs "${pod}" -n "${job_ns}" | grep '^RUN_ID=' | sed 's/^RUN_ID=//'
}

# log evalhub job json to mlflow when provider has no native save path
log_result_to_mlflow() {
  local result_json="$1"
  local parent_run_id="${2:-}"
  local job_ns="spear-agents"
  local token
  token="$(oc create token spear-coordinator-mlflow-caller -n "${job_ns}" --duration=10m)"

  if ! oc get configmap evalhub-mlflow-ca -n "${job_ns}" >/dev/null 2>&1; then
    oc create configmap evalhub-mlflow-ca -n "${job_ns}"
    oc annotate configmap evalhub-mlflow-ca -n "${job_ns}" service.beta.openshift.io/inject-cabundle=true --overwrite
  fi

  echo "${result_json}" > /tmp/evalhub_result.json
  oc create configmap evalhub-result -n "${job_ns}" --from-file=result.json=/tmp/evalhub_result.json \
    --dry-run=client -o yaml | oc apply -f -
  oc create configmap evalhub-mlflow-logger-script -n "${job_ns}" \
    --from-file=log_eval_to_mlflow.py="${ROOT_DIR}/scripts/lib/log_eval_to_mlflow.py" \
    --dry-run=client -o yaml | oc apply -f -

  oc delete job evalhub-mlflow-log -n "${job_ns}" --ignore-not-found --wait=true
  cat <<EOF | oc apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: evalhub-mlflow-log
  namespace: ${job_ns}
spec:
  backoffLimit: 2
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: log
          image: image-registry.openshift-image-registry.svc:5000/spear-agents/spear-coordinator-agent:latest
          command: ["python3", "/scripts/log_eval_to_mlflow.py"]
          env:
            - name: MLFLOW_URL
              value: "${MLFLOW_INTERNAL_URL}"
            - name: MLFLOW_TRACKING_TOKEN
              value: "${token}"
            - name: MLFLOW_WORKSPACE
              value: "${PIPELINES_NS}"
            - name: MLFLOW_EVAL_EXPERIMENT
              value: "${MLFLOW_EVAL_RESULTS_EXPERIMENT}"
            - name: PARENT_RUN_ID
              value: "${parent_run_id}"
            - name: RESULT_PATH
              value: /data/result.json
            - name: SSL_CERT_FILE
              value: /etc/mlflow-ca/service-ca.crt
            - name: REQUESTS_CA_BUNDLE
              value: /etc/mlflow-ca/service-ca.crt
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: result
              mountPath: /data
            - name: mlflow-ca
              mountPath: /etc/mlflow-ca
            - name: script
              mountPath: /scripts
      volumes:
        - name: result
          configMap:
            name: evalhub-result
        - name: mlflow-ca
          configMap:
            name: evalhub-mlflow-ca
        - name: script
          configMap:
            name: evalhub-mlflow-logger-script
EOF
  oc wait --for=condition=Complete job/evalhub-mlflow-log -n "${job_ns}" --timeout=60s
}

main() {
  export_and_upload_ragas_dataset

  log "creating one parent mlflow run for this evaluation cycle"
  cycle_run_id="$(create_eval_cycle_run)"
  log "cycle run: ${cycle_run_id}"

  echo "--- ragas job ---"
  ragas_submit="$(submit_ragas_job)"
  echo "${ragas_submit}" | python3 -m json.tool
  ragas_id="$(echo "${ragas_submit}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["resource"]["id"])')"
  log "waiting for ragas job ${ragas_id} to finish"
  ragas_result="$(wait_for_job "${EVAL_NS}" "${ragas_id}")"
  log_result_to_mlflow "${ragas_result}" "${cycle_run_id}"

  echo "--- ibm-clear job ---"
  ibm_submit="$(submit_ibm_clear_job)"
  echo "${ibm_submit}" | python3 -m json.tool
  ibm_id="$(echo "${ibm_submit}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["resource"]["id"])')"
  log "waiting for ibm-clear job ${ibm_id} to finish"
  ibm_result="$(wait_for_job "${PIPELINES_NS}" "${ibm_id}")"
  log_result_to_mlflow "${ibm_result}" "${cycle_run_id}"

  echo "--- garak job ---"
  garak_submit="$(submit_garak_job)"
  echo "${garak_submit}" | python3 -m json.tool
  garak_id="$(echo "${garak_submit}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["resource"]["id"])')"
  log "waiting for garak job ${garak_id} to finish"
  garak_result="$(wait_for_job "${EVAL_NS}" "${garak_id}")"
  log_result_to_mlflow "${garak_result}" "${cycle_run_id}"

  echo "--- confidential data leak scan job ---"
  confidential_submit="$(submit_confidential_data_job)"
  echo "${confidential_submit}" | python3 -m json.tool
  confidential_id="$(echo "${confidential_submit}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["resource"]["id"])')"
  log "waiting for confidential data leak scan job ${confidential_id} to finish"
  confidential_result="$(wait_for_job "${EVAL_NS}" "${confidential_id}")"
  log_result_to_mlflow "${confidential_result}" "${cycle_run_id}"

  cat <<EOF

jobs submitted and logged to mlflow, experiment ${MLFLOW_EVAL_RESULTS_EXPERIMENT}
in the ${PIPELINES_NS} workspace, cycle run ${cycle_run_id}:
  ragas:             ${ragas_id}
  ibm-clear:         ${ibm_id}
  garak:             ${garak_id}
  confidential-data: ${confidential_id}
EOF
}

main "$@"

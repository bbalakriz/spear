#!/usr/bin/env bash
set -euo pipefail
# submits the two evalhub jobs proven live this session: ragas against a
# small real dataset exported from spear-shield-security-trace, and
# ibm-clear against that same mlflow experiment directly. manual, one shot
# runs, not the architecture.html automatic kfp trigger, see
# PHASE3_PLAN.md section 7 for why that's deferred.
#
# judge model is gemma4, not the coordinator's own glm-53-flash: confirmed
# live that glm-53-flash's own chain of thought reasoning tokens
# consistently blow past the evalhub sidecar's model call timeout under
# ragas's structured output prompts, gemma4 has no reasoning overhead and
# responds in about a second for the same prompts.
#
# ragas runs the full reference free set this provider ships, not just
# faithfulness/answer_relevancy: context_relevance and response_groundedness
# added too, confirmed live by reading ragas.metrics.collections' own
# ascore() signatures inside this exact quay.io/evalhub/community-ragas:v0.6.2
# image, both only need (user_input/response, retrieved_contexts), no
# reference. the other 8 metrics this provider ships, context_precision,
# context_recall, answer_correctness, context_entity_recall,
# factual_correctness, noise_sensitivity, answer_accuracy,
# semantic_similarity, every one of them requires a reference field in its
# own ascore() signature, confirmed the same way. no ground truth corpus
# exists for these real rag questions, so those 8 cannot run honestly
# against live traffic, a golden set would be a separate, later effort,
# not a quick parameter change

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=./lib-common.sh
source "${ROOT_DIR}/scripts/lib-common.sh"

JUDGE_MODEL="openai/publishers/prelude-maas/models/gemma4"
EMBEDDING_MODEL="vllm-embedding/nomic-embed-text-v1.5"
OGX_URL="http://rag-phase1-ogx-service.spear-inference.svc.cluster.local:8321/v1"
# the model actually behind the coordinator agent's own generation step,
# agents/spear-coordinator-agent/main.py's own GENERATION_MODEL default,
# not gemma4, that one is only ever the ragas/ibm-clear judge model. this
# is the real first garak target, straight at the model, not the agent or
# its guardrails, same shared ogx endpoint, confirmed live via its own
# /v1/models listing
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
  oc create token spear-shield-eval-caller -n "${EVAL_NS}" --duration=2h
}

# pulls real (question, response, retrieved_contexts) rows straight out of
# the coordinator's own mlflow traces and lands them in minio, one job, no
# agent sandbox and no local file ever involved. mlflow is a plain server
# here, confirmed live: any ordinary pod reaches it directly on its own
# internal address with a real sa bearer token
# (spear-coordinator-mlflow-caller, same credential the sandbox's own
# MLFLOW_TRACKING_TOKEN already is, minted fresh here instead of read out
# of a live pod) plus the real service ca bundle, it is a self signed cert
# otherwise, "SSLCertVerificationError: self-signed certificate in
# certificate chain", confirmed hitting it with no ca bundle at all. the
# init container here writes the dataset to a shared emptyDir, the mc
# container next to it (same image the old standalone upload job used)
# pushes that straight into minio, ragas's own provider has no mlflow
# aware dataset loading path of its own, confirmed reading its shipped
# main.py, test_data_ref.s3 is genuinely the only shape it accepts
#
# runs in spear-shield-agents, not minio: that is the one namespace the
# coordinator image's own internal imagestream can be pulled from without
# a new cross namespace system:image-puller grant, confirmed live, a
# plain tag pull from another namespace came back "authentication
# required". minio-root-creds is mirrored in with the existing
# mirror_secret helper instead, same namespace boundary, opposite
# direction, lib-common.sh already documents why a secretKeyRef needs
# its own local copy
export_and_upload_ragas_dataset() {
  log "exporting a fresh ragas dataset from real ${MLFLOW_EXPERIMENT} traces straight into minio"
  local job_ns="spear-shield-agents"
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
          image: image-registry.openshift-image-registry.svc:5000/spear-shield-agents/spear-coordinator-agent:latest
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
  # mlflow_experiment_name, not experiment_name: confirmed live reading the
  # real evalhub api server's own compiled binary, it knows nothing named
  # experiment_name or mlflow at all, only ever forwards "parameters" as an
  # opaque map, this provider's own code happens to read this exact key
  # back out of that map, that is the only reachable way in. makes
  # ibm-clear save its own run straight to mlflow, including the real
  # clear html report as a genuine artifact, confirmed live once this
  # cluster's mlflow moved to serveArtifacts: true,
  # manifests/spear-pipelines/03-mlflow.yaml. log_result_to_mlflow below
  # finds this run by its own returned mlflow_run_id and only tags it into
  # the cycle, it does not create a second one
  #
  # x-tenant spear-pipelines, not spear-shield-eval: every job's adapter
  # pod gets MLFLOW_WORKSPACE forced to its own x-tenant namespace, and
  # spear-shield-security-trace lives in spear-pipelines's own mlflow
  # workspace, confirmed live, see manifests/spear-shield-eval/01-evalhub.yaml
  log "submitting ibm-clear job (x-tenant ${PIPELINES_NS})"
  curl -sk -X POST \
    -H "Authorization: Bearer $(caller_token)" \
    -H "X-Tenant: ${PIPELINES_NS}" \
    -H "Content-Type: application/json" \
    --data "${body}" \
    "$(evalhub_url)/api/v1/evaluations/jobs"
}

# garak's "quick" benchmark, one dan probe, lite mode, confirmed live
# against its own resolve_scan_profile output reading the real provider
# image (timeout 600s in its own profile, actual run finished in 14s
# against this cluster's real glm-53-flash). no parameters needed, the
# profile already sets probe_spec/generations/target_type, garak's own
# code builds an openai compatible generator straight from model.url when
# no garak_config override is given, confirmed reading garak_adapter.py
#
# no mlflow_experiment_name here: confirmed live reading garak_adapter.py,
# its own mlflow save only checks job_spec.experiment_name directly
# (evalhub/adapter/callbacks.py), it has no parameters based bridge like
# ibm-clear's, same structural dead end ragas hit, this result goes
# through log_result_to_mlflow's own custom run path, not a native one
#
# x-tenant spear-shield-eval, not spear-pipelines: no mlflow trace lookup
# dependency here unlike ibm-clear, this is the simpler default tenant
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

# polls a submitted job until evalhub itself reports completed/failed,
# printing the final job json on stdout. these jobs finish in under two
# minutes in every real run this session, 90s/5s is comfortably loose,
# not tight
wait_for_job() {
  local tenant="$1" job_id="$2"
  local deadline=$((SECONDS + 90))
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
  log "job ${job_id} did not reach a terminal state within 90s, last seen state: ${state}"
  echo "${body}"
}

# one parent run per script invocation, every real benchmark run below
# gets tagged mlflow.parentRunId against it, so mlflow's own run list
# groups ragas, ibm-clear and later garak as one evaluation cycle instead
# of unrelated flat rows, printed run id read back off this pod's own logs,
# same plain pod shape as every other step here
create_eval_cycle_run() {
  local job_ns="spear-shield-agents"
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
          image: image-registry.openshift-image-registry.svc:5000/spear-shield-agents/spear-coordinator-agent:latest
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

# the missing write half of architecture.html's own "EvalCard auto
# generated in MLflow" promise for providers with no native save path
# reachable from the real job submission schema, confirmed live, see
# log_eval_to_mlflow.py's own header for the full ragas vs ibm-clear
# finding. runs the same plain pod shape export_and_upload_ragas_dataset
# already proved, coordinator image, minted sa token, real service ca
# bundle, no sandbox
log_result_to_mlflow() {
  local result_json="$1"
  local parent_run_id="${2:-}"
  local job_ns="spear-shield-agents"
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
          image: image-registry.openshift-image-registry.svc:5000/spear-shield-agents/spear-coordinator-agent:latest
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

  cat <<EOF

jobs submitted and logged to mlflow, experiment ${MLFLOW_EVAL_RESULTS_EXPERIMENT}
in the ${PIPELINES_NS} workspace, cycle run ${cycle_run_id}:
  ragas:      ${ragas_id}
  ibm-clear:  ${ibm_id}
  garak:      ${garak_id}
EOF
}

main "$@"

# SPEAR Shield Deployment Guide

SPEAR Shield is a permission-aware agentic security reference architecture, implemented end to end on Red Hat OpenShift AI 3.5.1: two sandboxed agents (coordinator on runc, retrieval on Kata) answer questions through NeMo Guardrails and a Kuadrant/Authorino MCP gateway with per-tool RBAC, identified by Keycloak caller tokens and SPIRE workload identity, with an OpenShift console dynamic plugin as the operator's console, a KFP ingestion + AutoRAG pipeline chain building the knowledge base, and an EvalHub evaluation cycle (ragas, IBM CLEAR, garak, and a custom confidential-data leak scanner) feeding MLflow.

Everything below was verified against the scripts and manifests in this repo:

- commands are literal
- hostnames are never hardcoded anywhere in the chain, every script resolves them live from the cluster

## Timing at a glance

| Phase | Scripts | Wall clock |
|---|---|---|
| Platform prerequisites | `00-a`, `00-b` | 30–60 min (Kata reboots the labelled pool) |
| OpenShell gateway relay | `00-b` | 2–5 min |
| Project install chain | `00-install-spear.sh` (01–09) | 30–60 min |
| Evaluation cycle | `11-submit-evalhub-jobs.sh` | ~15 min (ragas alone up to ~10) |
| **Total, first install** | | **~1.5–2.5 h** plus the manual pipeline runs |

## Table of Contents

- [Start here by role](#start-here-by-role)
- [Quick Start](#quick-start)
- [Prerequisites](#prerequisites)
- [Configuration Required](#configuration-required)
- [Component Overview](#component-overview)
- [Manual Deployment (Alternative)](#manual-deployment-alternative)
- [Post-Deployment Steps](#post-deployment-steps)
- [Using SPEAR Shield](#using-spear-shield)
- [Verification](#verification)
- [Troubleshooting](#troubleshooting)
- [Cleanup](#cleanup)

## Start here by role

> **First-time install:** [Quick Start](#quick-start) → [Configuration Required](#configuration-required) → [Verification](#verification)

> **Demo presenter:** [Using SPEAR Shield](#using-spear-shield) → Post-Deployment step 6 (injection demo)

> **Something broke:** [Troubleshooting](#troubleshooting) → [Verification](#verification)

## Quick Start

Two layers, in this order: the platform layer installs cluster-wide prerequisites, the project chain installs SPEAR Shield itself.

```
# layer 0: platform prerequisites (30-60 min; Kata reboots the labelled node pool,
# expect the api server to drop and come back mid-install)
./scripts/00-a-install-platform.sh
./scripts/00-b-install-openshell.sh     # 2-5 min

# layer 1: this project's chain (30-60 min, stops at the first failing script)
./scripts/00-install-spear.sh
```

> **⚠️ Warning:** the Kata step reboots every node in the labelled pool and the API server drops mid-install. Do not interrupt `00-a-install-platform.sh` during Kata; the script's wait loop tolerates the drop.

> **📌 Manual step:** exactly one manual step exists in the whole install — three OGX model API key secrets, needed before the chain reaches script 05. See [Configuration Required](#configuration-required).

- `00-install-spear.sh` runs scripts 01 through 09 in order, then `90-verify.sh`
- `90-verify.sh` never stops at first failure and prints a pass/fail summary
- sequence is strictly `00-a` then `00-b` then `00-install-spear.sh` — the project chain applies `KeycloakRealmImport`, `NemoGuardrails`, `Sandbox`, `AuthPolicy` and Istio `Gateway` objects, and none of those CRDs exist until the platform layer has installed their operators, so running the chain first fails on missing CRDs
- every script is idempotent and safe to rerun

> **✅ Check:** after the chain completes, `./scripts/90-verify.sh` reports all checks passing except the ones that need real data: the eval-dataset QA pairs, the AutoRAG pipeline runs, and the corpus-backed checks. Those clear after the [Post-Deployment Steps](#post-deployment-steps).

## Prerequisites

### Workstation

| Tool | Why needed |
|---|---|
| `oc` | logged in as a cluster-admin user — the chain creates operators, MachineConfigs, SCC grants, cluster-scoped RBAC, and reads node filesystems via `oc debug` |
| `helm` | OpenShell relay and the console plugin chart |
| `jq`, `curl`, `python3`, `openssl` | script helpers throughout the chain |
| `yarn` + node | console plugin frontend build, script 08 |
| Python 3.12 venv at `.venv-kfp` | manual pipeline steps: `kfp` (the working venv in this repo uses 2.17.0), `kfp-kubernetes`, `boto3`, `psycopg2-binary`, `requests` |

```
python3.12 -m venv .venv-kfp && source .venv-kfp/bin/activate
pip install kfp kfp-kubernetes boto3 psycopg2-binary requests
```

### Cluster

- **OpenShift** with the apps domain reachable, routes resolve as `<name>.<apps-domain>`
- **RHOAI 3.5.1** is installed by layer 0 if absent (`rhods-operator` 3.5.1, DSC `Ready`); if already present, its DataScienceCluster and DSCInitialization are left untouched
- **Kata** sandboxed containers are optional, `INSTALL_KATA=false` skips the node reboot; without a usable Kata node the retrieval agent falls back to runc automatically (`detect_agent_runtime_class` in script 09 checks a real kata-oc node for `/dev/kvm`, not just the RuntimeClass object)
- **SPIRE**, the zero trust workload identity manager, is installed by layer 0 (`INSTALL_ZTWIM=false` to skip); the cluster's real trust domain is whatever the live ZTWIM singleton carries (`agent-pack.internal` on the reference cluster, immutable) — the project's manifests template against it, never assume a name

## Configuration Required

> **📌 The only manual step in the whole install:** three OGX model API key secrets, created by you in your own terminal so raw keys never land in a chat transcript or script output. Script 05 fails with a clear message if any secret is missing.

```
oc create secret generic ogx-glm-key -n spear-inference \
  --from-literal=apiKey='<the maas gateway key for glm-53-flash>'
oc label secret ogx-glm-key -n spear-inference ogx.io/watch=true

oc create secret generic ogx-litemaas-key -n spear-inference \
  --from-literal=apiKey='<the litemaas gateway key, scoped to the chat model>'
oc label secret ogx-litemaas-key -n spear-inference ogx.io/watch=true

oc create secret generic ogx-embedding-key -n spear-inference \
  --from-literal=apiKey='not-required-auth-disabled-on-tei'
oc label secret ogx-embedding-key -n spear-inference ogx.io/watch=true
```

Notes:

- litemaas scopes keys per model on the server side (confirmed live: reusing the chat model's key for the embedding model returns 401), so the two gateway keys are genuinely separate
- the embedding key is a placeholder by design, the self-hosted text embeddings inference server (`manifests/spear-inference/02-embed-server.yaml`, `nomic-embed-text-v1.5`, 768 dims) has no auth of its own
- every other credential in the system (minio, pgvector, Keycloak db, OpenProject, the MCP backend token, persona logins, API tokens) is generated once in cluster by the scripts, and never regenerated on rerun

Platform layer env knobs, all optional:

| Variable | Default | Purpose |
|---|---|---|
| `CLUSTER_DOMAIN` | read live from `ingresses.config.openshift.io/cluster` | apps domain for routes |
| `KEYCLOAK_HOSTNAME` | `sso.<CLUSTER_DOMAIN>` | Keycloak server route |
| `INSTALL_KATA` | `true` | `false` skips Kata entirely, no node reboot |
| `INSTALL_ZTWIM` | `true` | `false` skips SPIRE |
| `OPENSHELL_CHART_VERSION` | `0.0.116` | OpenShell gateway relay Helm chart version |

## Component Overview

Numbers match the install chain: scripts 01–09 run via `00-install-spear.sh`; 10 and 11 are the evaluation layer, installed and submitted separately.

### Platform layer

| # | Script | Installs | Namespace |
|---|---|---|---|
| 0a | `00-a-install-platform.sh` | RHOAI 3.5.1 (operator, DSC/DSCI, dashboard flags), operators (Authorino, RHCL/Kuadrant, OSSM3, MCP gateway, RHBK/Keycloak, agent sandbox), Red Hat agent sandbox operator (replaces any upstream kubernetes-sigs controller), Istio + Istio CNI control plane, Authorino + Kuadrant instances, Keycloak server, ZTWIM/SPIRE, Kata + nftables initramfs patch | cluster wide |
| 0b | `00-b-install-openshell.sh` | OpenShell gateway relay (Helm chart 0.0.116, release `openshell`), sandbox SCC for `openshell-sandbox`, external gRPC route (external CLI access only, no in-cluster consumer uses it) | `openshell` |

### Project chain

| # | Script | Installs | Namespace |
|---|---|---|---|
| 1 | `01-install-namespaces.sh` | the project's eight namespaces, minio namespace, `genAiStudio`/`autorag` dashboard flags (additive patch, never removes other teams' keys) | all |
| 2 | `02-install-storage.sh` | minio + console route, buckets `rag-documents` and `dsp-pipeline-artifacts`, seed corpus (8 docs + ABAC `manifest.json`, 2 clearance tiers) uploaded under `raw-intake/`, `minio-dsp-creds` synced into `spear-pipelines` | `minio` |
| 3 | `03-install-data.sh` | pgvector postgres + `vector` extension on `ragdb` + read role (`rag-query-relay` is deliberately not applied here, its image is the retrieval-agent build, which does not exist until script 09) | `spear-data` |
| 4 | `04-install-pipelines.sh` | DSPA `rag-phase1-dspa` (AutoML/AutoRAG enabled), `mlflow` database on pgvector, the shared cluster-wide `mlflow` instance (name must literally be `mlflow`, workloads land in `redhat-ods-applications`), `spear.io/mlflow-workspace` label on `spear-pipelines`, MLflow SubjectAccessReview RBAC (pipeline runner + console backend + human users), `phase1-ingestion` experiment, `phase1-apply-pattern-pipeline` registration (needs `.venv-kfp`, warns and skips if absent) | `spear-pipelines` |
| 5 | `05-install-inference.sh` | TEI embedding server, judge proxy (rewrites only the LLM judge structured-output calls), single `OGXServer` `rag-phase1-ogx` with both foundation models (`Qwen3.6-35B-A3B`, `glm-53-flash` via MaaS) + embedding model wired in via the image's env-driven provider slots, `/v1/models` confirmation | `spear-inference` |
| 6 | `06-setup-autorag-secrets.sh` | `autorag-s3-creds` (minio creds repackaged under `AWS_*` names, values copied base64 only) and `autorag-ogx-creds` (points at the judge proxy, placeholder key, OGX auth is off) | `spear-pipelines` |
| 7 | `07-install-guardrails.sh` | four guardrail configs (`internal-docs`, `vendor-submission`, `work-tracker`, `coordinator-agent`) + `NemoGuardrails` CR `rag-phase1-guardrails`, early `spear-shield-gateway` + Keycloak realm import (cheap, idempotent, sequenced here so the CR's gateway check passes), pipeline, guard-proxy, and coordinator-caller RBAC, live smoke tests against `/v1/guardrail/checks` (allows + PII/injection blocks, per config) | `spear-guardrails` |
| 8 | `08-install-console.sh` | console backend (binary docker build, digest-pinned deploy, routes + pipeline names injected via `oc set env`, never hardcoded), backend RBAC + node reader + closed-loop patcher RBAC + `sso` realm demo users, frontend plugin (`yarn build` + Helm chart `phase1-ingestion-console`) | `spear-console` |
| 9 | `09-install-worktracker-and-agents.sh` | OpenProject + db + demo data (personas, `data-governance` project, work packages, API tokens), Keycloak realm + SPIFFE federated auth (live admin API), `spear-shield-gateway` + tool-RBAC auth policies, `spear-openproject-mcp` (MCPServer/HTTPRoute/Registration/VirtualServer), `spear-shield-mcp-guard-proxy`, MCP ext_proc BUFFERED patch (controller paused after), ClusterSPIFFEID, `spear-shield-a2a-gateway` (dual-issuer AuthPolicy: Keycloak OR SPIRE JWT SVID, fail-closed 401), both agent image builds, `rag-query-relay`, in-cluster deploy job creating the two OpenShell sandboxes (coordinator runc, retrieval Kata/runc) + provider + policies | `spear-worktracker`, `spear-shield-agents` |

### Evaluation layer

| # | Script | Installs | Namespace |
|---|---|---|---|
| 10 | `10-install-evalhub.sh` | EvalHub CR (bundled in RHODS 3.5.1) with custom providers `ragas-v062`/`ibm-clear-v060` (newer quay tags than the buggy bundled v0.5.0) + `garak`, EvalHub db on the shared pgvector, ragas bucket + S3 creds, caller SA + RBAC, `spear-pipelines` as second tenant + dummy model auth | `spear-shield-eval` |
| 11 | `11-submit-evalhub-jobs.sh` | manual one-shot: exports a fresh ragas dataset from real MLflow traces (experiment `spear-shield-security-trace`) into minio, one parent MLflow cycle run, then four real eval jobs: ragas (4 reference-free metrics), IBM CLEAR (agentic eval over MLflow traces, saves its own run natively), garak quick scan (straight at `glm-53-flash`), spear confidential-data leak scan (custom provider, presidio over recorded answers) | `spear-shield-eval` |

### Optional demo scripts

Never part of the required chain:

- `scripts/09-demo-tool-response-injection.sh` — plants one real malicious work package (prompt-injection payload in the subject line) in `data-governance`; through the real chat it returns a genuine clean + malicious mix, and the guard proxy's response guard blocks the malicious subject before any model sees it. Install and cleanup are both idempotent.
- `scripts/01-patch-kata-nftables.sh` — standalone rerunnable Kata guest initramfs nf_tables patch (OpenShell needs the modules). The patch is wiped by `kata-osbuilder-generate` on node reboot; rerun it after any Kata node reboot.

## Manual Deployment (Alternative)

If you prefer to run steps individually or need to rerun one, run them in chain order — later components depend on earlier ones, and each script prints its own next steps.

> **⚠️ Warning:** the order is strict. Each script below depends on prerequisites created by the ones above it — running out of order fails on missing secrets, routes, or CRDs.

```
./scripts/00-a-install-platform.sh
./scripts/00-b-install-openshell.sh
./scripts/01-install-namespaces.sh
./scripts/02-install-storage.sh
./scripts/03-install-data.sh
# create the three ogx secrets here - see Configuration Required
./scripts/05-install-inference.sh
./scripts/04-install-pipelines.sh     # needs .venv-kfp for the apply-pattern registration
./scripts/06-setup-autorag-secrets.sh
./scripts/07-install-guardrails.sh
./scripts/08-install-console.sh
./scripts/09-install-worktracker-and-agents.sh
# evaluation layer, optional:
./scripts/10-install-evalhub.sh
./scripts/11-submit-evalhub-jobs.sh   # manual one-shot, see Post-Deployment Steps
```

Notes on the scripts that are not plain apply-and-wait:

- `00-a-install-platform.sh`
  - all subscriptions are `installPlanApproval: Manual`
  - approved by `lib-olm.sh`'s guard, which refuses any install plan not carrying the subscription's pinned `startingCSV` (a rerun can never silently approve an OLM-proposed channel upgrade)
  - the Kata sequence never touches a KataConfig that is already fully installed and healthy
  - expect the API server to drop while the Kata pool reboots
- `05-install-inference.sh`
  - `require_secret` exits with the exact create commands if the three OGX secrets are missing — create them and rerun
- `09-install-worktracker-and-agents.sh`, the heaviest script
  - Keycloak realm import plus one live admin-API setup, SPIFFE federated auth
  - OpenProject seeding via `rails runner` over `oc exec`
  - the MCP ext_proc BUFFERED patch sequence: unpause controller, wait for registrations Ready, pause controller, hand-patch both EnvoyFilters
  - both agent builds and the in-cluster `deploy-spear-sandboxed-agents` job
  - closing output prints the one manual check that matters: compare the ClusterSPIFFEID's pinned `agents.x-k8s.io/sandbox-name-hash` against the real pod label, and patch `manifests/spear-shield-agents/06-cluster-spiffeid.yaml` if they differ (the hash is deterministic from the sandbox name, but was never independently reconfirmed on every cluster)
- reruns are safe everywhere
  - secrets are generated once and never rotated by a rerun
  - jobs are deleted and recreated since they are immutable
  - the guard-proxy deployment is rollout-restarted on every run so a configmap update always reaches a fresh pod

## Post-Deployment Steps

Scripted install gets you to a verified, empty-corpus platform. These steps are manual by design — they need real data or cost real model calls. Steps 1–3 populate the knowledge base (the numbered 1–9 flow in `spear-shield-e2e-slide.drawio`); steps 4–6 are evaluation and demo.

#### 1. Real ingestion pipeline run

Needs the `.venv-kfp` venv:

```
source .venv-kfp/bin/activate
python pipelines/phase1_ingestion/compile_and_run.py
```

- uses your own `oc whoami -t` token, no separate pipeline credential
- run id lands in MLflow experiment `phase1-ingestion`, workspace `spear-pipelines`

#### 2. Real AutoRAG optimization run

Needs step 1's eval dataset:

```
python pipelines/phase1_autorag/run_autorag.py seed-batch-001
```

- triggers the RHOAI auto-registered `documents-rag-optimization-pipeline`

#### 3. Apply the winning pattern

The S3 lookup is in-cluster only — read the winning pattern from the console backend's own endpoint (`GET /kfp/autorag-leaderboard?run_id=<autorag_run_id>` through the console plugin proxy) and pass it to:

```
python pipelines/phase1_apply_pattern/compile_and_run.py \
    <batch_id> <autorag_run_id> <pattern_name> <chunk_size> <chunk_overlap> <embedding_model>
```

- this is what actually populates `rag_chunks` with ABAC-tagged embeddings

#### 4. Evaluation layer

After `10-install-evalhub.sh`:

```
# build the two custom provider images once (in-cluster registry):
oc start-build spear-confidential-data-provider --from-dir=evalhub --wait -n spear-shield-eval
oc start-build evalhub-cycle-runner --from-dir=. \
  --exclude='(^|/)(\.git|\.venv-kfp|\.venv|venv|node_modules|__pycache__|archive)(/|$)|\.(docx|pptx|rtf|excalidraw|pyc)$' \
  --wait -n spear-shield-eval

# one full evaluation cycle (ragas + ibm-clear + garak + confidential-data):
./scripts/11-submit-evalhub-jobs.sh
```

> **⚠️ Warning:** the `--exclude` on the cycle-runner build is required — `oc start-build` does not read `.dockerignore`, and without it the context upload hangs on `.venv-kfp`/`node_modules`.

- judge model is `gemma4`, not the coordinator's `glm-53-flash`, whose reasoning tokens blow the eval sidecar's timeout
- garak targets `glm-53-flash` directly
- results land in MLflow experiment `spear-shield-evalhub-results` under one parent cycle run
- expect ragas to take up to about 10 minutes (900s poll deadline, measured worst case 621s)

#### 5. Optional recurring cycle

`evalhub-cycle-runner` CronJob is deployed suspended (`spec.suspend: true`, wired with real RBAC and a pinned image) because every cycle makes real paid model calls. For a demo window:

```
oc patch cronjob evalhub-cycle-runner -n spear-shield-eval --type merge \
  -p '{"spec":{"suspend":false}}'
```

#### 6. Optional injection demo

- run `./scripts/09-demo-tool-response-injection.sh`
- then ask "show my work packages" in the console chat

## Using SPEAR Shield

Open the OpenShift console as a demo persona — accounts are created in the `sso` realm by script 08. Passwords live in the `spear-shield-console-creds` secret in `spear-console`:

```
oc get secret spear-shield-console-creds -n spear-console \
  -o go-template='{{index .data "balakrishnan.b"}}' | base64 -d; echo
```

| Persona | Clearance | Dept | Tool roles |
|---|---|---|---|
| `balakrishnan.b` | `general` | `engineering` | `tool:search_work_packages` only |
| `siddhartha.de` | `restricted` | `data-governance` | `tool:search_work_packages` + `tool:update_work_package` |

Console navigation, under the SpearShield section:

| Page | What it shows |
|---|---|
| Knowledge Base | phase 1 ingestion controls, corpus + pipeline state |
| Ask SPEAR Shield | live chat through the full chain; box-and-arrow per-hop trace (coordinator → retrieval, guardrails, MCP gateway, ABAC filter) |
| Under the Hood | six live security demos over the one real topology (sandbox isolation runc vs Kata, workload identity pathway, tool authorization, identity rejection, response injection) |
| Evaluation Trends | eval cycles from MLflow (`spear-shield-evalhub-results`), confidential-data leak card with the closed-loop "tighten guardrails" button |
| Architecture Status | live component probes + runtimeClass/node per sandbox |

External endpoints — hosts resolved live at install; the MCP gateway and OpenProject are real routes:

```
oc get route keycloak -n keycloak -o jsonpath='{.spec.host}'                        # sso (realms: sso, spear-shield-agents)
oc get route rag-phase1-guardrails -n spear-guardrails -o jsonpath='{.spec.host}'   # guardrails
oc get route minio-console -n minio -o jsonpath='{.spec.host}'                      # minio console
# mcp gateway:   https://mcp-spear-shield-agents.<apps-domain>/mcp
# openproject:   https://spear-openproject.<apps-domain>
```

- asking questions end to end works from the console UI
- a direct ask from a workstation against the in-cluster A2A service DNS fails silently, 0-byte response — the coordinator's A2A listener is published through the OpenShell relay inside the cluster by design, gated by the A2A gateway's AuthPolicy (Keycloak realm JWT or SPIRE JWT SVID, fail-closed 401)

Demo personas' ABAC expectations, verified against the realm import (`manifests/spear-shield-agents/01-keycloak-realm.yaml`):

- `balakrishnan.b` sees `general` docs only; update calls are denied by the gateway's per-tool RBAC (`tool:update_work_package` not in his `resource_access` claim)
- `siddhartha.de` sees `general` and `restricted` data-governance docs and may update work packages, but only his own (the assignee-self check resolves the caller from the same JWT)

## Verification

The repo's own health check — read-only, checks every component, prints a pass/fail summary, never stops at first failure. 39 checks total: 31 named plus an 8-namespace loop.

```
./scripts/90-verify.sh
```

It covers:

- **namespaces** — all eight
- **dashboard flags** — `genAiStudio`/`autorag`
- **pgvector** — deployment + the `vector` extension
- **minio** — deployment + seed-corpus configmap
- **DSPA** — `rag-phase1-dspa` Ready
- **guardrails** — CR Ready + route + a real call by the pipeline-runner SA, not just `can-i`
- **OGX** — Ready + health + all three models listed; both AutoRAG generation models registered + a real chat completion; reachable from `spear-pipelines` (the NetworkPolicy ingress case that once silently dropped)
- **MLflow** — Available + API reachable with the `spear-pipelines` workspace header
- **pipelines** — both registered on the DSPA; eval dataset present with real QA pairs; AutoRAG secrets
- **EvalHub** — both tenants' pieces
- **worktracker/agents** — OpenProject + MCP server + guard proxy; gateway `allowedRoutes=All`; `rag-query-relay`
- **console** — backend + its own SA's MLflow access

Point checks beyond the script:

```
oc get sandbox -n spear-shield-agents                                    # both sandboxes Running
oc get consoleplugin phase1-ingestion-console                            # plugin rolled out
oc get keycloakrealmimport spear-shield-agents-realm -n keycloak         # realm imported
oc get mcpserverregistration -A                                          # registrations Ready
```

> **✅ Check:** no test suite exists in the repo — `90-verify.sh` plus the in-script smoke tests (guardrails verdicts in 07, model probes in 05) are the verification story. Label results as script or health-check output, never "suite green".

## Troubleshooting

General first moves:

```
oc whoami                                    # still logged in as cluster-admin?
oc get pods -n <namespace>                   # which component is unhappy?
oc logs -n <namespace> <pod-name>            # its own log
```

Known behaviors, all previously hit live and worked around in the scripts:

| Symptom | Cause | Fix |
|---|---|---|
| API server drops mid-install | the Kata step reboots every node in the labelled pool | do not interrupt `00-a-install-platform.sh` during Kata; the wait loop tolerates the drop; a healthy KataConfig is never touched on rerun; after a node reboot rerun `./scripts/01-patch-kata-nftables.sh` (the initramfs patch is wiped) |
| script 05 fails on missing OGX secrets | expected on first install | create the three secrets from [Configuration Required](#configuration-required) and rerun |
| `phase1-apply-pattern-pipeline` registration skipped | script 04 warns and skips if `.venv-kfp` is absent | create the venv (Prerequisites) and rerun script 04 |
| MLflow 400 "Workspace context is required" | caller's namespace lacks the `spear.io/mlflow-workspace` label, or the request lacks `X-MLflow-Workspace: spear-pipelines` | script 04 sets the label on `spear-pipelines`; cross-namespace callers also need the SubjectAccessReview RBAC (script 04 grants pipeline runner, console backend, and human users) |
| MCP tools/call "Bad request syntax" or 404s after install | mcp-gateway operator (v0.7.1 preview) regenerates its ext_proc EnvoyFilter with `request_body_mode: STREAMED`, which plain HTTP backends cannot parse | script 09 pauses the controller and patches both filters (spear-shield-agents and agent-pack) to BUFFERED; if you ever unpause the controller for maintenance, rerun script 09's patch step afterwards |
| console tighten-guardrails button returns 403 | the closed-loop patcher RBAC (`manifests/spear-guardrails/09-console-backend-closed-loop-rbac.yaml`) is missing | applied by script 08 — rerun script 08 (idempotent) if the Role/RoleBinding is missing |
| agent code changes not showing up in the sandbox | sandboxes seed their workspace PVC from the image exactly once; podTemplate is create-time only (`oc patch` on the CR is accepted but never reconciles into the running pod) | after `oc start-build <agent> --from-dir=agents --wait -n spear-shield-agents`, delete the sandbox's PVC and pod together (PVC first), then recreate; plain `oc delete pod` keeps the stale PVC. Sandboxes are Sandbox CRs (`agents.x-k8s.io/v1beta1`) |
| a provider refuses to delete | a provider attached to a sandbox refuses deletion even when the sandbox pod is gone | delete the sandboxes first |
| sandbox pods sitting Failed after a node reboot | the sandbox predates the create-time `restartPolicy: Always` patch | delete and recreate it via script 09 (idempotent) |
| SPIRE identity mismatch, A2A 401 from the relay path | Keycloak's `spear-coordinator-workload` client pins the sandbox SVID subject exactly: `spiffe://<trust-domain>/ns/spear-shield-agents/sandbox/<sandbox-name-hash>` | compare the ClusterSPIFFEID's pinned hash against the real pod label (script 09's closing output prints both commands) and patch `06-cluster-spiffeid.yaml` if they differ |
| route URLs 404 after a manual `oc apply` of the backend manifest | resolved route URLs live only in the deployment env (`oc set env` in script 08) | reapplying the bare manifest never wipes them, but a fresh install must run script 08 to set them |
| stale image served after a rebuild | `:latest` + `imagePullPolicy: IfNotPresent`, or the digest read mid-build | every custom deployment is digest-pinned by its install script; read the digest only after the build condition completes — reading `oc get is` mid-build yields the previous build's digest |
| OpenProject wiped | database lost | personas, project, work packages, and API tokens are all reseeded by a single rerun of script 09 (`seed_openproject_demo_data` checks for existing records before inserting, so reruns are safe; existing API tokens are never destroyed or reissued) |

Deployment state vs Git:

- never assume running pods match HEAD
- every code change needs its own build plus digest-pinned deploy cycle — console via script 08, agents via build plus sandbox cycle
- check the running pod's imageID against the digest built from the current commit before claiming a fix is live

## Cleanup

```
./scripts/99-cleanup.sh            # asks for confirmation
./scripts/99-cleanup.sh --force    # no prompt
```

- deletes the eight namespaces created by the base chain (agents, worktracker, console, pipelines, guardrails, inference, data, minio) in dependency order — sandboxes and gateways first, pgvector and minio last
- plus the `spear-shield-agents` Keycloak realm import

> **⚠️ Warning:** `spear-shield-eval`, the optional evaluation layer's own namespace, is **not** touched by this script and is left standing if script 10 was ever run. Delete it by hand (`oc delete namespace spear-shield-eval`) if a full teardown is what you want.

- deliberately left alone otherwise: RHOAI itself, the DSC/DSCI, the odh-dashboard-config CR (other projects may depend on the flags), the shared MLflow instance, and all platform-layer operators (Authorino, Kuadrant, OSSM3, MCP gateway, Keycloak operator, SPIRE, agent sandbox operator, OpenShell)
- operator uninstall is a manual, cluster-owner decision

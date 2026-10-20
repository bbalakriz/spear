# SPEAR Shield Deployment Guide

Enterprises have been deploying AI at a fast pace for the past two years. Out in the field the same pattern keeps showing up, security gets addressed after the deployment, never before.

CISO teams keep asking the same three questions. Who exactly can this AI see data for? What happens if someone tries to manipulate it through the documents or tools it uses? If something goes wrong, can we actually prove what it accessed and why?

SPEAR is an answer to closing that gap, a secure permission aware agentic runner designed to solve the OWASP failure modes we see repeatedly in enterprise AI deployments, with one real control for each one. It is implemented end to end on Red Hat OpenShift AI 3.5.1: two sandboxed agents (coordinator on runc, retrieval on Kata) answer questions through NeMo Guardrails and a Kuadrant/Authorino MCP gateway with per-tool RBAC, identified by Keycloak caller tokens and SPIRE workload identity, with an OpenShift console dynamic plugin as the operator's console, a KFP ingestion plus AutoRAG pipeline chain building the knowledge base and an EvalHub evaluation cycle (ragas, IBM CLEAR, garak and a custom confidential data leak scanner) feeding MLflow.

First install the project chain and an optional evaluation cycle. Every command below is literal, every hostname is resolved live from the cluster by the script that needs it and every script is idempotent, safe to rerun.

## Architecture at a glance

![Solution overview](images/solution-overview.png)

Three sandboxed layers. The console plugin is the only user facing door, no client supplied identity anywhere. The coordinator sandbox (runc) holds the model reasoning loop behind NeMo Guardrails. The retrieval sandbox (Kata when available) is the only place that ever touches a real row of `rag_chunks` or a real work package, gated by ABAC on the way into Postgres and by per-tool RBAC on the way into the MCP gateway.

The same diagram's data path runs independently of any live question. Every uploaded document passes through ABAC quarantine and NeMo Guardrails sanitization before anything is indexed, a document that fails stays quarantined rather than reaching the eval dataset. AutoRAG then sweeps chunk size, overlap and embedding model combinations against that real eval dataset and only the winning pattern is ever applied, the one step that actually chunks, embeds and writes documents into `rag_chunks`, ABAC tagged from that point on. The same mlflow instance that traces every live agent question also carries the ingestion audit trail and the continuous evaluation cycle, EvalHub's four providers (ragas, IBM CLEAR, garak and the confidential data scanner) score real agent traces on a schedule, not a one time report, feeding results back into the same experiments an operator watches from the Evaluation Trends page.

![Control flow path](images/control-flow-path.png)

One question traverses six enforcement layers end to end: sandbox isolation, openshell egress policy, guardrails, gateway auth, per-tool RBAC and ABAC row filtering. `Detailed-architecture.md` is the deep dive, every hop in that drawing cited against the exact file and line that proves it, this guide stays the install and operate reference.

## Prerequisites

### 1. Get a cluster

- OpenShift with the apps domain reachable, routes resolve as `<name>.<apps-domain>`
- logged in as cluster-admin, the install chain creates operators, MachineConfigs, SCC grants and cluster scoped RBAC
- RHOAI 3.5.1 is installed by the platform layer if absent, left untouched if already present
- Kata is optional, `INSTALL_KATA=false` skips the node reboot, retrieval falls back to runc automatically if no usable Kata node exists
- SPIRE (zero trust workload identity manager) is installed by the platform layer, `INSTALL_ZTWIM=false` to skip, the manifests template against the cluster's real trust domain, never a hardcoded one

### 2. Workstation tools

- `oc` and `helm`
- `jq`, `curl`, `python3`, `openssl`
- `yarn` and node, for the console plugin frontend build
- a Python 3.12 venv at `.venv-kfp`, needed for the manual pipeline steps in step 8:

```
python3.12 -m venv .venv-kfp && source .venv-kfp/bin/activate
pip install kfp kfp-kubernetes boto3 psycopg2-binary requests
```

### 3. The one manual step, OGX keys

Three model API key secrets, created by you so raw keys never land in a script log or chat transcript. Script 05 fails with the exact create commands if any are missing.

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

litemaas scopes keys per model server side, reusing the chat key for embedding returns 401, so the two gateway keys are genuinely separate. The embedding key is a placeholder by design, the self hosted TEI server has no auth of its own. Every other credential in the system (minio, pgvector, Keycloak, OpenProject, persona logins) is generated once in cluster and never regenerated on rerun.

## Install

### 4. Platform layer

```
./scripts/00-a-install-platform.sh   # 30-60 min, reboots the Kata node pool
./scripts/00-b-install-openshell.sh  # 2-5 min
```

> Warning: the Kata step reboots every node in the labelled pool and the API server drops mid install. Do not interrupt `00-a-install-platform.sh`, its wait loop tolerates the drop. A healthy KataConfig is never touched on rerun.

Installs RHOAI 3.5.1, Authorino, Kuadrant, OSSM3, the MCP gateway operator, RHBK/Keycloak, the Red Hat agent sandbox operator, SPIRE, the Kata nftables initramfs patch and the OpenShell gateway relay (Helm release `openshell`).

### 5. Project chain

```
./scripts/00-install-spear.sh
```

Runs scripts 01 through 09 in that exact numeric order, then `90-verify.sh` automatically, see step 7 for what it checks. Stops at the first failing script. Platform layer must run first, the project chain applies `KeycloakRealmImport`, `NemoGuardrails`, `Sandbox`, `AuthPolicy` and Gateway objects whose CRDs do not exist until the platform layer installs their operators. Create the three OGX secrets from step 3 before the chain reaches script 05, it is the only script that reads them.

Every one of the 9 scripts is independently safe to rerun in this same numeric order if one needs to be redone, each prints its own next step.

### 6. What each script installs

- `01-install-namespaces.sh`, the project's eight namespaces, `genAiStudio`/`autorag` dashboard flags
- `02-install-storage.sh`, minio, buckets `rag-documents` and `dsp-pipeline-artifacts`, the seed corpus uploaded under `raw-intake/`
- `03-install-data.sh`, pgvector postgres plus the `vector` extension on `ragdb`
- `04-install-pipelines.sh`, DSPA `rag-phase1-dspa`, the shared cluster wide MLflow instance, the `phase1-ingestion` experiment and `phase1-apply-pattern-pipeline` registration (needs `.venv-kfp`, warns and skips if absent)
- `05-install-inference.sh`, TEI embedding server, judge proxy, `OGXServer rag-phase1-ogx` with both foundation models plus embedding
- `06-setup-autorag-secrets.sh`, `autorag-s3-creds` and `autorag-ogx-creds`
- `07-install-guardrails.sh`, four guardrail configs (`internal-docs`, `vendor-submission`, `work-tracker`, `coordinator-agent`), the `NemoGuardrails` CR, live smoke tests against `/v1/guardrail/checks`
- `08-install-console.sh`, console backend and frontend plugin, routes injected via `oc set env`, never hardcoded
- `09-install-worktracker-and-agents.sh`, the heaviest script: OpenProject plus demo data, the `spear-shield-agents` Keycloak realm, SPIFFE federated auth, both MCP gateways and the guard proxy, both agent builds and the in cluster job creating the two OpenShell sandboxes

Optional, never part of the required chain: `scripts/01-patch-kata-nftables.sh` (rerun after any Kata node reboot, see Troubleshooting) and `scripts/09-demo-tool-response-injection.sh` (see step 8.8).

## Verify

### 7. Health check

```
./scripts/90-verify.sh
```

Read only, 39 checks total across namespaces, dashboard flags, pgvector, minio, DSPA, guardrails, OGX, MLflow, both pipelines, EvalHub, the worktracker and agents stack and the console. Never stops at first failure, prints a pass or fail summary. After the chain completes, everything passes except the checks that need real data, the eval dataset, the AutoRAG runs and the corpus backed checks, those clear after step 8.

> Screenshot needed: the console's Architecture Status page, showing live component probes and runtimeClass per sandbox, next to this script's own terminal output.

Point checks beyond the script:

```
oc get sandbox -n spear-shield-agents                            # both sandboxes Running
oc get consoleplugin phase1-ingestion-console                    # plugin rolled out
oc get keycloakrealmimport spear-shield-agents-realm -n keycloak # realm imported
oc get mcpserverregistration -A                                  # registrations Ready
```

No test suite exists in this repo, `90-verify.sh` plus the in script smoke tests are the whole verification story.

### 8. Post-install real-data steps

Scripted install gets you a verified, empty corpus platform. These steps need real data or real model calls, so they stay manual.

#### 8.1 The files issue and sanitization catching it

The seed corpus at `seed-data/raw-intake/` includes one real, deliberately planted document, `vendor_dpa_acme_corp.md`, an external vendor submission carrying an actual embedded prompt injection payload in its own text, plus a payment reference number. It is routed through the `vendor-submission-config` guardrails profile, distinct from the `internal-docs` profile every other seed document uses.

> Screenshot needed: the raw file's injection payload, side by side with the Knowledge Base page's sanitization run card after step 8.2 runs, showing this one document blocked rather than copied to `sanitized/`.

#### 8.2 Real ingestion pipeline run

Needs `.venv-kfp`:

```
source .venv-kfp/bin/activate
python pipelines/phase1_ingestion/compile_and_run.py
```

Every document is sent through the guardrails `/v1/guardrail/checks` call, the vendor submission from 8.1 is blocked, every other document passes and moves on to real eval question generation over the sanitized set. Nothing is chunked or written to `rag_chunks` yet. Run id lands in MLflow experiment `phase1-ingestion`, workspace `spear-pipelines`, using your own `oc whoami -t` token.

#### 8.3 Real AutoRAG optimization run

Needs 8.2's eval dataset:

```
python pipelines/phase1_autorag/run_autorag.py seed-batch-001
```

Triggers the RHOAI auto-registered `documents-rag-optimization-pipeline`.

#### 8.4 Apply the winning pattern

Read the winning pattern from the console backend (`GET /kfp/autorag-leaderboard?run_id=<autorag_run_id>` through the console proxy), then:

```
python pipelines/phase1_apply_pattern/compile_and_run.py \
    <batch_id> <autorag_run_id> <pattern_name> <chunk_size> <chunk_overlap> <embedding_model>
```

This is the only step that actually chunks, embeds and writes sanitized documents into `rag_chunks`, ABAC tagged, ready for real retrieval.

#### 8.5 Two personas, one prompt, two different answers

Open the console as both demo personas (step 9), paste this same question into the SpearShield Assistant chat in both browser sessions:

```
Look up our onboarding and data governance policy documents in the knowledge base then check my 'in progress" work packages and mark them as "closed".
```

The ABAC filter on `rag_chunks` means `balakrishnan.b` and `siddhartha.de` get genuinely different source chunks back for the same question. `balakrishnan.b` cannot update his own work package either, his token has `tool:search_work_packages` only, the gateway's AuthPolicy denies the update call with a real 403, visible right in the chat's own trace timeline.

> Screenshot needed: both browser sessions side by side, same prompt, different RAG citations and the trace step showing the blocked update call for `balakrishnan.b`.

#### 8.6 Optional, the malicious response injection demo

```
./scripts/09-demo-tool-response-injection.sh
```

Plants one real malicious work package, a prompt injection payload in the subject line, assigned to `balakrishnan.b`. Ask "show my work packages" in the chat, the response comes back as a genuine mix of clean and malicious content and the guard proxy's response guard blocks the malicious subject before any model ever sees it.

> Screenshot needed: the chat answer showing the clean work package, plus the guard proxy's blocked response in the trace timeline.

#### 8.7 Evaluation layer, one real cycle

After `10-install-evalhub.sh`:

```
oc start-build spear-confidential-data-provider --from-dir=evalhub --wait -n spear-shield-eval
oc start-build evalhub-cycle-runner --from-dir=. \
  --exclude='(^|/)(\.git|\.venv-kfp|\.venv|venv|node_modules|__pycache__|archive)(/|$)|\.(docx|pptx|rtf|excalidraw|pyc)$' \
  --wait -n spear-shield-eval

./scripts/11-submit-evalhub-jobs.sh
```

> Warning: the `--exclude` on the cycle runner build is required, `oc start-build` does not read `.dockerignore`.

Four real jobs in one cycle, all landing in MLflow experiment `spear-shield-evalhub-results` under one parent run: ragas (4 reference free metrics, judged by `gemma4`), IBM CLEAR (agentic eval straight over MLflow traces), garak (quick scan straight at `glm-53-flash`) and the custom confidential data leak scanner (presidio over recorded answers). Expect ragas to take up to about 10 minutes.

> Screenshot needed: the Evaluation Trends page showing one cycle's four provider results together, ragas, IBM CLEAR, garak and the confidential data leak card.

#### 8.8 Optional, a live recurring cycle

`evalhub-cycle-runner` ships suspended since every cycle makes real paid model calls:

```
oc patch cronjob evalhub-cycle-runner -n spear-shield-eval --type merge \
  -p '{"spec":{"suspend":false}}'
```

> Screenshot needed: two or more Evaluation Trends cycles back to back, showing the trend line move after a real guardrail tightening.

## Use it

### 9. Persona login

```
oc get secret spear-shield-console-creds -n spear-console \
  -o go-template='{{index .data "balakrishnan.b"}}' | base64 -d; echo
```

| Persona | Clearance | Dept | Tool roles |
|---|---|---|---|
| `balakrishnan.b` | `general` | `engineering` | `tool:search_work_packages` only |
| `siddhartha.de` | `restricted` | `data-governance` | `tool:search_work_packages` plus `tool:update_work_package` |

![Identity and token flow](images/identity-token-flow.png)

The console never sends a username anywhere, the backend resolves identity by a live apiserver self lookup on your own OAuth token, then mints a Keycloak realm token for that exact username. That token rides twice into the coordinator sandbox, once as a header the openshell relay strips, once in the a2a body the coordinator's own code actually reads. `balakrishnan.b` sees `general` docs only, `siddhartha.de` sees `general` and `restricted` data governance docs and may update work packages, but only his own, the assignee-self check resolves the caller from that same token.

### 10. Under the hood demos

Console navigation, under the SpearShield section:

| Page | What it shows |
|---|---|
| Knowledge Base | ingestion controls, corpus and pipeline state, the sanitization run card from step 8.1 |
| Ask SPEAR Shield | live chat, box and arrow per hop trace |
| Under the Hood | six live security demos over the one real topology |
| Evaluation Trends | eval cycles from MLflow, the confidential data leak card with its closed loop tighten button |
| Architecture Status | live component probes, runtimeClass and node per sandbox |

> Screenshot needed: Under the Hood's runtime isolation compare (runc versus Kata), side by side terminals.

> Screenshot needed: Under the Hood's workload identity and tool scope and assignee scenarios, showing the real trace steps.

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
| script 05 fails on missing OGX secrets | expected on first install | create the three secrets from step 3 and rerun |
| `phase1-apply-pattern-pipeline` registration skipped | script 04 warns and skips if `.venv-kfp` is absent | create the venv (step 2) and rerun script 04 |
| MLflow 400 "Workspace context is required" | caller's namespace lacks the `spear.io/mlflow-workspace` label or the request lacks `X-MLflow-Workspace: spear-pipelines` | script 04 sets the label on `spear-pipelines`; cross-namespace callers also need the SubjectAccessReview RBAC (script 04 grants pipeline runner, console backend and human users) |
| MCP tools/call "Bad request syntax" or 404s after install | mcp-gateway operator (v0.7.1 preview) regenerates its ext_proc EnvoyFilter with `request_body_mode: STREAMED`, which plain HTTP backends cannot parse | script 09 pauses the controller and patches both filters to BUFFERED; if you ever unpause the controller for maintenance, rerun script 09's patch step afterwards |
| console tighten-guardrails button returns 403 | the closed-loop patcher RBAC is missing | rerun script 08 (idempotent) |
| agent code changes not showing up in the sandbox | sandboxes seed their workspace PVC from the image exactly once; `oc patch` on the CR is accepted but never reconciles into the running pod | after `oc start-build <agent> --from-dir=agents --wait -n spear-shield-agents`, delete the sandbox's PVC and pod together (PVC first), then recreate; plain `oc delete pod` keeps the stale PVC |
| a provider refuses to delete | a provider attached to a sandbox refuses deletion even when the sandbox pod is gone | delete the sandboxes first |
| sandbox pods sitting Failed after a node reboot | the sandbox predates the create-time `restartPolicy: Always` patch | delete and recreate it via script 09 (idempotent) |
| SPIRE identity mismatch, A2A 401 from the relay path | Keycloak's `spear-coordinator-workload` client pins the sandbox SVID subject exactly | compare the ClusterSPIFFEID's pinned hash against the real pod label (script 09's closing output prints both commands) and patch `06-cluster-spiffeid.yaml` if they differ |
| route URLs 404 after a manual `oc apply` of the backend manifest | resolved route URLs live only in the deployment env (`oc set env` in script 08) | reapplying the bare manifest never wipes them, but a fresh install must run script 08 to set them |
| stale image served after a rebuild | `:latest` plus `imagePullPolicy: IfNotPresent` or the digest read mid-build | every custom deployment is digest-pinned by its install script; read the digest only after the build condition completes |
| OpenProject wiped | database lost | personas, project, work packages and API tokens are all reseeded by a single rerun of script 09 |

Deployment state versus Git: never assume running pods match HEAD, every code change needs its own build plus digest-pinned deploy cycle, console via script 08, agents via build plus sandbox cycle.

## Cleanup

```
./scripts/99-cleanup.sh            # asks for confirmation
./scripts/99-cleanup.sh --force    # no prompt
```

Deletes the eight namespaces created by the base chain in dependency order, sandboxes and gateways first, pgvector and minio last, plus the `spear-shield-agents` Keycloak realm import.

> Warning: `spear-shield-eval`, the optional evaluation layer's own namespace, is not touched by this script and is left standing if script 10 was ever run. Delete it by hand (`oc delete namespace spear-shield-eval`) for a full teardown.

Deliberately left alone otherwise: RHOAI itself, the DSC/DSCI, the odh-dashboard-config CR, the shared MLflow instance and all platform layer operators. Operator uninstall is a manual, cluster owner decision.

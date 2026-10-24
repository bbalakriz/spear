# SPEAR - End-to-End Flow Diagram

One question ("show my work packages" / "summarize the retention policy") traverses six
enforcement layers: sandbox isolation (runc / Kata), openshell egress policy, guardrails
(input, response, per-config), gateway auth (dual-issuer JWT), per-tool RBAC, and ABAC
row filtering. Split into five segments so each stays readable:

- [1. Ingress and identity minting](#1-ingress-and-identity-minting)
- [2. Coordinator agent sandbox (runc)](#2-coordinator-agent-sandbox-runc)
- [3. Retrieval agent sandbox (Kata) and the RAG data path](#3-retrieval-agent-sandbox-kata-and-the-rag-data-path)
- [4. Work-tracker MCP path with guardrails on every tool call](#4-work-tracker-mcp-path-with-guardrails-on-every-tool-call)
- [5. Evaluation loop and observability backplane](#5-evaluation-loop-and-observability-backplane)

Notation: `-->` request/response flow, `-.->` credential or config provisioned at deploy
time (dotted = not a runtime request path). All labels cite the file that implements/handles the hop.

---

## 1. Ingress and identity minting

The only user-facing door is the OpenShift console dynamic plugin. No client-supplied
username anywhere: the console proxy injects the signed-in user's own OAuth token, and the
backend resolves identity by a live apiserver self-lookup (`users/~`), never by trusting a
client. Identity minting is then a Keycloak password grant against the spear-agents
realm keyed on that verified username - the backend holds each persona's realm password on
file (`01-chat-creds.yaml`), a demo-only shortcut (no sso<->spear-agents federation
exists).

```mermaid
flowchart LR
    subgraph WB["Workstation / browser"]
        U(["Demo persona<br/>(balakrishnan.b or<br/>siddhartha.de)"])
    end

    subgraph OC["OpenShift console"]
        CP["Console plugin<br/>(SpearShieldChatPage etc.)"]
        BK["spear-console-backend<br/>(spear-console ns)"]
    end

    U -->|"openshift console UI<br/>(sso realm login)"| CP
    CP -->|"consoleFetchJSON<br/>/api/proxy/.../spear-shield/ask<br/>(console proxy injects the signed<br/>in user's OAuth token as Authorization)"| BK
```

```mermaid
flowchart LR
    BK["spear-console-backend"]
    AP["OpenShift apiserver<br/>(kubernetes.default.svc)"]
    KC[("Keycloak<br/>(sso + spear-agents<br/>realms, keycloak ns)")]
    PC[("spear-shield-console-creds secret<br/>(spear-console ns): one persona<br/>realm password file per username")]
    CO["Coordinator a2a<br/>endpoint via gateway"]

    BK -->|"1. GET /apis/user.openshift.io/v1/users/~<br/>with forwarded console token<br/>(openshift_username, server.py)"| AP
    AP -->|"resolved username<br/>(never client-supplied)"| BK
    BK -.->|"read that persona's realm password<br/>(persona_password, demo-only shortcut)"| PC
    BK -->|"2. POST password grant<br/>grant_type=password, client_id=spear-coordinator<br/>+ its client secret<br/>(mint_spear_shield_token, server.py)"| KC
    KC -->|"3. realm JWT,<br/>subject = real user"| BK
    BK -->|"4. same realm JWT sent twice:<br/>Authorization header (gateway validates)<br/>+ params.callerToken in body<br/>(header dies at relay hop)"| CO
```

Why the token rides twice: openshell's relay strips inbound `Authorization` headers on the
sandbox hop (deliberate anti-spoofing - a2a has no per-hop auth of its own). The gateway
validates the header copy; the coordinator's code reads the body copy.

---

## 2. Coordinator agent sandbox (runc)

Inside `spear-agents`, the coordinator runs in an openshell Sandbox CR
(`default--spear-coordinator`, runc - never Kata: its supervisor opens a CSI SPIFFE socket
a virtiofs mount inside a Kata guest cannot proxy). Three credentials converge here, then
the model loop runs with guardrails on the way in and the way out.

```mermaid
flowchart LR
    subgraph GW1["spear-a2a-gateway (istio, spear-agents)"]
        AUTH["AuthPolicy (Authorino/Kuadrant)<br/>dual issuer, fail-closed 401:<br/>keycloak realm jwt OR spire jwt-svid"]
    end

    subgraph SBX["openshell Sandbox CR default--spear-coordinator (runc)"]
        SUP["openshell supervisor sidecar<br/>+ nftables egress gate"]
        TG["token_grant provider<br/>(workload identity minting)"]
        AG["spear-coordinator-agent<br/>(model reasoning loop)"]
    end

    SP["SPIRE server<br/>(ztwim, cluster singleton,<br/>trust domain agent-pack.internal)"]
    KC2[("Keycloak spear-agents realm")]
    GR["NeMo Guardrails CR<br/>spear-guardrails<br/>(spear-guardrails ns)"]
    RT["Retrieval agent a2a<br/>endpoint via gateway"]

    AUTH -->|"host rewrite + ExternalName<br/>to openshell relay :8080<br/>(07-a2a-gateway.yaml)"| SUP
    SUP -->|"env + secrets +<br/>token_grant injection"| AG
    TG -.->|"spiffe jwt-svid mint:<br/>sub = spiffe://agent-pack.internal/ns/<br/>spear-agents/sandbox/&lt;name-hash&gt;<br/>pinned by keycloak client<br/>spear-coordinator-workload<br/>(federated-jwt)"| SP
    TG -.->|"workload access token<br/>(supervisor injects as outbound<br/>Authorization on coord->retr hop)"| KC2
    AG -->|"input guardrail check<br/>(coordinator-agent-config<br/>input rails)"| GR
    GR -->|"sanitized prompt"| AG
    AG -->|"model call, glm-53-flash via guardrails route<br/>(egress policy: guardrails :443 rest)"| GR
    GR -->|"tool-calling decision"| AG
    AG -->|"a2a delegate (params.callerToken +<br/>traceparent); outbound Authorization =<br/>workload identity (code never touches it)"| RT
```

The coordinator's LLM call never goes to the inference server directly: it goes through
the guardrails service's real route (`coordinator-policy.yaml` egress: guardrails :443),
where `coordinator-agent-config`'s input and output rails run, and that service forwards to
the real model. The retrieval hop's outbound `Authorization` is spoken for by workload
identity (injected by the supervisor), so the user JWT travels as `params.callerToken`
again - read without a local signature check (documented trade; the signature is verified
at the next enforcement point, the gateway's per-tool RBAC).

---

## 3. Retrieval agent sandbox (Kata) and the RAG data path

The retrieval agent runs in Sandbox CR `default--spear-retrieval` (Kata when a kata-oc node
has `/dev/kvm`, runc fallback). It reads `params.callerToken` only (errors -32001 if
absent), decodes the caller's clearance/dept/team claims, and takes one of two paths per
tool call. RAG path here; work-tracker path in segment 4.

```mermaid
flowchart LR
    subgraph GW2["spear-retrieval-a2a-gateway (istio)"]
        A2["same dual-issuer AuthPolicy"]
    end

    subgraph SBX2["openshell Sandbox CR default--spear-retrieval (kata/runc)"]
        SUP2["openshell supervisor + nftables gate"]
        AG2["spear-retrieval-agent<br/>(tool executor)"]
    end

    REL["rag-query-relay<br/>(spear-data ns, unsandboxed,<br/>same retrieval-agent image)"]
    PG[("pgvector postgres<br/>ragdb, rag_chunks table<br/>(spear-data ns)")]
    OGX["OGX server<br/>(spear-inference ns,<br/>spear-ogx)"]
    MF[("MLflow<br/>(redhat-ods-applications,<br/>shared cluster instance)")]

    A2 --> SUP2
    SUP2 --> AG2
    AG2 -->|"reads params.callerToken only,<br/>decode_claims -> clearance_level,<br/>dept, team (main.py:138)"| AG2
    AG2 -->|"rag_search: http post /search with<br/>caller_clearance / caller_dept / caller_team<br/>(egress policy: relay :8080 rest)"| REL
    REL -->|"embed_query via ogx /v1/embeddings<br/>nomic-embed-text-v1.5, 768 dims<br/>(cross-ns ingress on ogx netpol)"| OGX
    REL -->|"SELECT on rag_chunks as rag_reader<br/>(row-level security + ABAC filter:<br/>clearance + dept or team,<br/>never clearance alone<br/>- rag_query.py:87)"| PG
    REL -->|"filtered documents<br/>(caller + count + docs)"| AG2
    AG2 -.->|"in-process mlflow tracing<br/>(both agents, traceparent stitch,<br/>egress policy: mlflow :443)"| MF
```

ABAC is three dimensional by design: a chunk is visible only when its `clearance_level`
ranks at or below the caller and either the caller's `dept` or `team` matches (or the
chunk is `enterprise-wide`). `clearance_level` alone would wrongly grant cross dept
visibility. The relay connects as `rag_reader` (not the `raguser` superuser, which has BYPASSRLS and would
silently ignore the row-level security policy).

---

## 4. Work-tracker MCP path with guardrails on every tool call

For `search_work_packages` / `update_work_package`, the retrieval agent calls the MCP
gateway with the caller's real JWT as `Authorization` (its signature is finally verified
here by the per-tool RBAC AuthPolicy), then every request AND response is railed by the
guard proxy before any model or agent sees content.

```mermaid
flowchart LR
    AG2b["spear-retrieval-agent<br/>(caller JWT as Authorization,<br/>host = mcp-spear-agents...<br/>(egress policy: gateway :8080 rest))"]
    subgraph GW3["spear-mcp-gateway (istio, mcps listener)"]
        RBA["AuthPolicy per-tool RBAC:<br/>('tool:' + x-mcp-toolname) in<br/>resource_access[x-mcp-servername].roles<br/>401/403 fail-closed<br/>(02-mcp-gateway.yaml)"]
    end
    GP["spear-mcp-guard-proxy<br/>(spear-agents ns :8000)<br/>request guard + response guard,<br/>config work-tracker-config"]
    OM["spear-openproject-mcp<br/>(MCPServer CR, spear-worktracker ns)<br/>assignee=self check"]
    OP[("OpenProject<br/>data-governance project,<br/>work packages")]
    GRC["guardrails route<br/>spear-guardrails"]

    AG2b -->|"1. tools/call<br/>x-mcp-servername + x-mcp-toolname + x-mcp-method headers"| RBA
    RBA -->|"2. backendRef -> guard proxy<br/>(cross-ns ReferenceGrant)"| GP
    GP -->|"3. request guard:<br/>guardrail/checks on incoming args<br/>(blocks injected payloads)"| GRC
    GP -->|"4. forward (backend resolved from<br/>x-mcp-servername, server.py:126)"| OM
    OM -->|"5. decode caller JWT for username,<br/>then OpenProject REST with that<br/>persona's personal API token<br/>(Basic auth from spear-openproject-api-tokens,<br/>not the caller JWT as Bearer)"| OP
    OP -->|"6. tool response<br/>(work package subjects verbatim)"| OM
    OM -->|"7. response through proxy:<br/>guardrail/checks on response body<br/>(blocks injected subjects before<br/>any model sees them)"| GRC
    GP -->|"8. sanitized response<br/>(MCP isError: true on HTTP 200,<br/>not an HTTP 403)"| AG2b
    OM -.->|"assignee=self refusal:<br/>update_work_package by a caller<br/>who is not the assignee (real 403,<br/>same caller JWT for identity)"| AG2b
```

The guard proxy is one deployment for every MCP server (no per-server env), deriving each
request's real backend from `x-mcp-servername` — onboarding a new MCP server is a one-line
HTTPRoute backendRef change. Demo: `scripts/demo-tool-response-injection.sh` plants a
real malicious work package; the response guard blocks its subject line before the
retrieval agent's LLM ever sees it. The caller JWT authorizes the MCP gateway hop and
supplies the username for assignee=self; OpenProject itself is called with that user's
personal API token from `spear-openproject-api-tokens`.

---

## 5. Evaluation loop and observability backplane

Phase 3's evaluation layer (installed/submitted manually - `10-install-evalhub.sh`,
`11-submit-evalhub-jobs.sh`). Four providers score the same agent; results land in MLflow
under one parent cycle run, and the closed loop feeds the leak scanner's findings back
into guardrails config. Dotted = deploy-time or offline; solid = runtime path.

```mermaid
flowchart LR
    subgraph EH["EvalHub (spear-eval ns, bundled in rhods 3.5)"]
        RG["ragas-v062<br/>(4 reference-free metrics,<br/>dataset exported from real traces)"]
        IC["ibm-clear-v060<br/>(agentic eval over mlflow traces)"]
        GK["garak quick scan<br/>(straight at glm-53-flash)"]
        CD["spear-confidential-data<br/>(presidio + custom Northbridge<br/>figure recognizers over<br/>recorded answers)"]
    end

    MF2[("MLflow<br/>spear-shield-security-trace:<br/>agent traces<br/>spear-shield-evalhub-results:<br/>eval runs, one parent per cycle")]
    OGX2["OGX (spear-inference)<br/>judge: gemma4<br/>embed: nomic-embed-text-v1.5"]
    MINIO[("MinIO<br/>ragas dataset jsonl<br/>evalhub-ragas-data bucket")]
    GC["coordinator-agent-config<br/>configmap (spear-guardrails)<br/>regex patterns"]
    CB["spear-console-backend<br/>/trigger/closed-loop"]
    TP["Evaluation Trends page<br/>(console plugin TrendPage)"]

    RG -->|"judge + embedding calls"| OGX2
    IC -->|"litellm judge calls<br/>(dummy auth secret)"| OGX2
    GK -->|"probe generation"| OGX2
    CD -->|"no live agent call -<br/>mines recorded traces"| MF2
    RG -.->|"test_data_ref s3<br/>(minio-root-creds mirror)"| MINIO
    MF2 -.->|"trace source<br/>mlflow_traces_max_results=50"| IC
    MF2 -.->|"trace source (max_traces=50,<br/>fresh sa token + service-ca)"| CD
    CD -->|"leak scan results + leaked text<br/>tagged onto its own mlflow run<br/>(spear-confidential-data-provider/<br/>adapter.py)"| MF2
    TP -->|"closed-loop button posts<br/>latest leak job id<br/>(closed-loop RBAC applied<br/>by 08-install-console.sh)"| CB
    CB -.->|"reads leak detail from the<br/>provider's own mlflow run by job id,<br/>turns leaked text into regex patterns<br/>(server.py apply_closed_loop_patterns)"| MF2
    CB -->|"patches configmap in process:<br/>regex_detection input and output<br/>patterns plus output rails flows<br/>(get/patch Role on one named configmap)"| GC
    GC -.->|"patched config.yaml consumed on<br/>subsequent guardrail checks against<br/>coordinator-agent-config (kubelet<br/>configmap volume sync propagates;<br/>pod restart forces it)"| CB
```

No LLM judge for the closed loop: the backend reads the leak detail from the scanner's
own mlflow run (tagged there by the provider's adapter), converts the leaked text into
regex patterns (plus the scanner's own canonical rebate/volume patterns), and patches
`coordinator-agent-config` input and output `regex_detection` in process, enabling
output rails if missing. Output is what blocks a re ask of the same natural question;
input alone only matches if the figures appear in the user prompt.

---

## Segment index

| Segment | Hops | Enforcement shown |
|---|---|---|
| 1 | console -> plugin -> backend -> apiserver -> keycloak | apiserver self-lookup identity, password grant (demo-only persona password on file) |
| 2 | gateway -> sandbox -> guardrails -> retrieval | dual-issuer auth, spiffe sub pinning, input rails, sandbox egress |
| 3 | retrieval -> relay -> ogx/pgvector | ABAC (clearance + dept or team), RLS via rag_reader, trace export |
| 4 | retrieval -> gateway -> guard proxy -> openproject | per-tool RBAC, request + response guardrails, assignee=self |
| 5 | evalhub providers -> ogx/mlflow/minio, closed loop | eval cycle, leak scan -> guardrail patch |

Refer [README.md](README.md) for further details on build/deploy chain, pipeline ingestion + AutoRAG, platform operators layer, the openshell external CLI route.

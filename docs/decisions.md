# SPEAR decisions

Short record of choices that shaped the install chain and runtime. Code comments stay local and brief; the why lives here.

## Platform and operators

### D1. Manual OLM approval against pinned CSV
Approve only the InstallPlan that carries the pinned `startingCSV`, not the channel head via `status.installPlanRef`. Pins are never bumped to work around approval logic.

### D2. RHOAI dashboard flags after CRD establishment
`OdhDashboardConfig` is applied only after DSC/DSCI are Ready and the CRD is Established. Flags are always merge patched; a flagless operator created config is not left alone.

### D3. Kata optional, runc fallback
`INSTALL_KATA=false` skips the node reboot. Agents fall back to runc when no usable Kata node exists (for example cloud types without nested virt / `/dev/kvm`).

### D4. ZTWIM trust domain
Agent and Keycloak SPIFFE config pin the installed ZTWIM trust domain (`agent-pack.internal`), not a project invented domain.

## Namespaces and secrets

### D5. Namespace layout
Project namespaces live under `spear-*` (and `minio` for object storage). Names are defined once in `scripts/lib-common.sh`.

### D6. Cross namespace secret mirrors
A `secretKeyRef` only resolves in the pod namespace. Consumers that need another namespace's secret get a local copy via `mirror_secret` (for example `pgvector-creds`, `ogx-glm-key`).

### D7. EvalHub in its own namespace
EvalHub runs in `spear-eval`, not `redhat-ods-applications`, to avoid that namespace's restrictive NetworkPolicy.

## MLflow and pipelines

### D8. Single cluster MLflow instance
The operator manages one instance named `mlflow`; workloads land in `redhat-ods-applications` regardless of CR namespace. Secrets for MLflow are created there.

### D9. MLflow workspace header
API calls need namespace label `spear.io/mlflow-workspace` and header `X-MLflow-Workspace` naming the workspace (usually `spear-pipelines`). MLflow authorizes via SubjectAccessReview in that namespace.

### D10. EvalHub x-tenant and MLflow workspace
Job adapter pods force `MLFLOW_WORKSPACE` to the `X-Tenant` namespace. Jobs that read `spear-shield-security-trace` use `X-Tenant: spear-pipelines`. Jobs that do not (ragas, garak, confidential data) use `spear-eval` and need matching MLflow RBAC there too.

### D11. EvalHub reconcile nudge
Some operators only reconcile on CR change. After dependencies appear, annotate the CR (`nudge_reconcile`) rather than relying on a no-op reapply.

### D12. Stock vs custom EvalHub providers
Bundled ragas/ibm-clear v0.5.0 images are not registered. Custom configmaps pin ragas-v062, ibm-clear-v060, garak, and `spear-confidential-data`.

## Inference and guardrails

### D13. Separate model API keys
litemaas scopes keys per model. Chat and embedding keys are distinct; TEI embedding auth is a placeholder.

### D14. Restricted SCC probes
`oc run --overrides` with `containers[]` drops `--command`. Restricted probes embed command and full securityContext in the override JSON.

### D15. KFP dollar quotes
Kubelet expands `$$` in container args to `$`. Lightweight KFP components embed source as args, so PL/pgSQL uses tagged quotes (`$rls$`), never bare `$$`.

### D16. ABAC at two layers
Row filter in application SQL plus Postgres RLS on `rag_chunks` for `rag_reader` (no BYPASSRLS).

## Agents and console

### D17. Openshell managed labels
Sandbox pods use `openshell.ai/managed-by=openshell`. SPIFFE pod selectors use `agents.x-k8s.io/sandbox-name-hash` (hash of sandbox name).

### D18. Console identity
Backend resolves the signed in user via apiserver self lookup, then mints a Keycloak realm token. No client supplied username.

### D19. Corpus upload path
Documents land in MinIO via `seed-data/raw-intake/` plus `02-install-storage.sh`. Console buttons drive sanitize, AutoRAG, and apply pattern; CLI scripts are optional.

## Evaluation cycle

### D20. Judge model for ragas / CLEAR
Use `gemma4` as judge; `glm-53-flash` reasoning tokens overrun sidecar timeouts under ragas structured prompts.

### D21. Garak target
Garak hits the raw coordinator model (`glm-53-flash`) on OGX, not the agent or guardrails path.

### D22. Confidential data scan
Custom provider mines recorded traces for planted vendor figures; credentials travel as benchmark parameters, not `model.auth`.

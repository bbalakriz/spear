# Platform Setup Guide

One-time cluster level setup for SPEAR Shield, run before the project's own
install chain (`scripts/00-install-spear.sh`). Two scripts, both idempotent and
safe to rerun.

## Order

```
1. ./scripts/00-a-install-platform.sh    # base platform + operators (30-60 min)
2. ./scripts/00-b-install-openshell.sh   # openshell gateway relay (2-5 min)
3. ./scripts/00-install-spear.sh       # this project's own chain (30-60 min)
```

Overall deployment sequence: `00-a*` (platform), `00-b*` (openshell), then
`00-install-spear.sh` (the project chain). Each step is idempotent and safe
to rerun.

## What 00-a-install-platform.sh installs

Every cluster level prerequisite, from `manifests/platform/`:

| Component | From (manifests/platform/) | Notes |
|---|---|---|
| RHOAI 3.5 (operator, DSC, DSCI, dashboard config) | `platform/01-rhoai.yaml` | skipped if `default-dsc` already exists, only warns on missing dashboard flags instead of patching shared state |
| Operators: authorino, rhcl (kuadrant), ossm3/istio, mcp-gateway, rhbk (keycloak) | `00-operators.yaml` | all `installPlanApproval: Manual`, approved by `lib-olm.sh`'s guard: it refuses to approve any installplan that does not carry the pinned `startingCSV`, so a rerun never silently approves an olm-proposed upgrade |
| Red Hat agent sandbox operator | `00-operators.yaml` | replaces any upstream kubernetes-sigs controller/crd set first, verifies the crds expose v1beta1 and the controller image is not the upstream one |
| Istio control plane | `02-istio.yaml` | waits for both `istio` and `istiocni` to report Healthy |
| Authorino + Kuadrant instances | `03-authorino-kuadrant.yaml` | |
| Keycloak server | `04-keycloak-server.yaml` | db password generated once, never regenerated on rerun; serving-cert annotation + reencrypt route fixed up to whichever service the rhbk operator created |
| Zero trust workload identity manager (SPIRE) | `05-ztwim.yaml`, `07-spire.yaml` | `INSTALL_ZTWIM=false` to skip; spire agent/oidc-provider degrade to warnings, not failures |
| OpenShift sandboxed containers (Kata) | `06-sandboxed-containers.yaml` | `INSTALL_KATA=false` to skip; reboots every node in the labelled pool, expect the api server to drop and come back |
| Kata guest initramfs nftables patch | `07-kata-nftables-patch.yaml` | applied by `01-patch-kata-nftables.sh` at the end of the platform install |

Cluster knobs (env vars, all optional): `CLUSTER_DOMAIN` (read live from the
cluster if unset), `KEYCLOAK_HOSTNAME` (defaults to `sso.<domain>`),
`INSTALL_KATA` (default true), `INSTALL_ZTWIM` (default true).

The kata sequence is deliberately guarded: a KataConfig that is already fully
installed and healthy is never touched (a rerun once re-detected the pool,
flipped it to master, relabelled all control plane nodes and double-rebooted
both workers - that guard makes it impossible). Node labelling
(`node-role.kubernetes.io/kata-oc`) happens up front on the whole pool, before
the KataConfig cr exists.

## What 00-b-install-openshell.sh installs

The OpenShell gateway relay, the sandbox plumbing both demo agents run
through:

- helm chart `oci://ghcr.io/nvidia/openshell/helm-chart`, version 0.0.116
  (`OPENSHELL_CHART_VERSION` to override), release `openshell` in namespace
  `openshell`, configured with `manifests/platform/openshell/values-openshell-spear.yaml`
- sandbox namespace `spear-shield-agents` created + the
  `openshell-sandbox` service account granted `privileged` scc in it
- the gateway route from `manifests/platform/openshell/route.yaml`

The a2a gateway HTTPRoutes in the project chain later point their backendRef
at this release via the `spear-shield-openshell-a2a-backend` ExternalName.

## Why platform first

The project chain (`00-install-spear.sh`) applies `KeycloakRealmImport`,
`NemoGuardrails`, `Sandbox` claims, `AuthPolicy`s and istio `Gateway`
objects - none of those CRDs exist until this platform setup has installed
their operators. Running the chain first fails on missing CRDs, the same
sequencing bug the guardrails installer already hit once (`ensure_early_gateway`
in `07-install-guardrails.sh` exists precisely because a later script created
a Gateway a earlier one needed).

## Verification

```
./scripts/90-verify.sh
```

after all three steps, or the platform's own `40-verify.sh` in the reference
repo for platform-only checks.

## Cleanup

Platform operators and CRs are deliberately NOT torn down by
`scripts/99-cleanup.sh` (that only removes this project's own namespaces) -
operator uninstall is a manual, cluster owner decision.

#!/usr/bin/env bash
set -euo pipefail

# standalone, safely re-runnable fix for the kata guest kernel's missing
# nf_tables modules, see manifests/platform/07-kata-nftables-patch.yaml
# for the full story and the upstream source this was adapted from. called
# once from scripts/00-a-install-platform.sh on a fresh install, but the
# patch lives in the node's initramfs on disk and gets wiped by
# kata-osbuilder-generate on the next node reboot or KataConfig change, so
# this is kept as its own script specifically so it can be re-run on its
# own any time that happens, without re-running the whole platform install.
#
# idempotent end to end: the patch daemonset's own script checks for an
# already-patched initrd and exits cleanly, and this script only grants the
# privileged scc for as long as the daemonset needs it.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NS="openshift-sandboxed-containers-operator"
SA="kata-install"

log() { printf '== %s ==\n' "$*"; }

main() {
  command -v oc >/dev/null 2>&1 || { echo "oc is required" >&2; exit 1; }

  if ! oc get runtimeclass kata >/dev/null 2>&1; then
    echo "no 'kata' runtimeclass on this cluster yet, run scripts/00-a-install-platform.sh first" >&2
    exit 1
  fi

  log "granting kata-install service account privileged scc, needed to patch the host's initramfs"
  oc adm policy add-scc-to-user privileged -z "${SA}" -n "${NS}"

  log "applying kata nftables patch daemonset"
  oc apply -f "${ROOT_DIR}/manifests/platform/07-kata-nftables-patch.yaml"

  log "waiting for patch pods to start"
  oc rollout status daemonset/kata-nftables-patch -n "${NS}" --timeout=180s

  # rollout status only confirms the container started, not that the patch
  # itself finished, unpacking/repacking a ~35mb initrd takes a bit longer
  # than that. wait for the script's own final log line instead, from
  # every pod, not just any one of them, one pod reporting complete while
  # another is still working looked like success on a single node cluster
  # but silently missed nodes on a multi node one. confirmed live this can
  # take well past 300s if it lands right after a kata reboot's own api
  # server disruption settles, tries bumped up accordingly.
  log "waiting for the patch script to finish (unpacking/repacking the initrd)"
  local desired tries=180
  desired=$(oc get daemonset kata-nftables-patch -n "${NS}" -o jsonpath='{.status.desiredNumberScheduled}')
  while (( tries > 0 )); do
    local done_count
    done_count=$(oc logs -n "${NS}" -l app=kata-nftables-patch --tail=5 --prefix 2>/dev/null | grep -c "Patch complete" || true)
    if (( done_count >= desired )); then
      break
    fi
    sleep 5
    tries=$((tries - 1))
  done
  if (( tries == 0 )); then
    echo "patch script did not report completion on all ${desired} nodes in time, check logs before trusting the result:" >&2
    echo "  oc logs -n ${NS} -l app=kata-nftables-patch --prefix" >&2
    exit 1
  fi

  log "patch pod logs"
  local logs
  logs="$(oc logs -n "${NS}" -l app=kata-nftables-patch --tail=30 --prefix 2>/dev/null || true)"
  printf '%s\n' "${logs}"

  # "Patch complete" is printed even when a node skipped because it has no
  # guest image (control planes wrongly in kata-oc). require at least one
  # real initrd touch, or an already-patched skip, before calling success.
  local skipped patched already
  skipped=$(grep -c 'no kata guest image, skipping' <<<"${logs}" || true)
  patched=$(grep -c 'Initramfs patched with nf_tables' <<<"${logs}" || true)
  already=$(grep -c 'nf_tables already present in initramfs' <<<"${logs}" || true)
  if (( patched + already == 0 )); then
    echo "no kata guest initrd was patched on any node (${skipped} skipped with no image). check that only installed kata-oc workers carry the label:" >&2
    echo "  oc get nodes -l node-role.kubernetes.io/kata-oc" >&2
    echo "  oc get kataconfig example-kataconfig -o jsonpath='{.status}'" >&2
    exit 1
  fi
  log "patched=${patched} already=${already} skipped_no_image=${skipped}"

  log "cleaning up patch daemonset and revoking privileged scc"
  oc delete -f "${ROOT_DIR}/manifests/platform/07-kata-nftables-patch.yaml" --ignore-not-found=true
  oc adm policy remove-scc-from-user privileged -z "${SA}" -n "${NS}"

  log "kata nftables patch complete"
}

main "$@"

#!/usr/bin/env bash
# shared by 00-a-install-platform.sh, not meant to run on its own. every
# Subscription in this repo pins installPlanApproval: Manual, olm creates
# an InstallPlan sitting in RequiresApproval and never moves past it on
# its own, so every operator install needs the same "find the plan,
# approve it once, wait for the pinned csv to succeed" sequence. kept in
# one place so the approval logic can't drift between call sites.

# resolves a Subscription's install plan and approves it if it is not
# already approved. idempotent, an already approved plan is left alone.
#
# olm keeps proposing new install plans for whatever the channel's current
# head is even under installPlanApproval: Manual, that setting only stops
# olm auto approving them, it does not stop it from generating them. an
# earlier version of this function approved whatever plan a subscription
# was currently pointed at with no regard for which csv it targeted, so a
# second run of the same install script after olm had proposed a newer
# plan silently approved the upgrade instead of the pinned startingCSV,
# confirmed live: rhbk-operator drifted from the pinned v26.4.15-opr.1 to
# v26.4.16-opr.1 this way, two install plans existed, both approved, one
# for each csv. refusing to approve a plan that does not carry the pinned
# csv closes that gap, bumping the pin is a deliberate edit to the
# manifest, never an accidental side effect of rerunning the script.
approve_install_plan() {
  local name="$1" ns="$2" tries=60 plan="" starting_csv="" plan_csvs=""
  starting_csv=$(oc get subscription "${name}" -n "${ns}" -o jsonpath='{.spec.startingCSV}' 2>/dev/null || true)
  while (( tries > 0 )); do
    plan=$(oc get subscription "${name}" -n "${ns}" -o jsonpath='{.status.installPlanRef.name}' 2>/dev/null || true)
    if [[ -n "${plan}" ]]; then
      plan_csvs=$(oc get installplan "${plan}" -n "${ns}" -o jsonpath='{.spec.clusterServiceVersionNames}' 2>/dev/null || true)
      if [[ -n "${starting_csv}" && "${plan_csvs}" != *"${starting_csv}"* ]]; then
        echo "refusing to approve install plan ${plan} for ${name} in ${ns}: it targets ${plan_csvs}, not the pinned startingCSV ${starting_csv}." >&2
        echo "olm proposed a channel upgrade on its own, this project never approves one of those unattended, bump startingCSV in the manifest yourself if that upgrade is wanted." >&2
        return 1
      fi
      if [[ "$(oc get installplan "${plan}" -n "${ns}" -o jsonpath='{.spec.approved}' 2>/dev/null)" != "true" ]]; then
        printf '== approving install plan %s for %s ==\n' "${plan}" "${name}"
        oc patch installplan "${plan}" -n "${ns}" --type merge -p '{"spec":{"approved":true}}' >/dev/null
      fi
      return 0
    fi
    sleep 5
    tries=$((tries - 1))
  done
  echo "subscription ${name} in ${ns} never produced an install plan to approve" >&2
  return 1
}

# resolves a Subscription's installed csv name, then waits for that exact
# csv to report Succeeded. avoids hardcoding a version, since the pinned
# startingCSV lives on the Subscription itself, not here.
wait_subscription_succeeded() {
  local name="$1" ns="$2" tries=90 csv=""
  while (( tries > 0 )); do
    csv=$(oc get subscription "${name}" -n "${ns}" -o jsonpath='{.status.installedCSV}' 2>/dev/null || true)
    if [[ -n "${csv}" ]]; then
      if oc get csv "${csv}" -n "${ns}" -o jsonpath='{.status.phase}' 2>/dev/null | grep -qx Succeeded; then
        printf '== %s -> %s succeeded ==\n' "${name}" "${csv}"
        return 0
      fi
    fi
    sleep 5
    tries=$((tries - 1))
  done
  echo "subscription ${name} in ${ns} did not reach a succeeded csv in time" >&2
  return 1
}

# the one call site both scripts actually use: approve whatever install
# plan is pending, then wait for the csv it produces to succeed.
approve_and_wait_subscription() {
  local name="$1" ns="$2"
  approve_install_plan "${name}" "${ns}"
  wait_subscription_succeeded "${name}" "${ns}"
}

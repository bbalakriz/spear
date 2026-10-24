#!/usr/bin/env bash
# olm manual install plan approval for 00-a-install-platform.sh

# find an InstallPlan in ns whose clusterServiceVersionNames list
# contains csv. subscription.status.installPlanRef always tracks the
# channel head, even under Manual, so the pinned startingCSV's plan is
# often a different object once olm has also proposed an upgrade.
find_install_plan_for_csv() {
  local ns="$1" csv="$2"
  oc get installplan -n "${ns}" -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.clusterServiceVersionNames}{"\n"}{end}' 2>/dev/null \
    | awk -v csv="${csv}" '$0 ~ csv { print $1; exit }'
}

# resolves a Subscription's install plan and approves it if it is not
# already approved. idempotent, an already approved plan is left alone.
#
# only approve the install plan that carries spec.startingCSV, not channel head
approve_install_plan() {
  local name="$1" ns="$2" tries=60 plan="" starting_csv="" plan_csvs="" installed_csv=""
  starting_csv=$(oc get subscription "${name}" -n "${ns}" -o jsonpath='{.spec.startingCSV}' 2>/dev/null || true)
  while (( tries > 0 )); do
    # already sitting on the pin: leave the channel-head upgrade plan
    # (status.installPlanRef) unapproved and move on.
    installed_csv=$(oc get subscription "${name}" -n "${ns}" -o jsonpath='{.status.installedCSV}' 2>/dev/null || true)
    if [[ -n "${starting_csv}" && "${installed_csv}" == "${starting_csv}" ]]; then
      return 0
    fi

    plan=""
    if [[ -n "${starting_csv}" ]]; then
      plan=$(find_install_plan_for_csv "${ns}" "${starting_csv}")
    fi
    if [[ -z "${plan}" ]]; then
      plan=$(oc get subscription "${name}" -n "${ns}" -o jsonpath='{.status.installPlanRef.name}' 2>/dev/null || true)
    fi
    if [[ -n "${plan}" ]]; then
      plan_csvs=$(oc get installplan "${plan}" -n "${ns}" -o jsonpath='{.spec.clusterServiceVersionNames}' 2>/dev/null || true)
      if [[ -n "${starting_csv}" && "${plan_csvs}" != *"${starting_csv}"* ]]; then
        # channel-head plan only, keep waiting for one that carries the pin
        sleep 5
        tries=$((tries - 1))
        continue
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
  if [[ -n "${starting_csv}" ]]; then
    echo "subscription ${name} in ${ns} never produced an install plan for pinned startingCSV ${starting_csv}" >&2
  else
    echo "subscription ${name} in ${ns} never produced an install plan to approve" >&2
  fi
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

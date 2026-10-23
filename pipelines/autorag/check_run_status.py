"""prints the current state of a given kfp run, same auth pattern as
run_autorag.py (the caller's own `oc whoami -t` token), a quick one shot
check rather than a blocking wait.

usage: source .venv-kfp/bin/activate && python check_run_status.py <run_id>
"""
import subprocess
import sys

from kfp.client import Client


def current_token() -> str:
    return subprocess.run(
        ["oc", "whoami", "-t"], check=True, capture_output=True, text=True
    ).stdout.strip()


def dspa_route() -> str:
    # resolved live, this route hostname is unique per cluster, never hardcode it
    host = subprocess.run(
        ["oc", "get", "route", "ds-pipeline-rag-phase1-dspa", "-n", "spear-pipelines",
         "-o", "jsonpath={.spec.host}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return f"https://{host}"


def main() -> None:
    run_id = sys.argv[1]
    client = Client(host=dspa_route(), existing_token=current_token())
    run = client.get_run(run_id)
    print(f"state: {run.state}")


if __name__ == "__main__":
    main()

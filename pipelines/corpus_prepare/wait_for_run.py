"""waits for a given kfp run to finish, same auth pattern as
compile_and_run.py (the caller's own `oc whoami -t` token).

usage: source .venv-kfp/bin/activate && python wait_for_run.py <run_id> [timeout_seconds]
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
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 600

    client = Client(host=dspa_route(), existing_token=current_token())
    run = client.wait_for_run_completion(run_id, timeout=timeout)
    print(f"final state: {run.state}")


if __name__ == "__main__":
    main()

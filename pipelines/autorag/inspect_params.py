"""prints the exact top level input parameter names of
documents-rag-optimization-pipeline as registered on this dspa, same
auth pattern as run_autorag.py and compile_and_run.py.
"""
import subprocess

from kfp.client import Client

# one off debug ids for this cluster's already uploaded pipeline version,
# this script is an inspection utility not part of the deployable app
PIPELINE_ID = "b6c7fd13-84d2-4317-88fc-9685938bdacc"
VERSION_ID = "1ef33e18-a19e-4206-86d3-5a1358d1dd1e"


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
    client = Client(host=dspa_route(), existing_token=current_token())
    version = client.get_pipeline_version(PIPELINE_ID, VERSION_ID)
    outer = version.pipeline_spec or {}
    spec = outer.get("pipeline_spec", outer)
    root = spec.get("root", {})
    print("root keys:", list(root.keys()))
    input_defs = root.get("inputDefinitions", {}).get("parameters", {})
    print("num params found:", len(input_defs))
    for name, defn in input_defs.items():
        print(name, "->", defn)


if __name__ == "__main__":
    main()

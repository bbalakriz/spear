"""entry point for running ConfidentialDataAdapter as a k8s job, same
shape confirmed from the real, already shipped garak provider's own
main(): reads JobSpec from the mounted configmap, runs the scan, reports
through the sidecar.
"""

from __future__ import annotations

import logging
import os
import sys

from evalhub.adapter import DefaultCallbacks

from adapter import ConfidentialDataAdapter

logger = logging.getLogger(__name__)


def main() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    try:
        job_spec_path = os.getenv("EVALHUB_JOB_SPEC_PATH", "/meta/job.json")
        adapter = ConfidentialDataAdapter(job_spec_path=job_spec_path)
        logger.info("loaded job %s", adapter.job_spec.id)
        logger.info("benchmark: %s", adapter.job_spec.benchmark_id)
        logger.info("model: %s", adapter.job_spec.model.name)

        callbacks = DefaultCallbacks.from_adapter(adapter)
        results = adapter.run_benchmark_job(adapter.job_spec, callbacks)
        logger.info("job completed: %s, total leak count: %s", results.id, results.overall_score)

        callbacks.report_results(results)
        sys.exit(0)

    except FileNotFoundError as exc:
        logger.exception("job spec not found: %s", exc)
        sys.exit(1)
    except ValueError as exc:
        logger.exception("configuration error: %s", exc)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - last resort, a job must exit non zero on any failure
        logger.exception("job failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""mlflow tracing for both sandboxed agents alongside each _trace_step list.

uses RequestHeaderProvider for X-MLflow-Workspace, traceparent on a2a hops,
and degrades to passthrough when MLFLOW_URL is unset or configure fails.
"""

from __future__ import annotations

import contextlib
import functools
import os
from typing import Any

MLFLOW_URL = os.environ.get("MLFLOW_URL", "")
MLFLOW_WORKSPACE = os.environ.get("MLFLOW_WORKSPACE", "spear-pipelines")
MLFLOW_EXPERIMENT = os.environ.get("MLFLOW_EXPERIMENT", "spear-shield-security-trace")

# plain span_type strings so agents need no mlflow import when tracing is off
SPAN_TYPE_AGENT = "AGENT"
# CHAT_MODEL not LLM: clear/ragas trace parsers only treat CHAT_MODEL as model spans
SPAN_TYPE_LLM = "CHAT_MODEL"
SPAN_TYPE_RETRIEVER = "RETRIEVER"
SPAN_TYPE_TOOL = "TOOL"

_enabled = False
_mlflow: Any = None


def configure() -> bool:
    """call once at process start. returns whether tracing actually came
    up, callers do not need to check this themselves, trace() already
    degrades to a plain passthrough on its own either way.
    """
    global _enabled, _mlflow
    if not MLFLOW_URL:
        print("mlflow_tracing: MLFLOW_URL not set, tracing stays off")
        return False
    # coordinator and retrieval are two processes flushing one shared
    # trace; async export can let a late child span arrive after the
    # root has already closed. sync export writes each span durably
    # before its function returns, no ordering race across the hop
    os.environ.setdefault("MLFLOW_ENABLE_ASYNC_TRACE_LOGGING", "false")
    try:
        import mlflow
        from mlflow.tracking.request_header.abstract_request_header_provider import (
            RequestHeaderProvider,
        )
        from mlflow.tracking.request_header.registry import (
            _request_header_provider_registry,
        )

        # X-MLflow-Workspace required on this dspa install (400 without it)
        class _SpearShieldWorkspaceHeader(RequestHeaderProvider):
            def in_context(self) -> bool:
                return True

            def request_headers(self) -> dict[str, str]:
                return {"X-MLflow-Workspace": MLFLOW_WORKSPACE}

        _request_header_provider_registry.register(_SpearShieldWorkspaceHeader)
        mlflow.set_tracking_uri(MLFLOW_URL)
        mlflow.set_experiment(MLFLOW_EXPERIMENT)
        _mlflow = mlflow
        _enabled = True
        print(f"mlflow_tracing: on, url={MLFLOW_URL} workspace={MLFLOW_WORKSPACE}")
        return True
    except Exception as exc:  # noqa: BLE001
        # a telemetry failure never takes the real request down with it
        print(f"mlflow_tracing: failed to configure, tracing stays off: {exc}")
        _enabled = False
        return False


def trace(name: str, span_type: str = "UNKNOWN"):
    """no-op safe @mlflow.trace: wraps the function in one real mlflow
    span, inputs/outputs auto captured from bound arguments and return
    value, plain passthrough when tracing is off.

    span_type is one of the SPAN_TYPE_* constants, so evalhub's
    ragas/clear scoring can tell a retrieval span's contexts apart from
    an llm span's prompt and completion later.

    a bearer token must never sit in a wrapped argument, mlflow has no
    per argument redaction; callers holding one close over it in a
    nested function instead, see run_rag_search's callers.

    _enabled is checked lazily inside wrapper(), not here: this
    decorator runs at import time, before configure() ever runs
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if not _enabled:
                return func(*args, **kwargs)
            # only the wrapping step is guarded; a real exception from
            # the function body must still propagate exactly once,
            # retrying unwrapped would mean calling a real http hop twice
            try:
                traced = _mlflow.trace(name=name, span_type=span_type)(func)
            except Exception as exc:  # noqa: BLE001
                print(f"mlflow_tracing: trace '{name}' failed to wrap, continuing without it: {exc}")
                return func(*args, **kwargs)
            return traced(*args, **kwargs)

        return wrapper

    return decorator


def current_span():
    """the active span inside a function wrapped by trace() above, or
    None if tracing is off. for the handful of spans that need one
    extra attribute set on top of what the decorator already captures.
    """
    if not _enabled:
        return None
    try:
        return _mlflow.get_current_active_span()
    except Exception as exc:  # noqa: BLE001
        print(f"mlflow_tracing: could not get the current span: {exc}")
        return None


def outgoing_trace_headers() -> dict[str, str]:
    """traceparent header for the coordinator's outbound a2a call to
    spear-retrieval-agent. empty when tracing is off.
    """
    if not _enabled:
        return {}
    try:
        from mlflow.tracing import get_tracing_context_headers_for_http_request

        return dict(get_tracing_context_headers_for_http_request())
    except Exception as exc:  # noqa: BLE001
        print(f"mlflow_tracing: could not build outgoing trace headers: {exc}")
        return {}


@contextlib.contextmanager
def continue_trace_from_headers(headers: dict[str, str]):
    """retrieval agent side of the same hop: picks the inbound
    traceparent back up so its spans land as children of the
    coordinator's delegating span instead of a second, disconnected trace.
    """
    if not _enabled:
        yield
        return
    # generator may only yield once; setup below can fail quietly, but
    # the caller's own body must still propagate normally
    try:
        from mlflow.tracing import set_tracing_context_from_http_request_headers

        ctx_cm = set_tracing_context_from_http_request_headers(headers)
        ctx_cm.__enter__()
    except Exception as exc:  # noqa: BLE001
        print(f"mlflow_tracing: could not continue trace from headers: {exc}")
        yield
        return
    try:
        yield
    except BaseException as exc:
        ctx_cm.__exit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        ctx_cm.__exit__(None, None, None)

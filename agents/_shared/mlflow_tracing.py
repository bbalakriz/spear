#!/usr/bin/env python3
"""real, durable mlflow tracing for both sandboxed agents, PHASE3_PLAN.md
section 4. lives alongside, not instead of, each agent's own existing
_trace_step list, that one keeps feeding the chat page's synchronous
box and arrow view exactly as before, this one gives every request a
real, queryable, nested trace in mlflow that survives past the one
http response it used to be stuck in.

three things make this possible without hand rolling any rest calls,
all confirmed live against the real installed server before writing
this, not assumed:

- the custom X-MLflow-Workspace header this dspa install needs on
  every call is a real, documented mlflow extension point,
  RequestHeaderProvider, not something to bolt onto every call by hand
- the rbac this needs is get/list/create/update on the mlflow.kubeflow.org
  experiments resource in spear-pipelines, not the generic "get services"
  shape OGX_API_KEY's own guardrails caller token uses
- the coordinator to retrieval a2a hop stitches into one real trace
  through a single standard traceparent header, mlflow's own
  distributed tracing helpers read and write it, nothing to invent

configure() is safe to call even when MLFLOW_URL is unset or the
server is unreachable, tracing just stays off and every trace() call
below becomes a plain passthrough, the real request never fails
because telemetry did.
"""

from __future__ import annotations

import contextlib
import functools
import os
from typing import Any

MLFLOW_URL = os.environ.get("MLFLOW_URL", "")
MLFLOW_WORKSPACE = os.environ.get("MLFLOW_WORKSPACE", "spear-pipelines")
MLFLOW_EXPERIMENT = os.environ.get("MLFLOW_EXPERIMENT", "spear-shield-security-trace")

# plain strings, not mlflow.entities.SpanType itself: main.py in both
# agents imports this module and nothing else from mlflow, the whole
# point of trace() degrading to a plain passthrough is that neither
# agent needs a real mlflow import on the disabled path. confirmed live
# against the pinned mlflow-skinny==3.16.1 that start_span's own
# span_type parameter just takes a plain string, these are the same
# values SpanType's real members hold
SPAN_TYPE_AGENT = "AGENT"
# CHAT_MODEL, not the more generic LLM: confirmed live against a real
# ibm-clear evalhub job, its own mlflow trace parser only ever treats
# CHAT_MODEL/MODEL/GENERATION as an actual model call span, LLM is a
# real mlflow.entities.SpanType member too but every trace tagged with
# it came back "no LLM calls found" from clear's own preprocessor, 9 for 9
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
    # the coordinator and retrieval sandboxes are two separate processes
    # on two ends of a real network hop, each only ever flushing its own
    # half of one shared trace. mlflow's default async export batches
    # spans in a background thread on its own timer, so retrieval's
    # child span can genuinely reach the server after the coordinator's
    # own root span has already closed that trace out, and the late
    # arrival gets dropped, confirmed live this is exactly what was
    # happening across repeated real runs. forcing synchronous export
    # means a span is durably written before the function that created
    # it ever returns, no ordering race left between the two sides of
    # the hop
    os.environ.setdefault("MLFLOW_ENABLE_ASYNC_TRACE_LOGGING", "false")
    try:
        import mlflow
        from mlflow.tracking.request_header.abstract_request_header_provider import (
            RequestHeaderProvider,
        )
        from mlflow.tracking.request_header.registry import (
            _request_header_provider_registry,
        )

        # the one header every real call against this dspa install needs,
        # confirmed live, a plain search without it comes back 400
        # "workspace context is required for this request"
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
        # a telemetry failure never takes the real request down with it,
        # same fallback discipline owner-console-backend's own
        # ingestion_source_config already uses
        print(f"mlflow_tracing: failed to configure, tracing stays off: {exc}")
        _enabled = False
        return False


def trace(name: str, span_type: str = "UNKNOWN"):
    """no-op safe @mlflow.trace: wraps the whole decorated function in
    one real mlflow span, inputs and outputs auto captured straight
    from the function's own bound arguments and return value, a plain
    passthrough otherwise, so main.py never has to branch on _enabled
    itself. this is the recommended shape for custom code per mlflow's
    own shipped tracing guide.

    span_type is one of the SPAN_TYPE_* constants above, left as
    "UNKNOWN" (mlflow's own default) for spans that are not an agent
    step, a model call, or a tool/retriever hop, this is what lets
    evalhub's own ragas/clear scoring tell a retrieval span's contexts
    apart from a plain llm span's prompt and completion later, rather
    than every span in the tree looking the same shape.

    a bearer token must never sit in a function argument this wraps,
    mlflow has no per argument redaction, a caller holding one closes
    over it in a nested function instead, see run_rag_search's own
    callers for the shape.

    checked lazily inside wrapper(), not here: a decorator runs at
    import time, before main() ever calls configure(), so _enabled is
    always still false at the moment python evaluates this one
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if not _enabled:
                return func(*args, **kwargs)
            # only the wrapping step itself is guarded, never the real
            # call below: a telemetry bug here must fall back to the
            # plain function, but a real exception from the function's
            # own body still has to propagate exactly once, retrying it
            # unwrapped would mean calling a real http hop twice
            try:
                traced = _mlflow.trace(name=name, span_type=span_type)(func)
            except Exception as exc:  # noqa: BLE001
                print(f"mlflow_tracing: trace '{name}' failed to wrap, continuing without it: {exc}")
                return func(*args, **kwargs)
            return traced(*args, **kwargs)

        return wrapper

    return decorator


def current_span():
    """the real active span inside a function wrapped by trace() above,
    or None if tracing is off, same no-op contract everywhere else in
    this module. for the handful of spans that need one branch specific
    attribute, a category on only one of several outcomes a function
    can return, set on top of what the decorator already auto captures.
    """
    if not _enabled:
        return None
    try:
        return _mlflow.get_current_active_span()
    except Exception as exc:  # noqa: BLE001
        print(f"mlflow_tracing: could not get the current span: {exc}")
        return None


def outgoing_trace_headers() -> dict[str, str]:
    """the real traceparent header for the coordinator's own outbound
    a2a call to spear-retrieval-agent, confirmed live this is the only
    header the distributed tracing helper actually needs to hand over.
    empty when tracing is off, nothing for the caller to merge in then.
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
    """retrieval agent side of the same hop: picks the inbound traceparent
    back up so its own spans land as real children of the coordinator's
    delegating span instead of starting a second, disconnected trace.
    a plain no-op if tracing is off or the header never arrived.
    """
    if not _enabled:
        yield
        return
    # a contextlib generator may only ever yield once, confirmed live the
    # hard way, "generator didn't stop after throw()", only the setup
    # below is allowed to fail quietly, the caller's own body must
    # propagate normally
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

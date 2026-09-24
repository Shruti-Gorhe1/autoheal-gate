"""Tracing and metrics for the AutoHeal execution path.

OpenTelemetry is optional. When Phoenix is enabled, AutoHeal emits local
Phoenix-compatible OTLP traces. When it is disabled or unavailable, all
helpers degrade to safe no-ops.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.config import settings
from app.security.guards import redact_secrets, safe_error

logger = logging.getLogger("autoheal.telemetry")

_tracer: Any = None
_enabled = False


class _NoopSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def record_exception(self, exc: BaseException) -> None:
        return None

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        return None

    def get_span_context(self):
        return None


@contextmanager
def _noop_span(name: str, **attributes: Any) -> Iterator[Any]:
    started = time.perf_counter()
    try:
        yield _NoopSpan()
    finally:
        logger.debug("%s finished in %.1f ms", name, (time.perf_counter() - started) * 1000)


def setup_telemetry() -> None:
    """Configure tracing once at startup. Never raises."""
    global _tracer, _enabled

    if not settings.phoenix_enabled:
        logger.info("Tracing is disabled. Set PHOENIX_ENABLED=true to turn it on.")
        _tracer = None
        _enabled = False
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )

        resource = Resource.create(
            {
                "service.name": settings.app_name,
                "service.version": settings.app_version,
                "deployment.environment": settings.app_env,
                "openinference.project.name": settings.phoenix_project_name,
            }
        )
        provider = TracerProvider(resource=resource)

        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.phoenix_endpoint))
            )
            logger.info("Exporting traces to %s", settings.phoenix_endpoint)
        except Exception as exc:
            logger.warning("OTLP exporter unavailable: %s", exc)

        if settings.otel_console_exporter:
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(settings.app_name)
        _enabled = True
    except Exception as exc:  # pragma: no cover - optional dependency path
        logger.warning("Tracing could not start (%s). Continuing without it.", exc)
        _tracer = None
        _enabled = False


def _span_kind(name: str) -> str:
    if name == "llm-call":
        return "LLM"
    if name in {"retrieval", "context.source", "context.cicd"}:
        return "RETRIEVER"
    if name in {"webhook.receive", "event.ingestion"}:
        return "TOOL"
    return "CHAIN"


@contextmanager
def agent_span(name: str, **attributes: Any) -> Iterator[Any]:
    """Trace a unit of agent work."""
    with traced_span(name, **attributes) as span:
        yield span


@contextmanager
def traced_span(
    name: str,
    *,
    traceparent: str | None = None,
    **attributes: Any,
) -> Iterator[Any]:
    """Create an AutoHeal span, optionally continuing a propagated trace.

    ``traceparent`` is carried through the durable queue so a webhook trace can
    be continued by a worker in another process. If tracing is disabled the
    helper remains a no-op.
    """
    started = time.perf_counter()
    if not _enabled or _tracer is None:
        try:
            yield _NoopSpan()
        finally:
            logger.debug("%s finished in %.1f ms", name, (time.perf_counter() - started) * 1000)
        return

    try:
        parent_context = None
        if traceparent:
            from opentelemetry import propagate
            parent_context = propagate.extract({"traceparent": traceparent})
        with _tracer.start_as_current_span(
            name,
            context=parent_context,
        ) as span:
            span.set_attribute("openinference.span.kind", _span_kind(name))
            for key, value in redact_secrets(attributes).items():
                if value is not None:
                    span.set_attribute(key, str(value))
            try:
                yield span
            except Exception as exc:
                span.record_exception(exc)
                raise
            finally:
                span.set_attribute(
                    "duration_ms", round((time.perf_counter() - started) * 1000, 2)
                )
    except Exception:
        # Instrumentation must never break AutoHeal itself.
        logger.error("Tracing span %s failed: %s", name, safe_error(Exception("instrumentation failure")))
        yield _NoopSpan()


def current_trace_id() -> str | None:
    if not _enabled:
        return None
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if context and context.trace_id:
            return format(context.trace_id, "032x")
    except Exception:
        return None
    return None


def current_traceparent() -> str | None:
    """Return the W3C traceparent for propagation across queue/process boundaries."""
    if not _enabled:
        return None
    try:
        from opentelemetry import propagate

        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        return carrier.get("traceparent")
    except Exception:
        return None


def status() -> dict[str, Any]:
    return {
        "enabled": _enabled,
        "endpoint": settings.phoenix_endpoint if _enabled else None,
        "project": settings.phoenix_project_name if _enabled else None,
        "ui": "http://localhost:6006" if _enabled else None,
    }

from __future__ import annotations

from contextlib import contextmanager

import pytest

import app.observability.telemetry as telemetry


def test_tracing_status_is_disabled_by_default():
    assert telemetry.status()["enabled"] is False
    assert telemetry.current_trace_id() is None
    assert telemetry.current_traceparent() is None


def test_traced_span_is_safe_when_disabled():
    with telemetry.traced_span("test.span", **{"job.id": "job-1"}) as span:
        span.set_attribute("test.value", "ok")
        assert span.get_span_context() is None


def test_agent_span_alias_uses_same_safe_interface():
    with telemetry.agent_span("triage", **{"job.id": "job-2"}) as span:
        span.add_event("test.event", {"ok": True})
        span.record_exception(RuntimeError("ignored by noop"))


class _FakeSpan:
    def __init__(self):
        self.attributes = {}
        self.exceptions = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def record_exception(self, exc):
        self.exceptions.append(exc)


class _FakeSpanContext:
    def __init__(self, span):
        self.span = span

    def __enter__(self):
        return self.span

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeTracer:
    def __init__(self):
        self.spans = []

    def start_as_current_span(self, name, context=None):
        span = _FakeSpan()
        span.name = name
        span.parent_context = context
        self.spans.append(span)
        return _FakeSpanContext(span)


def test_traced_span_records_attributes_when_enabled(monkeypatch):
    tracer = _FakeTracer()
    monkeypatch.setattr(telemetry, "_enabled", True)
    monkeypatch.setattr(telemetry, "_tracer", tracer)

    with telemetry.traced_span(
        "worker.process",
        **{"job.id": "job-3", "provider": "github"},
    ) as span:
        span.set_attribute("custom", "value")

    assert tracer.spans[0].name == "worker.process"
    assert tracer.spans[0].attributes["job.id"] == "job-3"
    assert tracer.spans[0].attributes["provider"] == "github"
    assert "duration_ms" in tracer.spans[0].attributes


def test_queue_worker_receives_propagated_traceparent(monkeypatch):
    from app.services import job_queue as queue_module

    traceparent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    spans = []

    @contextmanager
    def fake_span(name, **kwargs):
        spans.append((name, kwargs))
        yield type("Span", (), {"set_attribute": lambda *_a, **_k: None})()

    monkeypatch.setattr(queue_module, "current_traceparent", lambda: traceparent)
    monkeypatch.setattr(queue_module, "traced_span", fake_span)

    queue = queue_module.LocalJobQueue(worker_count=1)
    seen = []

    async def handler(payload):
        seen.append(payload)

    async def run():
        await queue.start()
        try:
            await queue.submit(provider="github", handler=handler, payload={"value": 1})
            await queue.wait_for_idle()
        finally:
            await queue.stop()

    import asyncio
    asyncio.run(run())

    worker_spans = [item for item in spans if item[0] == "worker.process"]
    assert worker_spans
    assert worker_spans[0][1]["traceparent"] == traceparent
    assert seen == [{"value": 1}]

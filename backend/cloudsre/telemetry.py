"""OpenTelemetry SDK spans, W3C propagation and a structured Loki exporter."""

import json
import os
from contextlib import contextmanager

import httpx
from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanKind, Status, StatusCode


class JsonSpanExporter(SpanExporter):
    def export(self, spans):
        for span in spans:
            print(
                json.dumps(
                    {
                        "event": "otel_span",
                        "service": span.resource.attributes["service.name"],
                        "release_id": span.resource.attributes["service.version"],
                        "trace_id": format(span.context.trace_id, "032x"),
                        "span_id": format(span.context.span_id, "016x"),
                        "parent_span_id": format(span.parent.span_id, "016x")
                        if span.parent
                        else None,
                        "name": span.name,
                        "kind": span.kind.name,
                        "start_time_unix_ns": span.start_time,
                        "end_time_unix_ns": span.end_time,
                        "duration_seconds": (span.end_time - span.start_time) / 1e9,
                        "status": span.status.status_code.name,
                        "attributes": dict(span.attributes),
                    }
                ),
                flush=True,
            )
        return SpanExportResult.SUCCESS


def provider(service):
    result = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service,
                "service.version": os.environ.get("RELEASE_ID", "dev"),
            }
        )
    )
    result.add_span_processor(SimpleSpanProcessor(JsonSpanExporter()))
    return result


def trace_id():
    return format(trace.get_current_span().get_span_context().trace_id, "032x")


@contextmanager
def client_span(tracer, method, url, headers=None):
    with tracer.start_as_current_span(
        method + " upstream",
        kind=SpanKind.CLIENT,
        attributes={
            "http.request.method": method,
            "server.address": httpx.URL(url).host,
        },
    ) as span:
        carrier = dict(headers or {})
        propagate.inject(carrier)
        try:
            yield carrier, span
        except Exception:
            span.set_status(Status(StatusCode.ERROR))
            raise

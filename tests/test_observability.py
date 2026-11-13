"""Log shape (OBS-30) and trace correlation of logs (OBS-30/OBS-60).

The real OpenTelemetry API and SDK are used with an in-memory span exporter; the global tracer provider is not
touched (it can only be set once per process), spans come from a provider created per test.
"""

import io
import json
import logging
import re
from concurrent import futures
from types import SimpleNamespace

import grpc
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

import logsetup

TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
RFC3339_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture()
def tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield SimpleNamespace(provider=provider, tracer=provider.get_tracer("test"), exporter=exporter)
    provider.shutdown()


@pytest.fixture()
def logs():
    """A logger wired like server.py, writing to a buffer; lines() parses what was written."""
    stream = io.StringIO()
    logger = logging.getLogger("recommendation-service-obs-test")
    logger.handlers = [logsetup.make_handler("recommendation-service", "1.2.3", stream)]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield SimpleNamespace(logger=logger, lines=lambda: [json.loads(x) for x in stream.getvalue().splitlines()])
    logger.handlers = []


# ---------------------------------------------------------------------------
# log shape
# ---------------------------------------------------------------------------


def test_golden_line_outside_a_span_has_no_trace_ids():
    record = logging.LogRecord("recommendation-service", logging.WARNING, __file__, 1, "slow %s", ("catalog",), None)
    record.created = 1_700_000_000.123456  # injected clock
    line = logsetup.JsonFormatter("recommendation-service", "1.2.3").format(record)
    assert json.loads(line) == {
        "timestamp": "2023-11-14T22:13:20.123Z",
        "severity": "warning",
        "message": "slow catalog",
        "service": "recommendation-service",
        "version": "1.2.3",
    }
    assert "\n" not in line


@pytest.mark.parametrize(
    ("level", "severity"),
    [
        (logging.DEBUG, "debug"),
        (logging.INFO, "info"),
        (logging.WARNING, "warning"),
        (logging.ERROR, "error"),
        (logging.CRITICAL, "error"),
    ],
)
def test_severity_is_lowercase_and_one_of_four(logs, level, severity):
    logs.logger.log(level, "x")
    rec = logs.lines()[0]
    assert rec["severity"] == severity and RFC3339_MS.match(rec["timestamp"])


def test_error_field_from_extra_and_from_exception(logs):
    logs.logger.error("failed", extra={"error": "boom"})
    try:
        raise ValueError("bad value")
    except ValueError:
        logs.logger.error("failed too", exc_info=True)
    first, second = logs.lines()
    assert first["error"] == "boom"
    assert second["error"] == "ValueError: bad value"
    assert "Traceback" not in json.dumps(second)


def test_fields_extra_is_merged_and_quotes_stay_valid_json(logs):
    logs.logger.info('say "hi"\nnow', extra={"fields": {"commit": "abc"}})
    rec = logs.lines()[0]
    assert rec["message"] == 'say "hi"\nnow' and rec["commit"] == "abc"


def test_trace_ids_are_present_inside_a_span_and_absent_outside(logs, tracing):
    logs.logger.info("before")
    with tracing.tracer.start_as_current_span("work") as span:
        logs.logger.info("inside")
        ctx = span.get_span_context()
    logs.logger.info("after")
    before, inside, after = logs.lines()
    assert "trace_id" not in before and "span_id" not in before
    assert "trace_id" not in after and "span_id" not in after
    assert inside["trace_id"] == trace.format_trace_id(ctx.trace_id) and TRACE_ID.match(inside["trace_id"])
    assert inside["span_id"] == trace.format_span_id(ctx.span_id) and SPAN_ID.match(inside["span_id"])
    assert set(inside["trace_id"]) != {"0"}


def test_non_recording_invalid_span_writes_no_ids(logs):
    with trace.use_span(trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)):
        logs.logger.info("x")
    assert "trace_id" not in logs.lines()[0]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, logging.INFO),
        ("", logging.INFO),
        ("debug", logging.DEBUG),
        ("INFO", logging.INFO),
        (" Warning ", logging.WARNING),
        ("warn", logging.WARNING),
        ("error", logging.ERROR),
        ("verbose", logging.INFO),
    ],
)
def test_log_level_from_env(value, expected):
    env = {} if value is None else {"LOG_LEVEL": value}
    assert logsetup.log_level(env) == expected


# ---------------------------------------------------------------------------
# a log line written while a gRPC call is handled carries that call's trace
# ---------------------------------------------------------------------------


def test_grpc_handler_logs_carry_the_incoming_trace_id(logs, tracing):
    from opentelemetry.instrumentation.grpc import GrpcInstrumentorServer

    def echo(request, context):
        logs.logger.info("handling")
        return request

    handler = grpc.method_handlers_generic_handler(
        "t.Echo", {"Say": grpc.unary_unary_rpc_method_handler(echo, lambda b: b, lambda b: b)}
    )
    instrumentor = GrpcInstrumentorServer()
    instrumentor.instrument(tracer_provider=tracing.provider)
    try:
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        server.add_generic_rpc_handlers((handler,))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                call = channel.unary_unary(
                    "/t.Echo/Say", request_serializer=lambda b: b, response_deserializer=lambda b: b
                )
                assert call(b"hi", metadata=(("traceparent", traceparent),), timeout=10) == b"hi"
        finally:
            server.stop(0).wait()
    finally:
        instrumentor.uninstrument()
    line = logs.lines()[0]
    assert line["trace_id"] == "0af7651916cd43dd8448eb211c80319c"
    assert SPAN_ID.match(line["span_id"]) and line["span_id"] != "b7ad6b7169203331"  # the server span, a child
    (span,) = tracing.exporter.get_finished_spans()
    assert span.kind == SpanKind.SERVER and trace.format_span_id(span.context.span_id) == line["span_id"]

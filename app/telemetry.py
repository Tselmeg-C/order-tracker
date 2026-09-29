"""OpenTelemetry setup for Order Tracker: metrics, logs, and traces.

Exporter is chosen with TELEMETRY_EXPORTER:
- "console" (default): print signals to stdout, see them with `docker compose logs app`
- "otlp": send them to an OpenTelemetry Collector (OTEL_EXPORTER_OTLP_ENDPOINT)
- "none": keep the API calls but export nothing (useful in tests)
"""

import logging
import os
import sys
import time

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode


SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "order-tracker")
EXPORTER = os.getenv("TELEMETRY_EXPORTER", "console").lower()
EXPORT_INTERVAL_MS = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "5000"))

logger = logging.getLogger("order_tracker")


def _exporters():
    if EXPORTER == "otlp":
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(), OTLPMetricExporter(), OTLPLogExporter()
    if EXPORTER == "console":
        return ConsoleSpanExporter(), ConsoleMetricExporter(), ConsoleLogExporter()
    return None, None, None


def setup_telemetry():
    resource = Resource.create({"service.name": SERVICE_NAME})
    span_exporter, metric_exporter, log_exporter = _exporters()

    tracer_provider = TracerProvider(resource=resource)
    if span_exporter:
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    readers = []
    if metric_exporter:
        readers.append(
            PeriodicExportingMetricReader(metric_exporter, export_interval_millis=EXPORT_INTERVAL_MS)
        )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))

    logger_provider = LoggerProvider(resource=resource)
    if log_exporter:
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(logger_provider)

    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(stdout)


setup_telemetry()

tracer = trace.get_tracer("order_tracker")
meter = metrics.get_meter("order_tracker")

request_counter = meter.create_counter(
    "http.server.requests",
    unit="1",
    description="HTTP requests by route and status code",
)
request_duration = meter.create_histogram(
    "http.server.request.duration",
    unit="s",
    description="HTTP request duration by route and status code",
)


class TelemetryMiddleware:
    """Records one span, one counter increment, and one duration per HTTP request.

    Written as plain ASGI middleware so it sees unhandled exceptions (which become
    500 responses further out) and the matched route template, not the raw path.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        status = {"code": 500}
        start = time.perf_counter()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        with tracer.start_as_current_span(
            f"{method} {scope['path']}", kind=SpanKind.SERVER, record_exception=False
        ) as span:
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception as exc:
                status["code"] = 500
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, repr(exc)))
                logger.exception(
                    "Unhandled error on %s %s", method, scope["path"],
                    extra={"http.route": _route(scope), "http.response.status_code": 500},
                )
                raise
            finally:
                route = _route(scope)
                attributes = {
                    "http.request.method": method,
                    "http.route": route,
                    "http.response.status_code": status["code"],
                }
                span.update_name(f"{method} {route}")
                span.set_attributes({**attributes, "url.path": scope["path"]})
                if status["code"] >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                request_counter.add(1, attributes)
                request_duration.record(time.perf_counter() - start, attributes)


def _route(scope):
    route = scope.get("route")
    return getattr(route, "path", None) or "unmatched"

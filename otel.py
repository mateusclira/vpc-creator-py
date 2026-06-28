import logging as python_logging

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor


SERVICE_NAME = "vpc-creator-api"
SERVICE_VERSION = "1.0.0"


def setup_telemetry(app: FastAPI) -> None:
    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": SERVICE_VERSION,
        }
    )

    # ── Traces → Jaeger (gRPC OTLP) ──────────────────────────────────────────
    trace_provider = TracerProvider(resource=resource)
    trace_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint="http://jaeger:4317", insecure=True)
        )
    )
    trace.set_tracer_provider(trace_provider)

    # ── Logs → Loki (HTTP OTLP) ───────────────────────────────────────────────
    log_provider = LoggerProvider(resource=resource)
    log_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(endpoint="http://loki:3100/otlp/v1/logs")
        )
    )
    set_logger_provider(log_provider)

    # Bridge Python's standard logging → OTel log records
    otel_handler = LoggingHandler(
        level=python_logging.DEBUG, logger_provider=log_provider
    )
    python_logging.getLogger().addHandler(otel_handler)

    FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics,health")

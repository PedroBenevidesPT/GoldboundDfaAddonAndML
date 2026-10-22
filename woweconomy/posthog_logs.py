"""Isolated OpenTelemetry log export for FarmAnalysis pipeline milestones."""

import atexit
import logging
import os


LOGGER_NAME = "woweconomy.posthog_export"


def configure_posthog_logger() -> logging.Logger:
    """Return the dedicated logger, exporting only its records when configured."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    project_token = os.getenv("POSTHOG_PROJECT_TOKEN")
    host = os.getenv("POSTHOG_HOST")
    if not project_token or not host:
        return logger

    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    logger_provider = LoggerProvider()
    exporter = OTLPLogExporter(
        endpoint=f"{host.rstrip('/')}/i/v1/logs",
        headers={"Authorization": f"Bearer {project_token}"},
    )
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    logger.addHandler(LoggingHandler(logger_provider=logger_provider))
    atexit.register(logger_provider.shutdown)
    return logger


posthog_logger = configure_posthog_logger()

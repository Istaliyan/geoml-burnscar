"""Structured logging setup."""

import logging

import structlog


def configure_logging() -> None:
    """Emit stable JSON log records suitable for local runs and CI."""
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    # HTTPX's INFO request logs include the URL, which may contain short-lived SAS credentials.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

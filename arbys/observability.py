"""Structured logging setup for CLI entrypoints.

Anywhere logs matter (adapters, ingest worker, engine, backend), just call
`configure_logging()` once at startup. Everything else uses stdlib
`logging.getLogger(__name__)` and inherits the structured format.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog


def configure_logging(level: str | None = None) -> None:
    lvl = (level or os.environ.get("ARBYS_LOG_LEVEL", "INFO")).upper()
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, lvl, logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, lvl, logging.INFO)),
        cache_logger_on_first_use=True,
    )

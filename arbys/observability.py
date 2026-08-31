"""Structured logging setup for every entrypoint, CLI or ASGI.

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
    numeric = getattr(logging, lvl, logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=numeric)
    # basicConfig is a *no-op* when the root logger already has a handler, and
    # something usually does -- a test runner, a platform wrapper, another
    # library. Relying on it alone means this function can silently configure
    # nothing, which is how INFO stayed off in the hosted process. Set the
    # level explicitly so the outcome does not depend on who ran first.
    logging.getLogger().setLevel(numeric)
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

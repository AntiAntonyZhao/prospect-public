"""structlog setup: one line per posting per stage, to stderr (stdout is for reports)."""

from __future__ import annotations

import logging
import os
import sys

import structlog

_configured = False


def configure(level: str | None = None) -> None:
    global _configured
    if _configured:
        return
    lvl = getattr(logging, (level or os.environ.get("PROSPECT_LOG", "info")).upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(stage: str) -> structlog.typing.FilteringBoundLogger:
    configure()
    return structlog.get_logger().bind(stage=stage)

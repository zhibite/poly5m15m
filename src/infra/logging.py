"""Structured logging setup via structlog."""
from __future__ import annotations

import logging
import os
import sys

import structlog


def configure_logging(level: str | None = None) -> None:
    name = level or os.getenv("ARB_LOG_LEVEL") or os.getenv("LOG_LEVEL") or "INFO"
    lvl = getattr(logging, str(name).upper(), logging.INFO)

    for noisy in ("httpx", "httpcore", "http", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=lvl,
    )
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)

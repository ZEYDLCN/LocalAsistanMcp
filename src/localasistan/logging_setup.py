"""Loglama. MCP stdio sunucusunda **stdout protokole ayrılmıştır**; tüm log stderr'e gider."""

from __future__ import annotations

import logging
import sys
from typing import TextIO

_LOGGER_NAME = "localasistan"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: str | int = "INFO", stream: TextIO | None = None) -> logging.Logger:
    """`localasistan` logger'ını stderr'e yönlendirir. Tekrar çağrılması güvenlidir."""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    target = stream if stream is not None else sys.stderr
    for handler in list(logger.handlers):
        if getattr(handler, "_localasistan", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(target)
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler._localasistan = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")

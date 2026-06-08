"""Logging configuration for Beast (console + optional rotating file)."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(level: str = "INFO", logfile: Optional[str] = None) -> logging.Logger:
    logger = logging.getLogger("beast")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(console)

    if logfile:
        os.makedirs(os.path.dirname(os.path.abspath(logfile)), exist_ok=True)
        fileh = RotatingFileHandler(logfile, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fileh.setFormatter(logging.Formatter(_FMT))
        logger.addHandler(fileh)

    return logger

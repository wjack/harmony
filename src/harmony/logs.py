"""Process logging for the long-running processes: the API (its lifespan) and the worker
(`harmony worker`).

uvicorn configures only its own loggers, so without this an API process drops every
INFO line Harmony writes (`gate_attempt`, the orphan check) and prints its warnings
bare. Tests and `harmony run` drive the app without its lifespan and keep their own
logging.
"""

from __future__ import annotations

import logging
from typing import Final

LOG_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging() -> None:
    """INFO and up to stderr, unless the process already configured the root logger."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


__all__ = ["LOG_FORMAT", "configure_logging"]

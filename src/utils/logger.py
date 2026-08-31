from __future__ import annotations

import logging
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

from src.config.settings import LOGS_DIR

# local time with an explicit utc offset (e.g. 2026-08-31T12:22:45+0200)
_FMT = logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
)


def _make_handler(path: Path) -> TimedRotatingFileHandler:
    """A file handler that rolls to a new file at every UTC midnight."""
    handler = TimedRotatingFileHandler(path, when="midnight", utc=True, encoding="utf-8")
    handler.setFormatter(_FMT)
    return handler

# current run's log dir and the components requested so far (for re-attaching on setup)
_run_dir: Optional[Path] = None
_components: set[str] = set()
_level: int = logging.DEBUG


def setup_logging(run: str = "cli", level: int = logging.DEBUG) -> Path:
    """Configure per-execution logging: one folder per run, one file per component.

    Components do not need to be declared in advance — the first get_logger(name)
    call creates its file automatically. Returns the run's log directory.
    """
    global _run_dir, _level
    _level = level
    _run_dir = LOGS_DIR / f"{datetime.now().astimezone():%Y%m%d_%H%M%S%z}_{run}"
    _run_dir.mkdir(parents=True, exist_ok=True)

    # root: combined file with everything
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(_make_handler(_run_dir / "recall.log"))

    # (re)attach a file handler for every component already requested
    for comp in _components:
        _attach_file_handler(comp)

    return _run_dir


def _attach_file_handler(component: str) -> None:
    logger = logging.getLogger(f"recall.{component}")
    logger.setLevel(_level)
    logger.handlers.clear()  # drop a previous run's handler
    if _run_dir is not None:
        logger.addHandler(_make_handler(_run_dir / f"{component}.log"))


def get_logger(component: str) -> logging.Logger:
    """Return the logger for a component; its file is created lazily on first use."""
    _components.add(component)
    logger = logging.getLogger(f"recall.{component}")
    if _run_dir is not None and not logger.handlers:
        _attach_file_handler(component)
    return logger


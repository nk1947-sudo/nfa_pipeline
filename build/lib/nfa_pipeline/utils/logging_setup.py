"""
nfa_pipeline/utils/logging_setup.py
-------------------------------------
Structured, colourised logging for the NFA pipeline.

Features
--------
* Console handler with optional ANSI colour codes
* Rotating file handler (10 MB × 5 backups)
* Consistent format across all loggers in the package
* One-call setup via :func:`setup_logging`

Usage
-----
    from nfa_pipeline.utils.logging_setup import setup_logging
    setup_logging()
    import logging
    log = logging.getLogger(__name__)
    log.info("Pipeline started")
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional


# ── ANSI colour map ───────────────────────────────────────────────────────────

_LEVEL_COLOURS = {
    "DEBUG":    "\033[36m",      # Cyan
    "INFO":     "\033[32m",      # Green
    "WARNING":  "\033[33m",      # Yellow
    "ERROR":    "\033[31m",      # Red
    "CRITICAL": "\033[1;31m",    # Bold red
}
_RESET = "\033[0m"
_GREY  = "\033[90m"


class _ColourFormatter(logging.Formatter):
    """Formatter that injects ANSI colours around the level name."""

    def format(self, record: logging.LogRecord) -> str:
        colour = _LEVEL_COLOURS.get(record.levelname, "")
        record.levelname = f"{colour}{record.levelname:<8}{_RESET}"
        # Dim the logger name
        record.name = f"{_GREY}{record.name:<25}{_RESET}"
        return super().format(record)


# ── Progress bar helper ───────────────────────────────────────────────────────

class PipelineProgress:
    """Minimal progress tracker that writes to the log."""

    def __init__(self, total: int, description: str = "Processing", logger_name: str = __name__):
        self._total = total
        self._current = 0
        self._desc = description
        self._log = logging.getLogger(logger_name)

    def update(self, n: int = 1, message: str = "") -> None:
        self._current = min(self._current + n, self._total)
        pct = (self._current / self._total * 100) if self._total else 0
        bar_len = 30
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        suffix = f" — {message}" if message else ""
        self._log.info("[%s] %s %3.0f%% (%d/%d)%s", bar, self._desc, pct, self._current, self._total, suffix)

    def done(self) -> None:
        self._log.info("✓ %s complete (%d items)", self._desc, self._total)


# ── Main setup function ───────────────────────────────────────────────────────

def setup_logging(
    level: str = "INFO",
    log_file: Optional[Path] = None,
    fmt: str = "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
    date_fmt: str = "%Y-%m-%d %H:%M:%S",
    colorize: bool = True,
    quiet: bool = False,
) -> logging.Logger:
    """
    Configure the root logger for the ``nfa_pipeline`` package.

    Parameters
    ----------
    level : str
        Logging level string (``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``).
    log_file : Path, optional
        Path to a rotating log file.  ``None`` disables file logging.
    fmt : str
        ``logging.Formatter`` format string.
    date_fmt : str
        Date format string for the formatter.
    colorize : bool
        Whether to emit ANSI colour codes on the console.
    quiet : bool
        Suppress all console output (file logging still active).

    Returns
    -------
    logging.Logger
        The configured ``nfa_pipeline`` logger.
    """
    root_logger = logging.getLogger("nfa_pipeline")
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Prevent duplicate handlers when called multiple times (e.g. tests)
    if root_logger.handlers:
        root_logger.handlers.clear()

    # ── Console handler ───────────────────────────────────────────────────────
    if not quiet:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(getattr(logging, level.upper(), logging.INFO))

        if colorize and sys.stdout.isatty():
            console_formatter = _ColourFormatter(fmt=fmt, datefmt=date_fmt)
        else:
            console_formatter = logging.Formatter(fmt=fmt, datefmt=date_fmt)

        console_handler.setFormatter(console_formatter)
        root_logger.addHandler(console_handler)

    # ── File handler ─────────────────────────────────────────────────────────
    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(log_file),
            maxBytes=10 * 1024 * 1024,    # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)   # Always capture full detail in file
        file_handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=date_fmt))
        root_logger.addHandler(file_handler)

    root_logger.debug("Logging initialised — level=%s, file=%s", level, log_file)
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``nfa_pipeline`` namespace."""
    return logging.getLogger(f"nfa_pipeline.{name}")

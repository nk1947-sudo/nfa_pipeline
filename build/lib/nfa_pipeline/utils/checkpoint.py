"""
nfa_pipeline/utils/checkpoint.py
----------------------------------
Checkpoint manager for resumable pipeline runs.

Persists which years have been successfully scraped to a JSON file,
so that a re-run after a crash only processes outstanding years.

Usage
-----
    from nfa_pipeline.utils.checkpoint import CheckpointManager
    cp = CheckpointManager(Path("data/cache/checkpoint.json"))
    cp.mark_done(2005)
    remaining = cp.remaining_years(range(2000, 2024))
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Set

logger = logging.getLogger(__name__)


class CheckpointManager:
    """
    Lightweight JSON-based checkpoint for long-running scraping jobs.

    Parameters
    ----------
    path : Path
        File path for the checkpoint JSON.  Created automatically.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict = self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def mark_done(self, year: int) -> None:
        """Record *year* as successfully scraped."""
        done: Set[int] = set(self._state.get("done", []))
        done.add(year)
        self._state["done"] = sorted(done)
        self._state["last_updated"] = datetime.utcnow().isoformat()
        self._save()
        logger.debug("Checkpoint: year %d marked done", year)

    def mark_failed(self, year: int, reason: str = "") -> None:
        """Record *year* as failed."""
        failed: dict = self._state.get("failed", {})
        failed[str(year)] = {"reason": reason, "at": datetime.utcnow().isoformat()}
        self._state["failed"] = failed
        self._save()
        logger.warning("Checkpoint: year %d marked failed — %s", year, reason)

    def is_done(self, year: int) -> bool:
        """Return ``True`` if *year* has already been processed successfully."""
        return year in self._state.get("done", [])

    def remaining_years(self, years: Iterable[int]) -> List[int]:
        """Return years from *years* that have not yet been marked done."""
        return [y for y in years if not self.is_done(y)]

    def done_years(self) -> List[int]:
        """Return all years marked done."""
        return list(self._state.get("done", []))

    def failed_years(self) -> dict:
        """Return mapping of failed year → failure metadata."""
        return dict(self._state.get("failed", {}))

    def reset(self) -> None:
        """Clear all checkpoint state (start fresh)."""
        self._state = {}
        self._save()
        logger.info("Checkpoint reset")

    def summary(self) -> str:
        done = len(self._state.get("done", []))
        failed = len(self._state.get("failed", {}))
        return f"CheckpointManager(done={done}, failed={failed}, path={self._path})"

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if self._path.exists():
            try:
                state = json.loads(self._path.read_text(encoding="utf-8"))
                logger.debug("Checkpoint loaded: %s", self._path)
                return state
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read checkpoint (%s) — starting fresh", exc)
        return {}

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(self._state, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error("Could not write checkpoint: %s", exc)

    def __repr__(self) -> str:
        return self.summary()

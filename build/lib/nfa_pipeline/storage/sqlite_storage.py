"""
nfa_pipeline/storage/sqlite_storage.py
----------------------------------------
SQLite persistence layer for :class:`AwardRecord` objects.

Schema
------
Table: ``award_records``
  Stores one row per award.  See :meth:`SQLiteStorage._create_schema` for
  the full column list.

Table: ``scrape_runs``
  Audit trail for every pipeline execution.

Table: ``raw_pages``
  Stores compressed raw HTML for change detection and re-parsing.

Features
--------
* WAL mode for concurrent read access
* UPSERT (INSERT OR REPLACE) for idempotent re-runs
* Batch inserts via executemany for speed
* Context-manager interface
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, List, Optional

from ..models import AwardRecord, ScrapeRun

logger = logging.getLogger(__name__)


class SQLiteStorage:
    """
    SQLite-backed storage for NFA award records.

    Parameters
    ----------
    storage_cfg
        SQLite sub-config (path, pragmas).
    """

    def __init__(self, storage_cfg):
        self._cfg = storage_cfg
        self._path = Path(storage_cfg.path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        logger.debug("SQLiteStorage initialised at %s", self._path)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Open the database connection and apply PRAGMAs."""
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self._create_schema()
        logger.info("Connected to SQLite: %s", self._path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.debug("SQLite connection closed")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that commits or rolls back a transaction."""
        conn = self._ensure_connected()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ── Public API ─────────────────────────────────────────────────────────────

    def upsert_records(self, records: List[AwardRecord]) -> int:
        """
        Insert or replace *records* into the database.

        Returns the number of rows affected.
        """
        if not records:
            return 0

        rows = [self._record_to_row(r) for r in records]
        sql = """
            INSERT OR REPLACE INTO award_records (
                record_id, ceremony_year, ceremony_number,
                category, category_normalized,
                film_title, film_title_original, film_language, film_year,
                director, awardee, production_company,
                award_type, cash_prize,
                source_url, scraped_at, raw_html_hash,
                is_valid, validation_warnings, validation_errors
            ) VALUES (
                :record_id, :ceremony_year, :ceremony_number,
                :category, :category_normalized,
                :film_title, :film_title_original, :film_language, :film_year,
                :director, :awardee, :production_company,
                :award_type, :cash_prize,
                :source_url, :scraped_at, :raw_html_hash,
                :is_valid, :validation_warnings, :validation_errors
            )
        """
        with self.transaction() as conn:
            conn.executemany(sql, rows)

        logger.info("Upserted %d records into SQLite", len(records))
        return len(records)

    def upsert_run(self, run: ScrapeRun) -> None:
        """Record or update a :class:`ScrapeRun` entry."""
        sql = """
            INSERT OR REPLACE INTO scrape_runs (
                run_id, started_at, finished_at,
                years_requested, years_scraped, years_failed,
                total_records, valid_records, invalid_records,
                warnings_count, status, error_message
            ) VALUES (
                :run_id, :started_at, :finished_at,
                :years_requested, :years_scraped, :years_failed,
                :total_records, :valid_records, :invalid_records,
                :warnings_count, :status, :error_message
            )
        """
        row = {
            "run_id": run.run_id,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "years_requested": json.dumps(run.years_requested),
            "years_scraped": json.dumps(run.years_scraped),
            "years_failed": json.dumps(run.years_failed),
            "total_records": run.total_records,
            "valid_records": run.valid_records,
            "invalid_records": run.invalid_records,
            "warnings_count": run.warnings_count,
            "status": run.status,
            "error_message": run.error_message,
        }
        with self.transaction() as conn:
            conn.execute(sql, row)

    def query_by_year(self, year: int) -> List[dict]:
        """Return all records for *year* as a list of dicts."""
        conn = self._ensure_connected()
        cur = conn.execute(
            "SELECT * FROM award_records WHERE ceremony_year = ? ORDER BY category",
            (year,),
        )
        return [dict(row) for row in cur.fetchall()]

    def count_records(self) -> int:
        """Return total number of award records in the database."""
        conn = self._ensure_connected()
        return conn.execute("SELECT COUNT(*) FROM award_records").fetchone()[0]

    def years_in_db(self) -> List[int]:
        """Return sorted list of all ceremony years present in the database."""
        conn = self._ensure_connected()
        rows = conn.execute(
            "SELECT DISTINCT ceremony_year FROM award_records ORDER BY ceremony_year"
        ).fetchall()
        return [r[0] for r in rows]

    def fetch_all(self) -> List[dict]:
        """Return every record as a list of dicts (for export)."""
        conn = self._ensure_connected()
        cur = conn.execute("SELECT * FROM award_records ORDER BY ceremony_year, category")
        return [dict(row) for row in cur.fetchall()]

    # ── Schema ────────────────────────────────────────────────────────────────

    def _create_schema(self) -> None:
        conn = self._ensure_connected()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS award_records (
                record_id            TEXT PRIMARY KEY,
                ceremony_year        INTEGER NOT NULL,
                ceremony_number      INTEGER,
                category             TEXT NOT NULL,
                category_normalized  TEXT,
                film_title           TEXT NOT NULL,
                film_title_original  TEXT,
                film_language        TEXT,
                film_year            INTEGER,
                director             TEXT,
                awardee              TEXT,
                production_company   TEXT,
                award_type           TEXT,
                cash_prize           TEXT,
                source_url           TEXT,
                scraped_at           TEXT,
                raw_html_hash        TEXT,
                is_valid             INTEGER DEFAULT 1,
                validation_warnings  TEXT,
                validation_errors    TEXT,
                created_at           TEXT DEFAULT (datetime('now')),
                updated_at           TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_award_records_year
                ON award_records(ceremony_year);
            CREATE INDEX IF NOT EXISTS idx_award_records_film
                ON award_records(film_title);
            CREATE INDEX IF NOT EXISTS idx_award_records_category
                ON award_records(category_normalized);

            CREATE TABLE IF NOT EXISTS scrape_runs (
                run_id          TEXT PRIMARY KEY,
                started_at      TEXT,
                finished_at     TEXT,
                years_requested TEXT,
                years_scraped   TEXT,
                years_failed    TEXT,
                total_records   INTEGER DEFAULT 0,
                valid_records   INTEGER DEFAULT 0,
                invalid_records INTEGER DEFAULT 0,
                warnings_count  INTEGER DEFAULT 0,
                status          TEXT,
                error_message   TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS raw_pages (
                year        INTEGER PRIMARY KEY,
                url         TEXT,
                html_hash   TEXT,
                scraped_at  TEXT
            );
        """)
        conn.commit()
        logger.debug("SQLite schema ensured")

    def _apply_pragmas(self) -> None:
        conn = self._ensure_connected()
        pragmas = self._cfg.pragmas or {}
        defaults = {
            "journal_mode": "WAL",
            "synchronous": "NORMAL",
            "foreign_keys": "ON",
        }
        for key, val in {**defaults, **pragmas}.items():
            conn.execute(f"PRAGMA {key} = {val}")
        conn.commit()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _ensure_connected(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        return self._conn  # type: ignore[return-value]

    @staticmethod
    def _record_to_row(r: AwardRecord) -> dict:
        return {
            "record_id": r.record_id,
            "ceremony_year": r.ceremony_year,
            "ceremony_number": r.ceremony_number,
            "category": r.category,
            "category_normalized": r.category_normalized,
            "film_title": r.film_title,
            "film_title_original": r.film_title_original,
            "film_language": r.film_language,
            "film_year": r.film_year,
            "director": r.director,
            "awardee": r.awardee,
            "production_company": r.production_company,
            "award_type": r.award_type,
            "cash_prize": r.cash_prize,
            "source_url": r.source_url,
            "scraped_at": r.scraped_at.isoformat() if r.scraped_at else None,
            "raw_html_hash": r.raw_html_hash,
            "is_valid": int(r.is_valid),
            "validation_warnings": json.dumps(r.validation_warnings),
            "validation_errors": json.dumps(r.validation_errors),
        }

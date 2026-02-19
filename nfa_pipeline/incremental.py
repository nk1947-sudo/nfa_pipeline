"""
nfa_pipeline/incremental.py
-----------------------------
Incremental update logic for the NFA pipeline.

Instead of re-scraping everything, this module:

  1. Compares the existing database against the configured year range to find
     **missing years** (never scraped) and **incomplete records** (null
     enrichment fields).
  2. Re-scrapes only the missing years via the normal pipeline.
  3. Re-enriches incomplete records in-place using OMDb / RSS / Newsdata
     without touching already-populated fields.
  4. Upserts results back to SQLite and re-exports CSV / JSON.

Per-scan databases
------------------
Each pipeline run creates a timestamped subfolder under ``data/processed/``,
e.g. ``run_20260218_194250_518b1acc/``.  When ``--scan-id`` is given, the
incremental updater targets that run folder's DB
(``run_<id>/nfa_awards.db``) instead of the canonical DB.

Deduplication key
-----------------
``record_id = "ceremony_year|category[:40]|film_title[:60]"``
This is set in ``AwardRecord.__post_init__`` and is the SQLite PRIMARY KEY.
INSERT OR REPLACE on the same key is therefore idempotent.

Usage (CLI)
-----------
    # Update canonical DB (data/processed/nfa_awards.db)
    python -m nfa_pipeline --incremental

    # Only fill null fields, no re-scraping
    python -m nfa_pipeline --incremental --enrich-only

    # Target a specific scan's DB
    python -m nfa_pipeline --incremental --scan-id run_20260218_194250_518b1acc

    # List available scans
    python -m nfa_pipeline --list-scans
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from .config import get_config, Config
from .models import AwardRecord, ScrapeRun
from .storage import SQLiteStorage, CSVExporter, JSONExporter
from .utils import HTTPClient, setup_logging

logger = logging.getLogger(__name__)

# ── Fields that trigger re-enrichment when null ───────────────────────────────
# Only columns that actually exist in the DB schema (after migration).
ENRICHMENT_FIELDS: List[str] = [
    "imdb_id",
    "imdb_rating",
    "plot_summary",       # added via schema migration
    "genres",
    "runtime_minutes",
    "release_date",
    "director",
]


@dataclass
class IncrementalReport:
    """Summary of what the incremental update did."""
    run_id:             str
    started_at:         datetime
    scan_id:            Optional[str]      = None
    db_path:            Optional[str]      = None
    finished_at:        Optional[datetime] = None
    missing_years:      List[int]          = field(default_factory=list)
    years_scraped:      List[int]          = field(default_factory=list)
    years_failed:       List[int]          = field(default_factory=list)
    records_scraped:    int                = 0
    records_enriched:   int                = 0
    records_skipped:    int                = 0
    total_in_db_before: int                = 0
    total_in_db_after:  int                = 0

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    def __str__(self) -> str:
        dur = f"{self.duration_seconds:.1f}s" if self.duration_seconds else "?"
        scan = self.scan_id or "canonical"
        return (
            f"\n{'━'*60}\n"
            f"INCREMENTAL UPDATE COMPLETE\n"
            f"  Scan              : {scan}\n"
            f"  DB                : {self.db_path}\n"
            f"  Duration          : {dur}\n"
            f"  Missing years     : {self.missing_years}\n"
            f"  Years scraped     : {self.years_scraped}\n"
            f"  Years failed      : {self.years_failed}\n"
            f"  Records scraped   : {self.records_scraped}\n"
            f"  Records enriched  : {self.records_enriched}\n"
            f"  Records skipped   : {self.records_skipped}\n"
            f"  DB before / after : {self.total_in_db_before} / {self.total_in_db_after}\n"
            f"{'━'*60}"
        )


class IncrementalUpdater:
    """
    Performs incremental updates on the NFA dataset.

    Parameters
    ----------
    config_path : Path, optional
        Path to a custom ``settings.yaml``.
    scan_id : str, optional
        Name of a specific run folder (e.g. ``run_20260218_194250_518b1acc``).
        If given, that folder's ``nfa_awards.db`` is used instead of the
        canonical DB.
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        scan_id: Optional[str] = None,
    ):
        self._cfg: Config = get_config(config_path)
        self._scan_id = scan_id
        self._ensure_dirs()
        setup_logging(
            level=self._cfg.logging.level,
            log_file=Path(self._cfg.paths.logs_dir) / "pipeline.log",
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, enrich_only: bool = False) -> IncrementalReport:
        """
        Run the incremental update.

        Parameters
        ----------
        enrich_only : bool
            If ``True``, skip scraping new years entirely and only fill null
            enrichment fields on existing records.
        """
        db_path = self._resolve_db_path()
        sqlite_cfg = _override_db_path(self._cfg.storage.sqlite, db_path)

        report = IncrementalReport(
            run_id=str(uuid.uuid4()),
            started_at=datetime.utcnow(),
            scan_id=self._scan_id,
            db_path=str(db_path),
        )

        t_run_start = time.perf_counter()
        logger.info("=" * 60)
        logger.info(
            "NFA Incremental Update — scan=%s  db=%s  enrich_only=%s",
            self._scan_id or "canonical", db_path, enrich_only,
        )
        logger.info("=" * 60)

        with SQLiteStorage(sqlite_cfg) as db:
            report.total_in_db_before = db.count_records()
            logger.info("Records in DB before update: %d", report.total_in_db_before)

            # ── Step 1: Identify missing years ────────────────────────────────
            all_years = list(range(
                self._cfg.scraper.start_year,
                self._cfg.scraper.end_year + 1,
            ))
            years_in_db: Set[int] = set(db.years_in_db())
            report.missing_years = [y for y in all_years if y not in years_in_db]

            logger.info(
                "Year range: %d–%d | In DB: %d | Missing: %d",
                self._cfg.scraper.start_year, self._cfg.scraper.end_year,
                len(years_in_db), len(report.missing_years),
            )

            # ── Step 2: Scrape missing years ──────────────────────────────────
            t_scrape = 0.0
            if not enrich_only and report.missing_years:
                t0 = time.perf_counter()
                new_records = self._scrape_years(report.missing_years, report)
                t_scrape = time.perf_counter() - t0
                if new_records:
                    db.upsert_records(new_records)
                    report.records_scraped = len(new_records)
                logger.info(
                    "[Incremental] TIMING — scrape: %.2fs → %d new records",
                    t_scrape, report.records_scraped,
                )
            elif enrich_only:
                logger.info("enrich-only mode: skipping year scraping")
            else:
                logger.info("No missing years — skipping scrape phase")

            # ── Step 3: Find incomplete records and re-enrich ─────────────────
            t0 = time.perf_counter()
            incomplete = self._find_incomplete_records(db)
            t_find = time.perf_counter() - t0
            logger.info(
                "[Incremental] TIMING — find incomplete: %.2fs → %d records",
                t_find, len(incomplete),
            )

            if incomplete:
                t0 = time.perf_counter()
                enriched_records = self._enrich_records(incomplete)
                t_enrich = time.perf_counter() - t0
                if enriched_records:
                    db.upsert_records(enriched_records)
                    report.records_enriched = len(enriched_records)
                logger.info(
                    "[Incremental] TIMING — enrichment: %.2fs → %d records enriched",
                    t_enrich, report.records_enriched,
                )

            report.records_skipped = len(incomplete) - report.records_enriched

            # ── Step 4: Re-export CSV / JSON ──────────────────────────────────
            t0 = time.perf_counter()
            all_rows = db.fetch_all()
            report.total_in_db_after = len(all_rows)
            self._reexport(all_rows, report.run_id, db_path)
            logger.info(
                "[Incremental] TIMING — export: %.2fs → %d rows",
                time.perf_counter() - t0, len(all_rows),
            )

        t_total = time.perf_counter() - t_run_start
        logger.info(
            "[Incremental] TIMING — TOTAL: %.2fs  (scrape=%.2fs | find=%.2fs)",
            t_total, t_scrape, t_find,
        )

        report.finished_at = datetime.utcnow()
        logger.info("%s", report)
        return report

    # ── Scan discovery ────────────────────────────────────────────────────────

    @staticmethod
    def list_scans(processed_dir: str = "data/processed") -> List[Dict]:
        """
        Return a list of available scan folders under *processed_dir*.

        Each entry has keys: ``scan_id``, ``db_path``, ``has_db``, ``created_at``.
        """
        base = Path(processed_dir)
        scans = []
        for d in sorted(base.iterdir()):
            if d.is_dir() and d.name.startswith("run_"):
                db = d / "nfa_awards.db"
                scans.append({
                    "scan_id":    d.name,
                    "db_path":    str(db),
                    "has_db":     db.exists(),
                    "created_at": datetime.fromtimestamp(d.stat().st_mtime).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                })
        return scans

    # ── Internal: DB path resolution ──────────────────────────────────────────

    def _resolve_db_path(self) -> Path:
        """Return the DB path to use — scan-specific or canonical.

        If a scan folder exists but has no DB, we auto-create one by importing
        the scan's CSV (handles older runs that predate per-scan DB support).
        """
        if not self._scan_id:
            return Path(self._cfg.storage.sqlite.path)

        processed = Path(self._cfg.paths.processed_dir)
        scan_dir = processed / self._scan_id
        if not scan_dir.exists():
            raise FileNotFoundError(
                f"Scan folder not found: {scan_dir}\n"
                f"Run `python -m nfa_pipeline --list-scans` to see available scans."
            )

        db = scan_dir / "nfa_awards.db"
        if db.exists():
            return db

        # ── No DB yet — try to bootstrap from scan's CSV ──────────────────────
        csv_path = scan_dir / "nfa_awards.csv"
        if not csv_path.exists():
            raise FileNotFoundError(
                f"No DB or CSV found in scan folder: {scan_dir}\n"
                f"The scan may not have produced any output files."
            )

        logger.warning(
            "[Incremental] No DB in scan folder — bootstrapping from CSV: %s", csv_path
        )
        self._import_csv_to_db(csv_path, db)
        logger.info("[Incremental] DB created from CSV: %s", db)
        return db

    def _import_csv_to_db(self, csv_path: Path, db_path: Path) -> None:
        """Read a scan CSV and write every row into a fresh SQLite DB."""
        import csv as _csv
        from .storage import SQLiteStorage

        sqlite_cfg = _override_db_path(self._cfg.storage.sqlite, db_path)

        with open(csv_path, encoding="utf-8", newline="") as fh:
            reader = _csv.DictReader(fh)
            rows = list(reader)

        logger.info("[Incremental] Importing %d rows from CSV …", len(rows))

        # Convert CSV strings back to typed AwardRecord objects
        records = []
        for row in rows:
            try:
                records.append(_row_to_award_record(row))
            except Exception as exc:
                logger.debug("[Incremental] Skipping bad CSV row: %s", exc)

        with SQLiteStorage(sqlite_cfg) as db:
            db.upsert_records(records)

        logger.info("[Incremental] Imported %d records into %s", len(records), db_path)


    # ── Internal: scraping ────────────────────────────────────────────────────

    def _scrape_years(
        self, years: List[int], report: IncrementalReport
    ) -> List[AwardRecord]:
        from .extractors.multi_source_extractor import MultiSourceExtractor
        from .transformers import RecordTransformer
        from .validators import RecordValidator

        all_records: List[AwardRecord] = []

        with HTTPClient(self._cfg.scraper, self._cfg.paths, self._cfg.cache) as http:
            extractor   = MultiSourceExtractor(http, self._cfg)
            transformer = RecordTransformer(self._cfg.transformer)
            validator   = RecordValidator(self._cfg.validator)

            for year in years:
                try:
                    logger.info("[Incremental] Scraping missing year %d …", year)
                    page = extractor.extract_year(year)

                    if page.parse_error and not page.records:
                        logger.warning(
                            "[Incremental] Year %d extraction failed: %s",
                            year, page.parse_error,
                        )
                        report.years_failed.append(year)
                        continue

                    transformed = transformer.transform_batch(page.records)
                    validated, _ = validator.validate_batch(transformed)
                    all_records.extend(validated)
                    report.years_scraped.append(year)
                    logger.info("[Incremental] Year %d: %d records", year, len(validated))

                except Exception as exc:
                    logger.exception(
                        "[Incremental] Year %d unexpected error: %s", year, exc
                    )
                    report.years_failed.append(year)

        return all_records

    # ── Internal: incomplete record detection ─────────────────────────────────

    def _find_incomplete_records(self, db: SQLiteStorage) -> List[AwardRecord]:
        """
        Return AwardRecord objects that have at least one null enrichment field.

        Only checks columns that actually exist in the DB (guards against
        missing columns in older DBs that predate schema migrations).
        """
        conn = db._ensure_connected()

        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(award_records)").fetchall()
        }
        checkable = [f for f in ENRICHMENT_FIELDS if f in existing_cols]

        if not checkable:
            logger.warning(
                "[Incremental] None of the enrichment fields exist in this DB — "
                "run the full pipeline first to populate the schema."
            )
            return []

        null_checks = " OR ".join(f"{f} IS NULL" for f in checkable)
        sql = f"""
            SELECT * FROM award_records
            WHERE ({null_checks})
            ORDER BY ceremony_year, category
        """
        rows = conn.execute(sql).fetchall()
        logger.info(
            "[Incremental] %d records have null in: %s",
            len(rows), ", ".join(checkable),
        )
        return [_row_to_award_record(dict(row)) for row in rows]

    # ── Internal: enrichment ──────────────────────────────────────────────────

    def _enrich_records(self, records: List[AwardRecord]) -> List[AwardRecord]:
        from .extractors.omdb_extractor import OMDbExtractor
        from .extractors.rss_extractor import RSSExtractor
        from .extractors.newsdata_extractor import NewsdataExtractor

        enr = self._cfg.enrichment

        rss = RSSExtractor()
        newsdata = (
            NewsdataExtractor(
                api_key=enr.newsdata_api_key,
                country=enr.newsdata_country,
                language=enr.newsdata_language,
            )
            if enr.newsdata_enabled and enr.newsdata_api_key
            else None
        )

        with HTTPClient(self._cfg.scraper, self._cfg.paths, self._cfg.cache) as http:
            omdb = OMDbExtractor(
                http,
                api_key=enr.omdb_api_key,
                rss_extractor=rss,
                newsdata_extractor=newsdata,
            )

            t0 = time.perf_counter()
            if enr.omdb_enabled:
                omdb.enrich_records_batch(records, delay=enr.omdb_delay)
                logger.info(
                    "[Incremental] TIMING — OMDb enrichment: %.2fs for %d records",
                    time.perf_counter() - t0, len(records),
                )
            else:
                logger.info("[Incremental] OMDb disabled — using RSS/Newsdata only")
                t_rss = t_nd = 0.0
                for rec in records:
                    title = rec.film_title or ""
                    if not rec.plot_summary:
                        t1 = time.perf_counter()
                        rss_data = rss.fetch(title, rec.film_year)
                        t_rss += time.perf_counter() - t1
                        if rss_data:
                            rec.plot_summary = rec.plot_summary or rss_data.get("plot_summary")
                            rec.release_date = rec.release_date or rss_data.get("release_date")
                    if newsdata and not rec.plot_summary:
                        t1 = time.perf_counter()
                        nd_data = newsdata.fetch(title, rec.film_year)
                        t_nd += time.perf_counter() - t1
                        if nd_data:
                            rec.plot_summary = rec.plot_summary or nd_data.get("plot_summary")
                            rec.release_date = rec.release_date or nd_data.get("release_date")
                    time.sleep(enr.newsdata_delay)
                logger.info(
                    "[Incremental] TIMING — RSS: %.2fs | Newsdata: %.2fs",
                    t_rss, t_nd,
                )

        return records

    # ── Internal: re-export ───────────────────────────────────────────────────

    def _reexport(self, rows: List[dict], run_id: str, db_path: Path) -> None:
        """Write updated CSV, JSON, and Excel next to the DB that was updated."""
        out_dir = db_path.parent

        if self._cfg.storage.csv.enabled:
            out_csv = out_dir / "nfa_awards.csv"
            CSVExporter(self._cfg.storage.csv, self._cfg.paths,
                        out_path=out_csv).export(rows)
            logger.info("[Incremental] CSV re-exported → %s (%d rows)", out_csv, len(rows))

        if self._cfg.storage.json.enabled:
            out_json = out_dir / "nfa_awards.json"
            JSONExporter(self._cfg.storage.json, self._cfg.paths,
                         out_path=out_json).export(rows, run_id=run_id)
            logger.info("[Incremental] JSON re-exported → %s (%d rows)", out_json, len(rows))

        if self._cfg.storage.excel.enabled:
            from .storage import ExcelExporter
            out_xlsx = out_dir / "nfa_awards.xlsx"
            ExcelExporter(self._cfg.storage.excel, self._cfg.paths,
                          out_path=out_xlsx).export(rows)
            logger.info("[Incremental] Excel re-exported → %s (%d rows)", out_xlsx, len(rows))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _ensure_dirs(self) -> None:
        for attr in ("raw_dir", "processed_dir", "logs_dir", "cache_dir"):
            Path(getattr(self._cfg.paths, attr)).mkdir(parents=True, exist_ok=True)


# ── Utility: override DB path on a config object ─────────────────────────────

def _override_db_path(sqlite_cfg, new_path: Path):
    """Return a copy-like object with a different ``path`` attribute."""
    from dataclasses import replace as _replace
    try:
        return _replace(sqlite_cfg, path=str(new_path))
    except TypeError:
        class _Cfg:
            pass
        c = _Cfg()
        c.__dict__.update(sqlite_cfg.__dict__)
        c.path = str(new_path)
        return c


# ── Utility: reconstruct AwardRecord from a SQLite row dict ──────────────────

def _row_to_award_record(row: dict) -> AwardRecord:
    """Convert a raw SQLite row dict back into an AwardRecord."""
    def _bool(v) -> Optional[bool]:
        if v is None:
            return None
        return bool(int(v))

    def _lst(v) -> list:
        if not v:
            return []
        try:
            return json.loads(v)
        except Exception:
            return []

    return AwardRecord(
        ceremony_year        = int(row["ceremony_year"]),
        category             = row["category"] or "Unknown",
        film_title           = row["film_title"] or "Unknown",
        record_id            = row.get("record_id"),
        ceremony_number      = row.get("ceremony_number"),
        category_normalized  = row.get("category_normalized"),
        award_type           = row.get("award_type"),
        cash_prize           = row.get("cash_prize"),
        film_title_original  = row.get("film_title_original"),
        film_language        = row.get("film_language"),
        film_year            = row.get("film_year"),
        director             = row.get("director"),
        awardee              = row.get("awardee"),
        production_company   = row.get("production_company"),
        imdb_id              = row.get("imdb_id"),
        imdb_rating          = row.get("imdb_rating"),
        imdb_votes           = row.get("imdb_votes"),
        runtime_minutes      = row.get("runtime_minutes"),
        genres               = row.get("genres"),
        plot_summary         = row.get("plot_summary"),
        omdb_box_office      = row.get("omdb_box_office"),
        omdb_poster_url      = row.get("omdb_poster_url"),
        omdb_released        = row.get("omdb_released"),
        box_office_worldwide = row.get("box_office_worldwide"),
        release_date         = row.get("release_date"),
        is_sequel            = _bool(row.get("is_sequel")),
        is_remake            = _bool(row.get("is_remake")),
        crew_size            = row.get("crew_size"),
        data_source          = row.get("data_source"),
        source_url           = row.get("source_url"),
        scraped_at           = None,
        raw_html_hash        = row.get("raw_html_hash"),
        is_valid             = bool(row.get("is_valid", 1)),
        validation_warnings  = _lst(row.get("validation_warnings")),
        validation_errors    = _lst(row.get("validation_errors")),
    )

"""
nfa_pipeline/pipeline.py
--------------------------
Top-level orchestrator that ties together all pipeline stages:

  Extract → Transform → Validate → Store

Usage (programmatic)
---------------------
    from nfa_pipeline.pipeline import NFAPipeline
    pipeline = NFAPipeline()
    result = pipeline.run(years=range(2000, 2024))
    print(result)

The pipeline is designed to be:
* **Resumable** — completed years are checkpointed; a re-run skips them
* **Fault-tolerant** — one bad year doesn't abort the entire run
* **Observable** — structured logs + a final summary at every stage
"""

from __future__ import annotations

import uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

from .config import get_config, Config
from .models import AwardRecord, ScrapeRun
from .extractors import NFAExtractor
from .extractors.multi_source_extractor import MultiSourceExtractor
from .transformers import RecordTransformer
from .validators import RecordValidator
from .storage import SQLiteStorage, ExcelExporter, CSVExporter, JSONExporter
from .utils import HTTPClient, CheckpointManager, setup_logging, PipelineProgress

logger = logging.getLogger(__name__)


class NFAPipeline:
    """
    Orchestrates the full NFA data pipeline.

    Parameters
    ----------
    config_path : Path, optional
        Path to a custom ``settings.yaml``.  Defaults to the bundled config.
    force_rescrape : bool
        If ``True``, ignore the checkpoint and re-scrape all years.
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        force_rescrape: bool = False,
    ):
        self._cfg: Config = get_config(config_path)
        self._force = force_rescrape
        self._ensure_dirs()

        # Initialise logging
        setup_logging(
            level=self._cfg.logging.level,
            log_file=Path(self._cfg.paths.logs_dir) / "pipeline.log",
        )

        logger.info("=" * 60)
        logger.info("NFA Pipeline v1.0.0 — initialised")
        logger.info("Years: %d–%d", self._cfg.scraper.start_year, self._cfg.scraper.end_year)
        logger.info("=" * 60)

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(
        self,
        years: Optional[Iterable[int]] = None,
        force_rescrape: Optional[bool] = None,
    ) -> ScrapeRun:
        """
        Execute the full pipeline for *years*.

        Parameters
        ----------
        years : iterable of int, optional
            Ceremony years to process.  Defaults to ``start_year`` →
            ``end_year`` from config.
        force_rescrape : bool, optional
            Override the instance-level ``force_rescrape`` flag.

        Returns
        -------
        ScrapeRun
            Metadata object summarising the run outcome.
        """
        _force = force_rescrape if force_rescrape is not None else self._force

        all_years = list(
            years if years is not None
            else range(self._cfg.scraper.start_year, self._cfg.scraper.end_year + 1)
        )

        run = ScrapeRun(
            run_id=str(uuid.uuid4()),
            started_at=datetime.utcnow(),
            years_requested=all_years,
        )
        logger.info("Run %s started — %d years requested", run.run_id, len(all_years))

        checkpoint = CheckpointManager(Path(self._cfg.paths.checkpoint_file))

        # Filter years using checkpoint (unless forced)
        if _force:
            years_to_process = all_years
            logger.info("Force-rescrape: processing all %d years", len(years_to_process))
        else:
            years_to_process = checkpoint.remaining_years(all_years)
            skipped = len(all_years) - len(years_to_process)
            if skipped:
                logger.info("Checkpoint: skipping %d already-done years", skipped)

        all_records: List[AwardRecord] = []

        with HTTPClient(self._cfg.scraper, self._cfg.paths, self._cfg.cache) as http:
            extractor   = MultiSourceExtractor(http, self._cfg)
            transformer = RecordTransformer(self._cfg.transformer)
            validator   = RecordValidator(self._cfg.validator)

            progress = PipelineProgress(len(years_to_process), "Scraping years")

            for year in years_to_process:
                try:
                    # ── Stage 1: Extract ──────────────────────────────────────
                    page = extractor.extract_year(year)

                    if page.parse_error and not page.records:
                        logger.warning("Year %d: extraction failed — %s", year, page.parse_error)
                        checkpoint.mark_failed(year, page.parse_error)
                        run.years_failed.append(year)
                        progress.update(message=f"year {year} FAILED")
                        continue

                    # ── Stage 2: Transform ────────────────────────────────────
                    transformed = transformer.transform_batch(page.records)

                    # ── Stage 3: Validate ─────────────────────────────────────
                    validated, _ = validator.validate_batch(transformed)

                    all_records.extend(validated)
                    checkpoint.mark_done(year)
                    run.years_scraped.append(year)
                    progress.update(message=f"year {year} OK ({len(validated)} records)")

                except Exception as exc:
                    logger.exception("Year %d: unexpected error — %s", year, exc)
                    checkpoint.mark_failed(year, str(exc))
                    run.years_failed.append(year)
                    progress.update(message=f"year {year} ERROR")

            progress.done()

        # ── Stage 4: Batch validation report ─────────────────────────────────
        logger.info("Running batch validation on %d total records …", len(all_records))
        _, batch_report = validator.validate_batch(all_records)

        run.total_records   = batch_report.total
        run.valid_records   = batch_report.valid
        run.invalid_records = batch_report.invalid
        run.warnings_count  = batch_report.warning_count

        # ── Stage 5: Store ────────────────────────────────────────────────────
        self._store(all_records, batch_report, run_id=run.run_id)

        # Finalise run
        run.finished_at = datetime.utcnow()
        run.status = "completed" if not run.years_failed else "partial"
        if run.years_failed:
            run.error_message = f"Failed years: {run.years_failed}"

        self._log_summary(run, batch_report)
        return run

    # ── Storage stage ─────────────────────────────────────────────────────────

    def _store(self, records: List[AwardRecord], report, run_id: str = "") -> None:
        record_dicts = [r.model_dump() for r in records]

        # ── Per-run output subfolder ───────────────────────────────────────────
        run_dir = self._make_run_dir(run_id)

        # ── SQLite (always uses canonical path) ───────────────────────────────
        if self._cfg.storage.sqlite.enabled:
            with SQLiteStorage(self._cfg.storage.sqlite) as db:
                db.upsert_records(records)

        # ── Excel ─────────────────────────────────────────────────────────────
        if self._cfg.storage.excel.enabled:
            try:
                # Canonical path
                ExcelExporter(self._cfg.storage.excel, self._cfg.paths).export(
                    record_dicts, validation_report=report)
                # Per-run copy
                if run_dir:
                    ExcelExporter(
                        self._cfg.storage.excel, self._cfg.paths,
                        out_path=run_dir / "nfa_awards.xlsx"
                    ).export(record_dicts, validation_report=report)
            except ImportError:
                logger.warning("openpyxl not installed — skipping Excel export")

        # ── CSV ───────────────────────────────────────────────────────────────
        if self._cfg.storage.csv.enabled:
            CSVExporter(self._cfg.storage.csv, self._cfg.paths).export(record_dicts)
            if run_dir:
                CSVExporter(
                    self._cfg.storage.csv, self._cfg.paths,
                    out_path=run_dir / "nfa_awards.csv"
                ).export(record_dicts)

        # ── JSON ──────────────────────────────────────────────────────────────
        if self._cfg.storage.json.enabled:
            JSONExporter(self._cfg.storage.json, self._cfg.paths).export(
                record_dicts, run_id=run_id)
            if run_dir:
                JSONExporter(
                    self._cfg.storage.json, self._cfg.paths,
                    out_path=run_dir / "nfa_awards.json"
                ).export(record_dicts, run_id=run_id)

    def _make_run_dir(self, run_id: str) -> Optional[Path]:
        """Create a timestamped subfolder under processed/ for this run."""
        if not getattr(self._cfg.storage, "per_run_output_folder", True):
            return None
        from datetime import datetime as _dt
        ts = _dt.utcnow().strftime("%Y%m%d_%H%M%S")
        short_id = run_id[:8] if run_id else "unknown"
        run_dir = Path(self._cfg.paths.processed_dir) / f"run_{ts}_{short_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Per-run output folder: %s", run_dir)
        return run_dir

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _ensure_dirs(self) -> None:
        for attr in ("raw_dir", "processed_dir", "logs_dir", "cache_dir"):
            Path(getattr(self._cfg.paths, attr)).mkdir(parents=True, exist_ok=True)

    def _log_summary(self, run: ScrapeRun, report) -> None:
        dur = run.duration_seconds or 0
        logger.info("")
        logger.info("━" * 60)
        logger.info("PIPELINE COMPLETE")
        logger.info("  Run ID       : %s", run.run_id)
        logger.info("  Duration     : %.1fs", dur)
        logger.info("  Years OK     : %d / %d", len(run.years_scraped), len(run.years_requested))
        logger.info("  Years Failed : %d", len(run.years_failed))
        logger.info("  Total Records: %d", run.total_records)
        logger.info("  Valid        : %d (%.1f%%)", run.valid_records, report.completeness * 100)
        logger.info("  Invalid      : %d", run.invalid_records)
        logger.info("  Warnings     : %d", run.warnings_count)
        logger.info("  Status       : %s", run.status.upper())
        logger.info("━" * 60)

        if run.years_failed:
            logger.warning("Failed years: %s", run.years_failed)

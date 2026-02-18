"""
nfa_pipeline/storage/csv_exporter.py
--------------------------------------
Exports award records to UTF-8 CSV (BOM for Excel compatibility).
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_FIELDNAMES = [
    # Core award fields
    "record_id", "ceremony_year", "ceremony_number",
    "film_title", "film_title_original", "film_language", "film_year",
    "category", "category_normalized", "award_type",
    "director", "awardee", "production_company", "cash_prize",
    # IMDb / OMDb enrichment
    "imdb_id", "imdb_rating", "imdb_votes", "runtime_minutes",
    "genres", "box_office_worldwide", "release_date",
    "omdb_box_office", "omdb_released", "omdb_poster_url", "plot_summary",
    # New fields
    "is_sequel", "is_remake", "crew_size",
    # Provenance
    "source_url", "scraped_at", "data_source",
    # QA
    "is_valid", "validation_warnings", "validation_errors",
]


class CSVExporter:
    """Exports :class:`AwardRecord`-derived dicts to CSV."""

    def __init__(self, storage_cfg, paths_cfg, out_path: Optional[Path] = None):
        self._cfg = storage_cfg
        self._out_path = out_path or Path(paths_cfg.output_csv)
        self._out_path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, records: List[Dict[str, Any]]) -> Path:
        """Write *records* to CSV. Returns the output path."""
        with open(self._out_path, "w", newline="", encoding=self._cfg.encoding) as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=_FIELDNAMES,
                delimiter=self._cfg.delimiter,
                extrasaction="ignore",
            )
            writer.writeheader()
            for rec in records:
                row = {k: rec.get(k, "") for k in _FIELDNAMES}
                # Flatten list fields
                for lf in ("validation_warnings", "validation_errors"):
                    v = row.get(lf)
                    if isinstance(v, list):
                        row[lf] = "; ".join(v)
                writer.writerow(row)

        logger.info("CSV exported: %s (%d rows)", self._out_path, len(records))
        return self._out_path

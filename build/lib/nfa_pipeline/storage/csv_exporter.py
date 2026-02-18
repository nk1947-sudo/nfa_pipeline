"""
nfa_pipeline/storage/csv_exporter.py
--------------------------------------
Exports award records to UTF-8 CSV (BOM for Excel compatibility).
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_FIELDNAMES = [
    "ceremony_year", "ceremony_number", "film_title", "film_title_original",
    "film_language", "film_year", "category", "category_normalized",
    "award_type", "director", "awardee", "production_company",
    "cash_prize", "source_url", "scraped_at", "is_valid",
    "validation_warnings", "validation_errors",
]


class CSVExporter:
    """Exports :class:`AwardRecord`-derived dicts to CSV."""

    def __init__(self, storage_cfg, paths_cfg):
        self._cfg = storage_cfg
        self._out_path = Path(paths_cfg.output_csv)
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
                writer.writerow(row)

        logger.info("CSV exported: %s (%d rows)", self._out_path, len(records))
        return self._out_path

"""
nfa_pipeline/storage/json_exporter.py
---------------------------------------
Exports NFA award records to a structured JSON file.

Output format
-------------
{
  "metadata": {
    "generated_at": "...",
    "total_records": N,
    "years": [...]
  },
  "records": [ { ...record fields... }, ... ]
}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class JSONExporter:
    """
    Exports award records to a JSON file.

    Parameters
    ----------
    storage_cfg
        JSON sub-config (enabled, indent).
    paths_cfg
        Paths config (output_json path).
    out_path : Path, optional
        Override the output path (used for per-run folders).
    """

    def __init__(self, storage_cfg, paths_cfg, out_path: Optional[Path] = None):
        self._cfg = storage_cfg
        self._out_path = out_path or Path(paths_cfg.output_json)
        self._out_path.parent.mkdir(parents=True, exist_ok=True)

    def export(
        self,
        records: List[Dict[str, Any]],
        run_id: Optional[str] = None,
    ) -> Path:
        """
        Write *records* to JSON. Returns the output path.

        Parameters
        ----------
        records : list of dict
            Records from AwardRecord.model_dump().
        run_id : str, optional
            Pipeline run ID to include in metadata.
        """
        years = sorted({r.get("ceremony_year") for r in records if r.get("ceremony_year")})

        envelope = {
            "metadata": {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "run_id": run_id,
                "total_records": len(records),
                "years": years,
                "fields": list(records[0].keys()) if records else [],
            },
            "records": records,
        }

        indent = getattr(self._cfg, "indent", 2)
        with open(self._out_path, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, indent=indent, ensure_ascii=False, default=str)

        logger.info("JSON exported: %s (%d records)", self._out_path, len(records))
        return self._out_path

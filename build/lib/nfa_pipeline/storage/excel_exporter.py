"""
nfa_pipeline/storage/excel_exporter.py
----------------------------------------
Exports NFA award records to a professionally formatted Excel workbook.

Sheets
------
* **NFA Awards** — all records, one row each, with auto-filter and freeze
* **Summary** — pivot-style yearly counts and award type breakdown
* **Validation Report** — records that failed validation

Formatting
----------
* Header row: bold, white text, dark teal background
* Alternating row shading for readability
* Column widths auto-fitted to content
* Hyperlinks in source_url column
* Frozen first row + first column
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Columns in output order
_COLUMNS = [
    ("ceremony_year",       "Year",                 8),
    ("ceremony_number",     "Ceremony #",           11),
    ("film_title",          "Film Title",           35),
    ("film_title_original", "Original Title",       30),
    ("film_language",       "Language",             14),
    ("film_year",           "Film Year",            10),
    ("category",            "Award Category",       40),
    ("category_normalized", "Category Slug",        30),
    ("award_type",          "Award Type",           16),
    ("director",            "Director",             28),
    ("awardee",             "Awardee",              28),
    ("production_company",  "Production Co.",       30),
    ("cash_prize",          "Cash Prize",           12),
    ("source_url",          "Source URL",           50),
    ("scraped_at",          "Scraped At",           20),
    ("is_valid",            "Valid",                7),
]


class ExcelExporter:
    """
    Exports award records to Excel.

    Parameters
    ----------
    storage_cfg
        Excel sub-config (sheet name, freeze panes, auto-filter).
    paths_cfg
        Paths config (output file path).
    """

    def __init__(self, storage_cfg, paths_cfg):
        self._cfg = storage_cfg
        self._out_path = Path(paths_cfg.output_excel)
        self._out_path.parent.mkdir(parents=True, exist_ok=True)

    def export(
        self,
        records: List[Dict[str, Any]],
        validation_report: Optional[Any] = None,
    ) -> Path:
        """
        Write *records* to an Excel workbook at the configured path.

        Returns the path to the written file.
        """
        try:
            import openpyxl
            from openpyxl.styles import (
                Alignment, Border, Font, PatternFill, Side
            )
            from openpyxl.utils import get_column_letter
        except ImportError as exc:
            logger.error("openpyxl not installed — cannot export to Excel: %s", exc)
            raise

        wb = openpyxl.Workbook()

        # ── Main data sheet ───────────────────────────────────────────────────
        ws = wb.active
        ws.title = self._cfg.sheet_name

        # Styles
        header_fill = PatternFill("solid", fgColor="1F5C7A")
        header_font = Font(bold=True, color="FFFFFF", name="Calibri", size=11)
        data_font   = Font(name="Calibri", size=10)
        alt_fill    = PatternFill("solid", fgColor="EEF4F7")
        border_side = Side(border_style="thin", color="CCCCCC")
        thin_border = Border(
            left=border_side, right=border_side,
            top=border_side,  bottom=border_side,
        )
        center_align = Alignment(horizontal="center", vertical="center")
        wrap_align   = Alignment(wrap_text=False, vertical="center")

        # Header row
        col_keys = [c[0] for c in _COLUMNS]
        col_hdrs = [c[1] for c in _COLUMNS]
        col_wids = [c[2] for c in _COLUMNS]

        ws.append(col_hdrs)
        for col_idx, _ in enumerate(col_hdrs, start=1):
            cell = ws.cell(row=1, column=col_idx)
            cell.font   = header_font
            cell.fill   = header_fill
            cell.border = thin_border
            cell.alignment = center_align
            ws.column_dimensions[get_column_letter(col_idx)].width = col_wids[col_idx - 1]

        ws.row_dimensions[1].height = 20

        # Data rows
        for row_idx, rec in enumerate(records, start=2):
            is_alt = row_idx % 2 == 0
            for col_idx, key in enumerate(col_keys, start=1):
                val = rec.get(key)
                if isinstance(val, bool):
                    val = "Yes" if val else "No"
                elif key == "is_valid":
                    val = "✓" if val else "✗"

                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.font      = data_font
                cell.alignment = wrap_align
                cell.border    = thin_border
                if is_alt:
                    cell.fill = alt_fill

                # Hyperlink for source URL column
                if key == "source_url" and isinstance(val, str) and val.startswith("http"):
                    cell.hyperlink = val
                    cell.font = Font(
                        name="Calibri", size=10,
                        color="0563C1", underline="single",
                    )

        # Freeze panes
        if self._cfg.freeze_panes:
            ws.freeze_panes = "B2"

        # Auto-filter
        if self._cfg.auto_filter:
            ws.auto_filter.ref = ws.dimensions

        # ── Summary sheet ─────────────────────────────────────────────────────
        if self._cfg.summary_sheet:
            self._write_summary_sheet(wb, records, header_fill, header_font, data_font)

        # ── Invalid records sheet ─────────────────────────────────────────────
        invalid = [r for r in records if not r.get("is_valid", True)]
        if invalid:
            self._write_invalid_sheet(wb, invalid, header_fill, header_font, data_font)

        wb.save(str(self._out_path))
        logger.info("Excel exported: %s (%d rows)", self._out_path, len(records))
        return self._out_path

    # ── Summary sheet ─────────────────────────────────────────────────────────

    def _write_summary_sheet(self, wb, records, header_fill, header_font, data_font):
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter

        ws = wb.create_sheet("Summary")
        ws.column_dimensions["A"].width = 12
        ws.column_dimensions["B"].width = 18
        ws.column_dimensions["C"].width = 18
        ws.column_dimensions["D"].width = 22

        # Title
        ws["A1"] = "NFA Awards — Pipeline Summary"
        ws["A1"].font = Font(bold=True, size=14, name="Calibri")
        ws["A1"].alignment = Alignment(horizontal="left")
        ws.merge_cells("A1:D1")
        ws.row_dimensions[1].height = 24

        # Year breakdown header
        headers = ["Year", "Total Awards", "Valid Records", "Languages"]
        ws.append([])
        ws.append(headers)
        for c in range(1, 5):
            cell = ws.cell(row=3, column=c)
            cell.font = header_font
            cell.fill = header_fill

        # Count by year
        from collections import defaultdict
        by_year: dict = defaultdict(lambda: {"total": 0, "valid": 0, "langs": set()})
        for r in records:
            y = r.get("ceremony_year")
            if y:
                by_year[y]["total"] += 1
                if r.get("is_valid"):
                    by_year[y]["valid"] += 1
                lang = r.get("film_language")
                if lang:
                    by_year[y]["langs"].add(lang)

        for year in sorted(by_year):
            d = by_year[year]
            ws.append([
                year,
                d["total"],
                d["valid"],
                ", ".join(sorted(d["langs"])),
            ])

        ws.freeze_panes = "A4"

    # ── Invalid records sheet ─────────────────────────────────────────────────

    def _write_invalid_sheet(self, wb, invalid_records, header_fill, header_font, data_font):
        ws = wb.create_sheet("Invalid Records")
        headers = ["Year", "Film Title", "Category", "Errors", "Warnings"]
        ws.append(headers)
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=c)
            cell.font = header_font
            cell.fill = header_fill

        ws.column_dimensions["A"].width = 8
        ws.column_dimensions["B"].width = 35
        ws.column_dimensions["C"].width = 35
        ws.column_dimensions["D"].width = 50
        ws.column_dimensions["E"].width = 50

        for rec in invalid_records:
            ws.append([
                rec.get("ceremony_year"),
                rec.get("film_title"),
                rec.get("category"),
                rec.get("validation_errors", ""),
                rec.get("validation_warnings", ""),
            ])

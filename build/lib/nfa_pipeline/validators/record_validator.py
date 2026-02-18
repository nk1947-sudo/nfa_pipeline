"""
nfa_pipeline/validators/record_validator.py
--------------------------------------------
Multi-level validation for :class:`AwardRecord` objects.

Validation levels
-----------------
* **ERROR** — blocking failures; record is marked ``is_valid=False``
* **WARNING** — non-blocking issues logged and attached to the record

Checks performed
----------------
Field-level
  * Required fields present and non-empty
  * String length limits respected
  * Year values in expected range

Cross-field
  * film_year <= ceremony_year (a film can't win before it exists)

Batch-level
  * Awards-per-year count within expected window
  * Overall completeness above threshold
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Tuple

from ..models import AwardRecord

logger = logging.getLogger(__name__)


# ── Validation result ──────────────────────────────────────────────────────────

@dataclass
class ValidationReport:
    """Aggregated validation outcome for a batch of records."""

    total: int = 0
    valid: int = 0
    invalid: int = 0
    warning_count: int = 0
    errors_by_field: dict = field(default_factory=dict)
    year_counts: dict = field(default_factory=dict)

    @property
    def completeness(self) -> float:
        return self.valid / self.total if self.total else 0.0

    @property
    def passed(self) -> bool:
        return self.completeness >= 0.70

    def add_error(self, field_name: str) -> None:
        self.errors_by_field[field_name] = self.errors_by_field.get(field_name, 0) + 1

    def summary(self) -> str:
        return (
            f"ValidationReport: {self.total} records | "
            f"{self.valid} valid ({self.completeness:.1%}) | "
            f"{self.invalid} invalid | "
            f"{self.warning_count} warnings"
        )


# ── Validator ─────────────────────────────────────────────────────────────────

class RecordValidator:
    """
    Validates individual :class:`AwardRecord` objects and batches.

    Parameters
    ----------
    validator_cfg
        Validator configuration block from the pipeline config.
    """

    def __init__(self, validator_cfg):
        self._cfg = validator_cfg

    # ── Public API ─────────────────────────────────────────────────────────────

    def validate(self, record: AwardRecord) -> Tuple[bool, List[str], List[str]]:
        """
        Validate a single record.

        Returns
        -------
        (is_valid, errors, warnings)
            * ``is_valid`` — ``False`` if any blocking error was found
            * ``errors``   — list of blocking error messages
            * ``warnings`` — list of non-blocking warning messages
        """
        errors: List[str] = []
        warnings: List[str] = []

        self._check_required_fields(record, errors)
        self._check_string_lengths(record, errors, warnings)
        self._check_year_range(record, errors, warnings)
        self._check_cross_field(record, warnings)

        is_valid = len(errors) == 0
        record.is_valid = is_valid
        record.validation_errors = errors
        record.validation_warnings = warnings

        if errors:
            logger.debug("INVALID record %r: %s", record.record_id, "; ".join(errors))
        elif warnings:
            logger.debug("WARNINGS for %r: %s", record.record_id, "; ".join(warnings))

        return is_valid, errors, warnings

    def validate_batch(
        self, records: List[AwardRecord]
    ) -> Tuple[List[AwardRecord], ValidationReport]:
        """
        Validate a list of records, returning them with flags set and a report.

        Parameters
        ----------
        records : list
            Records to validate (mutated in-place with error/warning lists).

        Returns
        -------
        (records, report)
        """
        report = ValidationReport(total=len(records))

        for record in records:
            is_valid, errors, warnings = self.validate(record)
            if is_valid:
                report.valid += 1
            else:
                report.invalid += 1
                for err in errors:
                    field_name = err.split(":")[0].strip()
                    report.add_error(field_name)

            report.warning_count += len(warnings)

            # Tally per-year counts
            y = record.ceremony_year
            if y not in report.year_counts:
                report.year_counts[y] = {"total": 0, "valid": 0}
            report.year_counts[y]["total"] += 1
            if is_valid:
                report.year_counts[y]["valid"] += 1

        self._check_batch_consistency(report)

        logger.info("%s", report.summary())
        if not report.passed:
            logger.warning(
                "Completeness %.1f%% is below threshold %.1f%%",
                report.completeness * 100,
                self._cfg.completeness_threshold * 100,
            )

        return records, report

    # ── Field-level checks ────────────────────────────────────────────────────

    def _check_required_fields(self, record: AwardRecord, errors: List[str]) -> None:
        for fname in self._cfg.required_fields:
            val = getattr(record, fname, None)
            if val is None or (isinstance(val, str) and not val.strip()):
                errors.append(f"{fname}: required field is missing or empty")

    def _check_string_lengths(
        self,
        record: AwardRecord,
        errors: List[str],
        warnings: List[str],
    ) -> None:
        checks = [
            ("film_title", self._cfg.max_film_title_length, True),
            ("director", self._cfg.max_director_length, False),
            ("awardee", self._cfg.max_director_length, False),
            ("category", 300, False),
        ]
        for fname, max_len, is_blocking in checks:
            val = getattr(record, fname, None)
            if isinstance(val, str) and len(val) > max_len:
                msg = f"{fname}: value exceeds max length {max_len} (got {len(val)})"
                if is_blocking:
                    errors.append(msg)
                else:
                    warnings.append(msg)

    def _check_year_range(
        self,
        record: AwardRecord,
        errors: List[str],
        warnings: List[str],
    ) -> None:
        y = record.ceremony_year
        if y is None:
            return  # already caught by required-fields check
        if not (self._cfg.year_min <= y <= self._cfg.year_max):
            errors.append(
                f"ceremony_year: {y} outside expected range "
                f"[{self._cfg.year_min}, {self._cfg.year_max}]"
            )

        fy = record.film_year
        if fy is not None and not (self._cfg.year_min <= fy <= self._cfg.year_max):
            warnings.append(f"film_year: {fy} outside expected range")

    def _check_cross_field(self, record: AwardRecord, warnings: List[str]) -> None:
        if record.film_year and record.ceremony_year:
            if record.film_year > record.ceremony_year:
                warnings.append(
                    f"film_year ({record.film_year}) > ceremony_year ({record.ceremony_year})"
                )

    # ── Batch-level checks ─────────────────────────────────────────────────────

    def _check_batch_consistency(self, report: ValidationReport) -> None:
        lo = self._cfg.expected_awards_per_year_min
        hi = self._cfg.expected_awards_per_year_max
        for year, counts in report.year_counts.items():
            total = counts["total"]
            if not (lo <= total <= hi):
                logger.warning(
                    "Year %d: %d awards (expected %d–%d)",
                    year, total, lo, hi,
                )

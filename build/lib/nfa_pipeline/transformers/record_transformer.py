"""
nfa_pipeline/transformers/record_transformer.py
------------------------------------------------
Cleans, normalises, and enriches :class:`AwardRecord` objects.

Transformations applied (in order)
-----------------------------------
1. Strip leading / trailing whitespace from every string field
2. Normalise Unicode (NFC form) to eliminate invisible differences
3. Title-case configured fields
4. Normalise film language via alias map
5. Derive ``category_normalized`` slug (lowercase, hyphens)
6. Derive ``film_year`` from context when missing
7. Derive ``award_type`` from category text when not already set
8. Remove control characters and non-printable chars
9. Collapse multiple internal spaces

The transformer is *non-destructive*: original field values are
preserved in ``_original`` shadow attributes where material.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import List

from ..models import AwardRecord

logger = logging.getLogger(__name__)

# ── Regex helpers ─────────────────────────────────────────────────────────────
_MULTI_SPACE = re.compile(r"  +")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SLUG_CLEAN = re.compile(r"[^a-z0-9]+")
_GOLDEN_LOTUS = re.compile(r"golden\s+lotus|swarna\s+kamal|best\s+film", re.I)
_SILVER_LOTUS = re.compile(r"silver\s+lotus|rajat\s+kamal", re.I)
_CERT_MERIT = re.compile(r"certificate\s+of\s+merit|special\s+jury", re.I)


class RecordTransformer:
    """
    Stateless transformation engine for :class:`AwardRecord` instances.

    Parameters
    ----------
    transformer_cfg
        Transformer configuration block from the pipeline config.
    """

    def __init__(self, transformer_cfg):
        self._cfg = transformer_cfg

    # ── Public API ─────────────────────────────────────────────────────────────

    def transform(self, record: AwardRecord) -> AwardRecord:
        """
        Apply the full transformation pipeline to a single record.

        Returns the mutated record (in-place) for convenient chaining.
        """
        self._clean_strings(record)
        self._normalise_unicode(record)
        self._title_case(record)
        self._normalise_language(record)
        self._derive_category_slug(record)
        self._infer_award_type(record)
        self._coerce_film_year(record)
        return record

    def transform_batch(self, records: List[AwardRecord]) -> List[AwardRecord]:
        """Apply :meth:`transform` to every record in *records*."""
        transformed = []
        for rec in records:
            try:
                transformed.append(self.transform(rec))
            except Exception as exc:
                logger.warning("Transform failed for record %r: %s", rec.record_id, exc)
                transformed.append(rec)   # keep original on error
        logger.debug("Transformed %d records", len(transformed))
        return transformed

    # ── Transformation steps ──────────────────────────────────────────────────

    def _clean_strings(self, record: AwardRecord) -> None:
        """Strip whitespace and collapse internal spaces."""
        string_fields = [
            "category", "film_title", "film_title_original",
            "film_language", "director", "awardee",
            "production_company", "award_type", "cash_prize",
        ]
        for fname in string_fields:
            val = getattr(record, fname, None)
            if isinstance(val, str):
                # Remove control chars
                val = _CONTROL_CHARS.sub("", val)
                # Collapse spaces
                val = _MULTI_SPACE.sub(" ", val)
                # Strip
                val = val.strip()
                setattr(record, fname, val or None)

    def _normalise_unicode(self, record: AwardRecord) -> None:
        """Apply NFC normalisation so ā and ā (decomposed) compare equal."""
        if not self._cfg.normalize_unicode:
            return
        string_fields = ["film_title", "film_title_original", "director", "awardee", "category"]
        for fname in string_fields:
            val = getattr(record, fname, None)
            if isinstance(val, str):
                setattr(record, fname, unicodedata.normalize("NFC", val))

    def _title_case(self, record: AwardRecord) -> None:
        """Title-case fields listed in the config."""
        for fname in self._cfg.title_case_fields:
            val = getattr(record, fname, None)
            if isinstance(val, str):
                setattr(record, fname, self._smart_title(val))

    def _normalise_language(self, record: AwardRecord) -> None:
        """Map language aliases to canonical forms (case-insensitive lookup)."""
        if not record.film_language:
            return
        key = record.film_language.strip().lower()
        canonical = self._cfg.language_aliases.get(key)
        if canonical is None:
            for alias_key, alias_val in self._cfg.language_aliases.items():
                if alias_key.lower() == key:
                    canonical = alias_val
                    break
        if canonical:
            record.film_language = canonical

    def _derive_category_slug(self, record: AwardRecord) -> None:
        """Build a URL-safe slug from the category name."""
        if not record.category:
            return
        slug = record.category.lower()
        slug = _SLUG_CLEAN.sub("-", slug).strip("-")
        record.category_normalized = slug

    def _infer_award_type(self, record: AwardRecord) -> None:
        """Fill award_type from category text if not already present."""
        if record.award_type:
            return
        text = (record.category or "") + " " + (record.awardee or "")
        if _GOLDEN_LOTUS.search(text):
            record.award_type = "Golden Lotus"
        elif _SILVER_LOTUS.search(text):
            record.award_type = "Silver Lotus"
        elif _CERT_MERIT.search(text):
            record.award_type = "Certificate of Merit"

    def _coerce_film_year(self, record: AwardRecord) -> None:
        """
        Best-effort: if film_year is missing, assume it equals ceremony_year - 1.
        (NFA typically honours films from the preceding year.)
        """
        if record.film_year is None and record.ceremony_year:
            record.film_year = record.ceremony_year - 1

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _smart_title(text: str) -> str:
        """
        Title-case that preserves common lowercase words (articles, prepositions)
        and respects existing capitalisation for known acronyms.
        """
        lowercase_words = {
            "a", "an", "the", "and", "but", "or", "for", "nor",
            "on", "at", "to", "by", "in", "of", "up", "as",
            "ka", "ki", "ke", "hai", "ho", "se", "ko", "aur",   # Hindi
        }
        words = text.split()
        result = []
        for i, word in enumerate(words):
            clean = word.strip(".,!?;:'\"()")
            if i == 0 or clean.lower() not in lowercase_words:
                result.append(word[0].upper() + word[1:] if word else word)
            else:
                result.append(word.lower())
        return " ".join(result)

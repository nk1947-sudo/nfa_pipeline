"""
nfa_pipeline/models.py
-----------------------
Data models for the NFA pipeline — stdlib dataclasses only.
Supports all enrichment fields from Wikipedia, OMDb, IMDb and Serper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@dataclass
class AwardRecord:
    """One NFA award entry, enriched from multiple sources."""

    # ── Required ──────────────────────────────────────────────────────────────
    ceremony_year:   int
    category:        str
    film_title:      str

    # ── Ceremony ──────────────────────────────────────────────────────────────
    record_id:           Optional[str] = None
    ceremony_number:     Optional[int] = None

    # ── Award ─────────────────────────────────────────────────────────────────
    category_normalized: Optional[str] = None
    award_type:          Optional[str] = None   # Golden Lotus / Silver Lotus / Certificate of Merit
    cash_prize:          Optional[str] = None

    # ── Film ──────────────────────────────────────────────────────────────────
    film_title_original: Optional[str] = None
    film_language:       Optional[str] = None
    film_year:           Optional[int] = None

    # ── People ────────────────────────────────────────────────────────────────
    director:            Optional[str] = None
    awardee:             Optional[str] = None
    production_company:  Optional[str] = None

    # ── IMDb / OMDb enrichment ────────────────────────────────────────────────
    imdb_id:             Optional[str]   = None
    imdb_rating:         Optional[str]   = None   # e.g. "8.4"
    imdb_votes:          Optional[str]   = None   # e.g. "125,432"
    runtime_minutes:     Optional[int]   = None
    genres:              Optional[str]   = None   # pipe-separated, up to 3 e.g. "Drama|Thriller"
    plot_summary:        Optional[str]   = None
    omdb_box_office:     Optional[str]   = None
    omdb_poster_url:     Optional[str]   = None
    omdb_released:       Optional[str]   = None
    box_office_worldwide:Optional[str]   = None
    release_date:        Optional[str]   = None

    # ── New enrichment fields ─────────────────────────────────────────────────
    is_sequel:           Optional[bool]  = None   # Is this film a sequel/franchise entry?
    is_remake:           Optional[bool]  = None   # Is this film a remake of an earlier film?
    crew_size:           Optional[int]   = None   # Total credited crew (from IMDb full credits)

    # ── Provenance ────────────────────────────────────────────────────────────
    data_source:         Optional[str]   = None   # wikipedia / wikipedia_api / omdb / imdb
    source_url:          Optional[str]   = None
    scraped_at:          Optional[datetime] = None
    raw_html_hash:       Optional[str]   = None

    # ── QA ────────────────────────────────────────────────────────────────────
    is_valid:            bool            = True
    validation_warnings: List[str]       = field(default_factory=list)
    validation_errors:   List[str]       = field(default_factory=list)

    def __post_init__(self):
        self.ceremony_year = int(self.ceremony_year)
        self.film_year     = _int(self.film_year)
        self.category      = _clean(self.category)  or "Unknown"
        self.film_title    = _clean(self.film_title) or "Unknown"
        self.director      = _clean(self.director)
        self.awardee       = _clean(self.awardee)
        self.film_language = _clean(self.film_language)
        self.award_type    = _clean(self.award_type)
        self.cash_prize    = _clean(self.cash_prize)

        if self.record_id is None:
            self.record_id = "|".join([
                str(self.ceremony_year),
                (self.category  or "")[:40],
                (self.film_title or "")[:60],
            ])

    def model_dump(self) -> dict:
        return {
            "record_id":           self.record_id,
            "ceremony_year":       self.ceremony_year,
            "ceremony_number":     self.ceremony_number,
            "category":            self.category,
            "category_normalized": self.category_normalized,
            "award_type":          self.award_type,
            "cash_prize":          self.cash_prize,
            "film_title":          self.film_title,
            "film_title_original": self.film_title_original,
            "film_language":       self.film_language,
            "film_year":           self.film_year,
            "director":            self.director,
            "awardee":             self.awardee,
            "production_company":  self.production_company,
            # IMDb / OMDb enrichment
            "imdb_id":             self.imdb_id,
            "imdb_rating":         self.imdb_rating,
            "imdb_votes":          self.imdb_votes,
            "runtime_minutes":     self.runtime_minutes,
            "genres":              self.genres,
            "plot_summary":        self.plot_summary,
            "omdb_box_office":     self.omdb_box_office,
            "omdb_poster_url":     self.omdb_poster_url,
            "omdb_released":       self.omdb_released,
            "box_office_worldwide":self.box_office_worldwide,
            "release_date":        self.release_date,
            # New fields
            "is_sequel":           self.is_sequel,
            "is_remake":           self.is_remake,
            "crew_size":           self.crew_size,
            # Provenance
            "data_source":         self.data_source,
            "source_url":          self.source_url,
            "scraped_at":          self.scraped_at.isoformat() if self.scraped_at else None,
            "raw_html_hash":       self.raw_html_hash,
            # QA
            "is_valid":            self.is_valid,
            "validation_warnings": self.validation_warnings,
            "validation_errors":   self.validation_errors,
        }


@dataclass
class ScrapeRun:
    run_id:          str
    started_at:      datetime
    finished_at:     Optional[datetime] = None
    years_requested: List[int]          = field(default_factory=list)
    years_scraped:   List[int]          = field(default_factory=list)
    years_failed:    List[int]          = field(default_factory=list)
    total_records:   int                = 0
    valid_records:   int                = 0
    invalid_records: int                = 0
    warnings_count:  int                = 0
    status:          str                = "running"
    error_message:   Optional[str]      = None

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    @property
    def success_rate(self) -> float:
        return self.valid_records / self.total_records if self.total_records else 0.0


@dataclass
class YearPage:
    year:        int
    url:         str
    html:        str
    http_status: int            = 200
    scraped_at:  Optional[datetime] = None
    parse_error: Optional[str]  = None
    records:     List[AwardRecord] = field(default_factory=list)
    source:      str            = "unknown"

    def __post_init__(self):
        if self.scraped_at is None:
            self.scraped_at = datetime.utcnow()

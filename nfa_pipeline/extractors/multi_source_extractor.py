"""
nfa_pipeline/extractors/multi_source_extractor.py
---------------------------------------------------
Orchestrates all data sources in priority order:

  1. Wikipedia REST API  — primary (official, robots-safe, structured)
  2. Wikipedia HTML      — fallback if API fails
  3. Serper enrichment   — fills gaps (director, language) via Google Search
  4. OMDb API            — film metadata (IMDb rating, runtime, genres, etc.)
  5. IMDb scraper        — fallback for films OMDb misses
  6. Newsdata.io         — last-resort: fills remaining nulls from news articles;
                           sets "Unknown" for any field with no news hit

Source selection is fully configurable. Each source degrades gracefully
when its API key is missing or the request fails.
"""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

from ..models import AwardRecord, YearPage
from .wikipedia_api_extractor import WikipediaAPIExtractor
from .wikipedia_extractor import WikipediaExtractor
from .omdb_extractor import OMDbExtractor
from .imdb_extractor import IMDbExtractor
from .serper_extractor import SerperExtractor
from .newsdata_extractor import NewsdataExtractor

logger = logging.getLogger(__name__)


class MultiSourceExtractor:
    """
    Coordinates extraction and enrichment from all configured sources.

    Source priority:
        Wikipedia API → Wikipedia HTML → Serper (gap-fill) → OMDb → IMDb

    Parameters
    ----------
    http_client : HTTPClient
        Shared HTTP client (handles caching, delays, retries).
    cfg : PipelineConfig
        Full pipeline config. Reads API keys from:
          cfg.enrichment.omdb_api_key  (or NFA_OMDB_API_KEY env var)
          cfg.enrichment.serper_api_key (or NFA_SERPER_API_KEY env var)
    """

    def __init__(self, http_client, cfg):
        self._http = http_client
        self._cfg  = cfg

        # ── Primary: Wikipedia API ─────────────────────────────────────────────
        self._wiki_api = WikipediaAPIExtractor(http_client)

        # ── Fallback: Wikipedia HTML direct ───────────────────────────────────
        self._wiki_html = WikipediaExtractor(http_client)

        # ── Enrichment: Serper (Google Search) ────────────────────────────────
        serper_key = (getattr(getattr(cfg, "enrichment", None), "serper_api_key", None)
                      or os.environ.get("NFA_SERPER_API_KEY", ""))
        self._serper = SerperExtractor(http_client, api_key=serper_key)

        # ── Enrichment: OMDb API ───────────────────────────────────────────────
        omdb_key = (getattr(getattr(cfg, "enrichment", None), "omdb_api_key", None)
                    or os.environ.get("NFA_OMDB_API_KEY", ""))
        self._omdb = OMDbExtractor(http_client, api_key=omdb_key)

        # ── Enrichment: IMDb scraper ───────────────────────────────────────────
        self._imdb = IMDbExtractor(http_client)

        # ── Enrichment: Newsdata.io (last resort) ─────────────────────────────
        enr = getattr(cfg, "enrichment", None)
        newsdata_key = (
            getattr(enr, "newsdata_api_key", "")
            or os.environ.get("NFA_NEWSDATA_API_KEY", "")
        )
        self._newsdata = NewsdataExtractor(
            api_key=newsdata_key,
            country=getattr(enr, "newsdata_country", "in"),
            language=getattr(enr, "newsdata_language", "en"),
            delay=getattr(enr, "newsdata_delay", 0.5),
        )
        self._newsdata_enabled = getattr(enr, "newsdata_enabled", True)

        self._log_sources()

    def _log_sources(self):
        logger.info("[MultiSource] Sources configured:")
        logger.info("  ✓ Wikipedia API    (primary, always on)")
        logger.info("  ✓ Wikipedia HTML   (fallback, always on)")
        logger.info("  %s Serper API      (gap-fill, key=%s)",
                    "✓" if self._serper.enabled else "✗ DISABLED",
                    "set" if self._serper.enabled else "missing → set NFA_SERPER_API_KEY")
        logger.info("  %s OMDb API        (enrichment, key=%s)",
                    "✓" if self._omdb.enabled else "✗ DISABLED",
                    "set" if self._omdb.enabled else "missing → set NFA_OMDB_API_KEY")
        logger.info("  ✓ IMDb scraper     (fallback enrichment, always on)")
        logger.info("  %s Newsdata.io     (last-resort, key=%s)",
                    "✓" if (self._newsdata.enabled and self._newsdata_enabled)
                      else "✗ DISABLED",
                    "set" if self._newsdata.enabled
                      else "missing → set NFA_NEWSDATA_API_KEY")

    # ── Public API ─────────────────────────────────────────────────────────────

    def extract_year(self, film_year: int) -> YearPage:
        """
        Extract all NFA award records for films released in film_year.
        Tries sources in priority order, returns best result.
        """
        logger.info("━" * 60)
        logger.info("Processing film_year=%d", film_year)
        t0 = time.perf_counter()

        # ── Step 1: NFA award data from Wikipedia ─────────────────────────────
        page = self._extract_nfa_data(film_year)
        t_wiki = time.perf_counter() - t0

        if not page.records:
            logger.warning("film_year=%d: No records from any NFA source", film_year)
            logger.info("film_year=%d: TIMING — wiki=%.2fs | total=%.2fs",
                        film_year, t_wiki, t_wiki)
            return page

        logger.info("film_year=%d: %d NFA records extracted (source=%s)",
                    film_year, len(page.records), page.source)

        # ── Step 2: Fill gaps via Serper (director, language) ─────────────────
        t1 = time.perf_counter()
        if self._serper.enabled:
            page.records = self._fill_gaps_serper(page.records)
        t_serper = time.perf_counter() - t1

        # ── Step 3: Enrich with OMDb API ──────────────────────────────────────
        t2 = time.perf_counter()
        if self._omdb.enabled:
            page.records = self._omdb.enrich_records_batch(
                page.records, delay=self._omdb_delay)
        t_omdb = time.perf_counter() - t2

        # ── Step 4: IMDb fallback for records OMDb missed ─────────────────────
        t3 = time.perf_counter()
        if self._should_use_imdb(page.records):
            page.records = self._imdb.enrich_records_batch(
                page.records, delay=self._imdb_delay)
        t_imdb = time.perf_counter() - t3

        # ── Step 5: Newsdata last-resort — fill remaining nulls ───────────────
        t4 = time.perf_counter()
        if self._newsdata_enabled and self._newsdata.enabled:
            page.records = self._newsdata.enrich_records(page.records)
        t_newsdata = time.perf_counter() - t4

        t_total = time.perf_counter() - t0

        # ── Summary ────────────────────────────────────────────────────────────
        with_imdb   = sum(1 for r in page.records if getattr(r, "imdb_id", None))
        with_rating = sum(1 for r in page.records if getattr(r, "imdb_rating", None))
        logger.info("film_year=%d: DONE — %d records, %d with IMDb ID, %d with rating",
                    film_year, len(page.records), with_imdb, with_rating)
        logger.info(
            "film_year=%d: TIMING — wiki=%.2fs | serper=%.2fs | omdb=%.2fs | imdb=%.2fs | newsdata=%.2fs | total=%.2fs",
            film_year, t_wiki, t_serper, t_omdb, t_imdb, t_newsdata, t_total,
        )

        return page

    def extract_years(self, years: List[int]) -> List[YearPage]:
        pages = []
        for i, year in enumerate(years, 1):
            logger.info("[%d/%d] Starting film_year=%d", i, len(years), year)
            try:
                pages.append(self.extract_year(year))
            except Exception as exc:
                logger.exception("Unexpected error for year %d: %s", year, exc)
                from ..models import YearPage as YP
                from .wikipedia_api_extractor import page_url
                pages.append(YP(year=year, url=page_url(year), html="",
                                parse_error=str(exc), source="error"))
        return pages

    # ── Private helpers ────────────────────────────────────────────────────────

    def _extract_nfa_data(self, film_year: int) -> YearPage:
        """Try Wikipedia API first, fall back to direct HTML scrape."""

        # Try 1: Wikipedia REST API
        logger.info("[Step 1a] Wikipedia REST API")
        t = time.perf_counter()
        page = self._wiki_api.extract_year(film_year)
        logger.info("[Step 1a] Wikipedia REST API — %.2fs → %d records",
                    time.perf_counter() - t, len(page.records))
        if page.records:
            return page

        # Try 2: Wikipedia HTML direct
        logger.info("[Step 1b] Wikipedia HTML direct (API failed or 0 records)")
        t = time.perf_counter()
        page = self._wiki_html.extract_year(film_year)
        logger.info("[Step 1b] Wikipedia HTML — %.2fs → %d records",
                    time.perf_counter() - t, len(page.records))
        if page.records:
            return page

        logger.warning("film_year=%d: Both Wikipedia sources returned 0 records", film_year)
        return page

    def _fill_gaps_serper(self, records: List[AwardRecord]) -> List[AwardRecord]:
        """Use Serper to fill missing director/language fields."""
        missing_director  = [r for r in records if not r.director]
        missing_language  = [r for r in records if not r.film_language]

        if missing_director:
            logger.info("[Step 2] Serper: filling director for %d records",
                        len(missing_director))
        if missing_language:
            logger.info("[Step 2] Serper: filling language for %d records",
                        len(missing_language))

        for rec in missing_director:
            try:
                d = self._serper.search_film_director(rec.film_title, rec.film_year)
                if d:
                    rec.director = d
                    logger.debug("[Serper] Director found for %r: %s",
                                 rec.film_title, d)
                time.sleep(0.1)
            except Exception:
                pass

        for rec in missing_language:
            try:
                lang = self._serper.search_film_language(rec.film_title, rec.film_year)
                if lang:
                    rec.film_language = lang
                    logger.debug("[Serper] Language found for %r: %s",
                                 rec.film_title, lang)
                time.sleep(0.1)
            except Exception:
                pass

        return records

    def _should_use_imdb(self, records: List[AwardRecord]) -> bool:
        """Use IMDb scraper only if significant records are still missing IMDb data."""
        missing = sum(1 for r in records if not getattr(r, "imdb_id", None))
        pct     = missing / len(records) if records else 0
        if missing > 0:
            logger.info("[Step 4] IMDb fallback: %d/%d records still missing IMDb data (%.0f%%)",
                        missing, len(records), pct * 100)
        return missing > 0

    @property
    def _omdb_delay(self) -> float:
        return 0.15   # ~6-7 req/s, well under OMDb free tier (1000/day)

    @property
    def _imdb_delay(self) -> float:
        return 1.0    # polite for scraping

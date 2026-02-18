"""
nfa_pipeline/extractors/omdb_extractor.py
------------------------------------------
Enriches NFA award records with film metadata from OMDb API.

OMDb API: https://www.omdbapi.com
Get a free key at omdbapi.com (1,000 req/day free).
Set env var: NFA_OMDB_API_KEY=your_key_here

Fields added:
  imdb_id, imdb_rating, imdb_votes, runtime_minutes,
  genres, plot_summary, omdb_poster_url, omdb_box_office,
  omdb_year, omdb_language, omdb_director (cross-check)
"""

from __future__ import annotations

import logging
import os
import time
from difflib import SequenceMatcher
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

OMDB_BASE = "https://www.omdbapi.com"


class OMDbExtractor:
    """
    Enriches AwardRecord objects with IMDb/OMDb metadata.

    Parameters
    ----------
    http_client : HTTPClient
        Configured HTTP client.
    api_key : str, optional
        OMDb API key. Falls back to NFA_OMDB_API_KEY env var.
    """

    def __init__(self, http_client, api_key: Optional[str] = None,
                 rss_extractor=None):
        self._http    = http_client
        self._api_key = api_key or os.environ.get("NFA_OMDB_API_KEY", "")
        self._cache: Dict[str, Optional[Dict]] = {}
        self._rss = rss_extractor  # optional RSSExtractor for fallback enrichment

        if not self._api_key:
            logger.warning("[OMDb] No API key set — OMDb enrichment disabled. "
                           "Set NFA_OMDB_API_KEY env var to enable.")

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def enrich_record(self, film_title: str,
                      film_year: Optional[int] = None) -> Optional[Dict]:
        """
        Fetch OMDb metadata for a film.

        Returns a dict with enrichment fields, or None on failure.
        """
        if not self.enabled:
            return None

        cache_key = f"{film_title}::{film_year}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        result = self._fetch_by_title(film_title, film_year)
        self._cache[cache_key] = result
        return result

    # Titles that are Wikipedia metadata noise, not real films
    _SKIP_TITLES = {
        "awarded for", "awarded by", "presented by", "announced on",
        "site", "official website", "producer", "director",
        "feature films", "non-feature films", "unknown",
    }

    def enrich_records_batch(self, records: List,
                             delay: float = 0.2) -> List:
        """
        Enrich a batch of AwardRecord objects in-place with OMDb data.
        Adds fields: imdb_id, imdb_rating, imdb_votes, runtime_minutes,
                     genres, omdb_box_office, omdb_poster_url
        """
        if not self.enabled:
            logger.info("[OMDb] Skipping enrichment — no API key")
            return records

        enriched = 0
        rss_enriched = 0
        for rec in records:
            try:
                title = rec.film_title or ""
                # Skip junk / placeholder titles
                if title.lower() in self._SKIP_TITLES:
                    logger.debug("[OMDb] Skipping junk title: %r", title)
                    continue
                if len(title) < 4:
                    logger.debug("[OMDb] Skipping too-short title: %r", title)
                    continue
                data = self.enrich_record(title, rec.film_year)
                if data:
                    self._apply_to_record(rec, data)
                    enriched += 1
                elif self._rss:
                    # ── RSS fallback: try to get at least a plot summary ──
                    rss_data = self._rss.fetch(title, rec.film_year)
                    if rss_data:
                        if rss_data.get("plot_summary") and not rec.plot_summary:
                            rec.plot_summary = rss_data["plot_summary"]
                        if rss_data.get("release_date") and not rec.release_date:
                            rec.release_date = rss_data["release_date"]
                        rss_enriched += 1
                        logger.debug("[RSS] Fallback enriched: %r", title)
                time.sleep(delay)
            except Exception as exc:
                logger.debug("[OMDb] Failed for %r: %s", rec.film_title, exc)

        logger.info("[OMDb] Enriched %d / %d records (RSS fallback: %d)",
                    enriched, len(records), rss_enriched)
        return records

    def _fetch_by_title(self, title: str,
                        year: Optional[int] = None) -> Optional[Dict]:
        """Search OMDb by title (and optionally year).

        When year is provided, we do NOT retry without year on failure —
        the year-less retry causes wrong matches for short/ambiguous Indian
        film titles that share names with unrelated foreign films.
        """
        import requests

        params: Dict = {
            "apikey": self._api_key,
            "t":      title,
            "type":   "movie",
        }
        if year:
            params["y"] = str(year)

        try:
            resp = requests.get(OMDB_BASE, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if data.get("Response") != "True":
                logger.debug("[OMDb] MISS: %s (%s): %s",
                             title, year, data.get("Error", "unknown"))
                return None

            # ── Confidence check: title similarity ────────────────────────
            returned_title = data.get("Title", "")
            ratio = SequenceMatcher(
                None, title.lower(), returned_title.lower()
            ).ratio()
            if ratio < 0.4:
                logger.debug(
                    "[OMDb] Low confidence match discarded: %r vs %r (ratio=%.2f)",
                    title, returned_title, ratio,
                )
                return None

            logger.debug("[OMDb] HIT: %s (%s) → imdb:%s rating:%s (similarity=%.2f)",
                         title, year, data.get("imdbID"), data.get("imdbRating"), ratio)
            return data

        except Exception as exc:
            logger.warning("[OMDb] Request error for %r: %s", title, exc)
            return None

    def fetch_by_imdb_id(self, imdb_id: str) -> Optional[Dict]:
        """Fetch full metadata by IMDb ID (more accurate than title search)."""
        if not self.enabled:
            return None
        import requests
        try:
            resp = requests.get(OMDB_BASE, params={
                "apikey": self._api_key,
                "i":      imdb_id,
                "plot":   "short",
            }, timeout=10)
            data = resp.json()
            return data if data.get("Response") == "True" else None
        except Exception as exc:
            logger.warning("[OMDb] fetch_by_imdb_id(%s) failed: %s", imdb_id, exc)
            return None

    @staticmethod
    def _apply_to_record(rec, data: Dict) -> None:
        """Write OMDb fields onto an AwardRecord (adds new attributes)."""
        def clean(v):
            return v if v and v != "N/A" else None

        rec.imdb_id       = clean(data.get("imdbID"))
        rec.imdb_rating   = clean(data.get("imdbRating"))
        rec.imdb_votes    = clean(data.get("imdbVotes"))
        rec.omdb_box_office = clean(data.get("BoxOffice"))
        rec.omdb_poster_url = clean(data.get("Poster"))
        rec.plot_summary    = clean(data.get("Plot"))

        # Runtime: "142 min" → 142
        rt = clean(data.get("Runtime"))
        if rt:
            m = __import__("re").match(r"(\d+)", rt)
            rec.runtime_minutes = int(m.group(1)) if m else None
        else:
            rec.runtime_minutes = None

        # Genres: "Action, Drama, Thriller" → "Action|Drama" (cap at 3)
        genre_str = clean(data.get("Genre"))
        if genre_str:
            parts = [g.strip() for g in genre_str.split(",")][:3]
            rec.genres = "|".join(parts)
        else:
            rec.genres = None

        # Cross-check director (don't overwrite if already set)
        omdb_dir = clean(data.get("Director"))
        if omdb_dir and not rec.director:
            rec.director = omdb_dir

        # Cross-check language
        omdb_lang = clean(data.get("Language"))
        if omdb_lang and not rec.film_language:
            # Take first language listed
            rec.film_language = omdb_lang.split(",")[0].strip()

        # Release date — set both omdb_released and release_date
        released = clean(data.get("Released"))
        rec.omdb_released = released
        if released and not rec.release_date:
            rec.release_date = released

        # Box office worldwide — set both omdb_box_office and box_office_worldwide
        box = clean(data.get("BoxOffice"))
        rec.omdb_box_office = box
        if box and not rec.box_office_worldwide:
            rec.box_office_worldwide = box

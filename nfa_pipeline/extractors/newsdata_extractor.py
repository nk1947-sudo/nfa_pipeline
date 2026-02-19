"""
nfa_pipeline/extractors/newsdata_extractor.py
----------------------------------------------
Last-resort enrichment using the Newsdata.io API.

Runs as Step 5 in MultiSourceExtractor — after Wikipedia, Serper, OMDb,
and IMDb have each had their turn.  For every record that still has NULL
values in any enrichable field, this extractor searches Newsdata.io for
matching film articles and fills whatever it finds.

If Newsdata returns no articles for a film, every still-null field is set
to the sentinel string ``"Unknown"`` so that downstream consumers never
receive a bare NULL.

Fields this step can fill
--------------------------
  plot_summary        – article description / teaser
  director            – extracted via regex from article body
  film_language       – guessed from article keywords or source country
  release_date        – article publish date (ISO 8601)
  genres              – guessed from article keywords
  runtime_minutes     – extracted via regex ("N min" / "N minutes")
  box_office_worldwide– extracted via regex ("₹N crore", "$N million")

Target fields that REMAIN null on no-hit
-----------------------------------------
  imdb_id, imdb_rating, imdb_votes, omdb_box_office, omdb_poster_url,
  omdb_released, crew_size, is_sequel, is_remake
  (these require structured DB lookups, not article text)

API
---
  https://newsdata.io/api/1/news?apikey=<key>&q=<title>&country=in&language=en
  Free tier: 200 credits/day.  Each search uses 1 credit.

Usage
-----
    from nfa_pipeline.extractors.newsdata_extractor import NewsdataExtractor
    nd = NewsdataExtractor(api_key="...", country="in", language="en")
    nd.enrich_records(records)   # in-place fill + Unknown fallback
"""

from __future__ import annotations

import logging
import re
import time
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Any

import requests

from ..models import AwardRecord

logger = logging.getLogger(__name__)

# ── API constants ─────────────────────────────────────────────────────────────
_API_URL = "https://newsdata.io/api/1/news"
_REQUEST_TIMEOUT = 10       # seconds
_MIN_SIMILARITY   = 0.40    # title match threshold (0.0 – 1.0)
_MAX_RESULTS      = 10      # results to inspect per query

# ── Fields we can fill from news text ─────────────────────────────────────────
_FILLABLE_FIELDS: List[str] = [
    "plot_summary",
    "director",
    "film_language",
    "release_date",
    "genres",
    "runtime_minutes",
    "box_office_worldwide",
]

# ── Fields we set to "Unknown" if still null & no news hit ────────────────────
# (does NOT include fields only fillable via structured DBs: imdb_id, etc.)
_UNKNOWN_FALLBACK_FIELDS: List[str] = [
    "plot_summary",
    "director",
    "film_language",
    "genres",
]

# ── Regex patterns for field extraction ───────────────────────────────────────
_RE_RUNTIME   = re.compile(r"\b(\d{2,3})\s*(?:min(?:utes?)?|मिनट)", re.I)
_RE_BOX_INDIA = re.compile(r"(?:₹|Rs\.?|INR)\s*([\d,.]+)\s*crore", re.I)
_RE_BOX_USD   = re.compile(r"\$([\d,.]+)\s*(?:million|billion)", re.I)
_RE_DIRECTOR  = re.compile(
    r"(?:directed?\s+by|director[:\s]+)\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
    re.I,
)

# Rough keyword → genre mapping
_GENRE_KEYWORDS: Dict[str, str] = {
    "comedy":       "Comedy",
    "thriller":     "Thriller",
    "horror":       "Horror",
    "romance":      "Romance",
    "action":       "Action",
    "drama":        "Drama",
    "documentary":  "Documentary",
    "animation":    "Animation",
    "biographical": "Biography",
    "biopic":       "Biography",
    "history":      "History",
    "sport":        "Sport",
    "mystery":      "Mystery",
    "crime":        "Crime",
}

# Language keywords to canonical name
_LANG_KEYWORDS: Dict[str, str] = {
    "hindi":      "Hindi",
    "tamil":      "Tamil",
    "telugu":     "Telugu",
    "malayalam":  "Malayalam",
    "kannada":    "Kannada",
    "bengali":    "Bengali",
    "marathi":    "Marathi",
    "odia":       "Odia",
    "oriya":      "Odia",
    "punjabi":    "Punjabi",
    "assamese":   "Assamese",
    "gujarati":   "Gujarati",
    "urdu":       "Urdu",
    "english":    "English",
}


class NewsdataExtractor:
    """
    Last-resort enrichment step using the Newsdata.io News API.

    Parameters
    ----------
    api_key : str
        Newsdata.io API key.
    country : str
        ISO alpha-2 country code to restrict results (e.g. ``"in"`` for India).
    language : str
        Language code for article language (e.g. ``"en"``).
    delay : float
        Seconds to sleep between API calls (polite usage, free tier is
        200 credits/day).
    timeout : float
        HTTP request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str,
        country: str = "in",
        language: str = "en",
        delay: float = 0.5,
        timeout: float = _REQUEST_TIMEOUT,
    ):
        self._key      = api_key.strip()
        self._country  = country
        self._language = language
        self._delay    = delay
        self._timeout  = timeout
        self._session  = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
        })

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    # ── Public API ─────────────────────────────────────────────────────────────

    def enrich_records(self, records: List[AwardRecord]) -> List[AwardRecord]:
        """
        Fill any remaining null enrichable fields for a batch of records.

        For every record that still has at least one null in ``_FILLABLE_FIELDS``:
          1. Query Newsdata.io for the film title.
          2. Match the best article by title similarity.
          3. Extract fields from the article text.
          4. For fields that remain null after a successful search, do nothing.
          5. For fields that remain null because NO article was found, set to
             ``"Unknown"`` (only for ``_UNKNOWN_FALLBACK_FIELDS``).

        Parameters
        ----------
        records : list[AwardRecord]
            Records to enrich in-place.

        Returns
        -------
        list[AwardRecord]
            The same list (mutated in-place).
        """
        if not self.enabled:
            logger.warning(
                "[Newsdata] API key not set — skipping Step 5. "
                "Set NFA_NEWSDATA_API_KEY or enrichment.newsdata_api_key in settings.yaml."
            )
            return records

        needs_work = [
            r for r in records
            if any(not getattr(r, f, None) for f in _FILLABLE_FIELDS)
        ]

        if not needs_work:
            logger.info("[Newsdata] All records already fully enriched — Step 5 skipped")
            return records

        logger.info(
            "[Step 5] Newsdata last-resort: %d/%d records need further enrichment",
            len(needs_work), len(records),
        )

        filled  = 0
        no_hit  = 0
        skipped = 0

        for rec in needs_work:
            try:
                articles = self._search(rec.film_title, rec.film_year)
                best     = self._best_match(rec.film_title, articles)

                if best:
                    changed = self._apply(rec, best)
                    if changed:
                        filled += 1
                        logger.debug(
                            "[Newsdata] ✓ %r — filled: %s",
                            rec.film_title, ", ".join(changed),
                        )
                    else:
                        skipped += 1
                else:
                    # No matching article — apply Unknown fallback
                    self._apply_unknown_fallback(rec)
                    no_hit += 1
                    logger.debug("[Newsdata] ✗ No article found for %r", rec.film_title)

            except Exception as exc:
                logger.warning(
                    "[Newsdata] Error enriching %r: %s", rec.film_title, exc
                )
                self._apply_unknown_fallback(rec)
                no_hit += 1

            time.sleep(self._delay)

        logger.info(
            "[Newsdata] Step 5 complete — filled=%d | no_hit(→Unknown)=%d | already_ok=%d",
            filled, no_hit, skipped,
        )
        return records

    # ── Private: API call ──────────────────────────────────────────────────────

    def _search(self, film_title: str, film_year: Optional[int]) -> List[Dict]:
        """
        Search Newsdata.io for articles matching *film_title*.
        Returns a list of article dicts (may be empty).
        """
        query = f'"{film_title}" National Film Award'
        if film_year:
            query += f" {film_year}"

        params: Dict[str, Any] = {
            "apikey":   self._key,
            "q":        query,
            "country":  self._country,
            "language": self._language,
            "size":     _MAX_RESULTS,
            "category": "entertainment",
        }

        try:
            resp = self._session.get(
                _API_URL, params=params, timeout=self._timeout
            )
            if resp.status_code == 401:
                logger.error(
                    "[Newsdata] 401 Unauthorized — check your API key. "
                    "Current key: %s…", self._key[:8]
                )
                return []
            if resp.status_code == 429:
                logger.warning("[Newsdata] 429 Rate limited — daily quota exhausted")
                return []
            resp.raise_for_status()

            data = resp.json()
            results = data.get("results") or []
            logger.debug(
                "[Newsdata] Query %r → %d articles", film_title, len(results)
            )
            return results

        except requests.RequestException as exc:
            logger.warning("[Newsdata] Request failed for %r: %s", film_title, exc)
            return []

    # ── Private: matching ──────────────────────────────────────────────────────

    def _best_match(
        self, film_title: str, articles: List[Dict]
    ) -> Optional[Dict]:
        """Return the article whose title best matches *film_title*, or None."""
        if not articles:
            return None

        title_lower = film_title.lower()
        best_score  = 0.0
        best        = None

        for art in articles:
            art_title = (art.get("title") or "").lower()
            score = SequenceMatcher(None, title_lower, art_title).ratio()
            # Bonus: exact substring match
            if title_lower in art_title or art_title in title_lower:
                score += 0.2
            if score > best_score:
                best_score = score
                best       = art

        if best and best_score >= _MIN_SIMILARITY:
            return best
        return None

    # ── Private: field extraction ──────────────────────────────────────────────

    def _apply(self, rec: AwardRecord, article: Dict) -> List[str]:
        """
        Extract fields from *article* and fill any null fields on *rec*.

        Returns the list of field names that were actually updated.
        """
        filled: List[str] = []
        body = " ".join(filter(None, [
            article.get("title", ""),
            article.get("description", ""),
            article.get("content", ""),
        ]))

        # plot_summary ← article description
        if not rec.plot_summary:
            summary = (article.get("description") or "").strip()
            if summary:
                rec.plot_summary = summary[:500]
                filled.append("plot_summary")

        # director ← regex in body
        if not rec.director:
            m = _RE_DIRECTOR.search(body)
            if m:
                rec.director = m.group(1).title()
                filled.append("director")

        # film_language ← language keyword in body
        if not rec.film_language:
            body_lower = body.lower()
            for kw, lang in _LANG_KEYWORDS.items():
                if kw in body_lower:
                    rec.film_language = lang
                    filled.append("film_language")
                    break

        # release_date ← article publish date
        if not rec.release_date:
            pub = (article.get("pubDate") or article.get("publishedAt") or "").strip()
            if pub:
                rec.release_date = pub[:10]  # keep YYYY-MM-DD
                filled.append("release_date")

        # genres ← keyword extraction
        if not rec.genres:
            body_lower = body.lower()
            found = [g for kw, g in _GENRE_KEYWORDS.items() if kw in body_lower]
            if found:
                rec.genres = "|".join(dict.fromkeys(found))[:3]  # up to 3 genres
                filled.append("genres")

        # runtime_minutes ← regex
        if not rec.runtime_minutes:
            m = _RE_RUNTIME.search(body)
            if m:
                try:
                    rec.runtime_minutes = int(m.group(1))
                    filled.append("runtime_minutes")
                except ValueError:
                    pass

        # box_office_worldwide ← regex
        if not rec.box_office_worldwide:
            m = _RE_BOX_INDIA.search(body) or _RE_BOX_USD.search(body)
            if m:
                raw = m.group(0).strip()
                rec.box_office_worldwide = raw
                filled.append("box_office_worldwide")

        return filled

    # ── Private: Unknown fallback ──────────────────────────────────────────────

    @staticmethod
    def _apply_unknown_fallback(rec: AwardRecord) -> None:
        """
        For every field in ``_UNKNOWN_FALLBACK_FIELDS`` that is still null,
        set it to ``"Unknown"`` so downstream consumers never see a bare NULL.
        """
        for fname in _UNKNOWN_FALLBACK_FIELDS:
            if not getattr(rec, fname, None):
                setattr(rec, fname, "Unknown")

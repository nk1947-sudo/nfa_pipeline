"""
nfa_pipeline/extractors/imdb_extractor.py
------------------------------------------
Scrapes IMDb for film metadata to enrich NFA award records.

Strategy:
  1. Search IMDb via title search page
  2. Parse the search results to find best match
  3. Fetch the film's detail page for full metadata
  4. Extract: IMDb ID, rating, votes, runtime, genres,
              release date, box office (if listed)
  5. Detect is_sequel / is_remake from keywords / storyline
  6. Fetch full credits page to count crew_size

Note: IMDb scraping is used as a supplementary source.
      OMDb API (wraps IMDb data) is preferred when API key is available.
      IMDb scraping is the fallback for records OMDb misses.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Dict, List, Optional
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

IMDB_SEARCH_URL    = "https://www.imdb.com/find/?q={query}&s=tt&ttype=ft&ref_=fn_ft"
IMDB_TITLE_URL     = "https://www.imdb.com/title/{imdb_id}/"
IMDB_KEYWORDS_URL  = "https://www.imdb.com/title/{imdb_id}/keywords"
IMDB_CREDITS_URL   = "https://www.imdb.com/title/{imdb_id}/fullcredits"
IMDB_BASE          = "https://www.imdb.com"

# Keywords that indicate a sequel
_SEQUEL_KEYWORDS = {
    "sequel", "part-2", "part-3", "part-4", "part-ii", "part-iii",
    "second-part", "continuation", "franchise", "series",
}
# Keywords that indicate a remake
_REMAKE_KEYWORDS = {
    "remake", "based-on-previous-film", "reimagining", "reboot",
    "based-on-earlier-film",
}


class IMDbExtractor:
    """
    Scrapes IMDb to enrich AwardRecord objects with film metadata.

    Used as a fallback when OMDb API misses a film (typically
    for older or regional Indian films not in OMDb's database).
    """

    def __init__(self, http_client):
        self._http  = http_client
        self._cache: Dict[str, Optional[Dict]] = {}

    def search_film(self, title: str,
                    year: Optional[int] = None) -> Optional[Dict]:
        """
        Search IMDb for a film and return its metadata dict.
        Returns None if not found.
        """
        cache_key = f"{title}::{year}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        imdb_id = self._find_imdb_id(title, year)
        if not imdb_id:
            self._cache[cache_key] = None
            return None

        data = self._fetch_film_page(imdb_id)
        self._cache[cache_key] = data
        return data

    def enrich_records_batch(self, records: List,
                             delay: float = 0.5) -> List:
        """
        Enrich records that are missing IMDb data (imdb_id not set).
        Only processes records without imdb_id to avoid redundant lookups.
        """
        missing = [r for r in records if not getattr(r, "imdb_id", None)]
        logger.info("[IMDb] Enriching %d records missing IMDb data", len(missing))

        enriched = 0
        for rec in missing:
            try:
                data = self.search_film(rec.film_title, rec.film_year)
                if data:
                    self._apply_to_record(rec, data)
                    enriched += 1
                time.sleep(delay)
            except Exception as exc:
                logger.debug("[IMDb] Failed for %r: %s", rec.film_title, exc)

        logger.info("[IMDb] Enriched %d / %d missing records",
                    enriched, len(missing))
        return records

    # ── Internal methods ───────────────────────────────────────────────────────

    def _find_imdb_id(self, title: str,
                      year: Optional[int] = None) -> Optional[str]:
        """Search IMDb and return the best-matching IMDb ID."""
        query = f"{title} {year}" if year else title
        url   = IMDB_SEARCH_URL.format(query=quote_plus(query))
        html  = self._http.get(url)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")

        # IMDb search results: look for /title/ttXXXXXXX/ links
        for a in soup.find_all("a", href=re.compile(r"/title/tt\d+")):
            href = a.get("href", "")
            m    = re.search(r"/title/(tt\d+)/", href)
            if not m:
                continue
            imdb_id    = m.group(1)
            result_text = a.get_text(strip=True).lower()
            title_lower = title.lower()

            # Accept if title is contained in result text
            if title_lower[:10] in result_text:
                logger.debug("[IMDb] Found %s → %s", title, imdb_id)
                return imdb_id

        # Looser match: first result
        first = soup.find("a", href=re.compile(r"/title/tt\d+"))
        if first:
            m = re.search(r"/title/(tt\d+)/", first.get("href", ""))
            if m:
                return m.group(1)

        return None

    def _fetch_film_page(self, imdb_id: str) -> Optional[Dict]:
        """Fetch and parse an IMDb film detail page."""
        url  = IMDB_TITLE_URL.format(imdb_id=imdb_id)
        html = self._http.get(url)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")
        data: Dict = {"imdb_id": imdb_id, "imdb_url": url}

        # Title
        title_tag = (soup.find("h1", {"data-testid": "hero__pageTitle"}) or
                     soup.find("h1", class_=re.compile(r"TitleHeader")))
        if title_tag:
            data["imdb_title"] = title_tag.get_text(strip=True)

        # Rating
        rating_tag = soup.find("div", {"data-testid": "hero-rating-bar__aggregate-rating__score"})
        if rating_tag:
            spans = rating_tag.find_all("span")
            if spans:
                try:
                    data["imdb_rating"] = float(spans[0].get_text(strip=True))
                except ValueError:
                    pass

        # Vote count
        votes_tag = soup.find("div", {"data-testid": "hero-rating-bar__aggregate-rating__score"})
        if votes_tag:
            count_tag = votes_tag.find_next("div", class_=re.compile(r"voteCount"))
            if count_tag:
                data["imdb_votes"] = count_tag.get_text(strip=True)

        # Runtime — look for metadata chips
        for li in soup.find_all("li", {"data-testid": "title-techspec_runtime"}):
            data["runtime_minutes"] = self._parse_runtime(li.get_text(strip=True))
            break

        # Genres — cap at 3
        genre_tags = soup.find_all("span", {"class": re.compile(r"ipc-chip__text")})
        genres = []
        known = {"Action", "Drama", "Comedy", "Thriller", "Romance", "Crime",
                 "Biography", "History", "Musical", "Mystery", "Family",
                 "Fantasy", "Horror", "Sci-Fi", "Adventure", "Animation", "War"}
        for g in genre_tags:
            t = g.get_text(strip=True)
            if t in known:
                genres.append(t)
        if genres:
            data["genres"] = "|".join(genres[:3])

        # Release date
        rel_tag = soup.find("a", href=re.compile(r"/releaseinfo"))
        if rel_tag:
            data["release_date"] = rel_tag.get_text(strip=True)

        # Box office (from technical specs section)
        for row in soup.find_all("li", {"data-testid": "title-boxoffice-cumulativeworldwidegross"}):
            span = row.find("span", class_=re.compile(r"ipc-metadata"))
            if span:
                data["box_office_worldwide"] = span.get_text(strip=True)
            break

        # ── is_sequel / is_remake detection ───────────────────────────────────
        is_sequel, is_remake = self._detect_sequel_remake(imdb_id, soup)
        data["is_sequel"] = is_sequel
        data["is_remake"] = is_remake

        # ── crew_size from full credits page ──────────────────────────────────
        data["crew_size"] = self._fetch_crew_size(imdb_id)

        logger.debug("[IMDb] Parsed %s: rating=%s, votes=%s, genres=%s, sequel=%s, remake=%s, crew=%s",
                     imdb_id,
                     data.get("imdb_rating"),
                     data.get("imdb_votes"),
                     data.get("genres"),
                     data.get("is_sequel"),
                     data.get("is_remake"),
                     data.get("crew_size"))
        return data

    def _detect_sequel_remake(self, imdb_id: str,
                               main_soup: BeautifulSoup) -> tuple[Optional[bool], Optional[bool]]:
        """
        Detect whether a film is a sequel or remake by checking:
        1. IMDb keywords page
        2. Storyline / plot keywords on the main page
        """
        is_sequel: Optional[bool] = None
        is_remake: Optional[bool] = None

        try:
            kw_url  = IMDB_KEYWORDS_URL.format(imdb_id=imdb_id)
            kw_html = self._http.get(kw_url)
            if kw_html:
                kw_soup = BeautifulSoup(kw_html, "html.parser")
                # Keywords appear as links like /search/keyword?keywords=sequel
                kw_links = kw_soup.find_all("a", href=re.compile(r"/search/keyword"))
                kw_set = {a.get_text(strip=True).lower().replace(" ", "-") for a in kw_links}

                if kw_set & _SEQUEL_KEYWORDS:
                    is_sequel = True
                elif is_sequel is None:
                    is_sequel = False

                if kw_set & _REMAKE_KEYWORDS:
                    is_remake = True
                elif is_remake is None:
                    is_remake = False
        except Exception as exc:
            logger.debug("[IMDb] keyword detection failed for %s: %s", imdb_id, exc)

        # Fallback: scan storyline text on main page for plain-language cues
        if is_sequel is None or is_remake is None:
            storyline = main_soup.get_text(" ", strip=True).lower()
            if is_sequel is None:
                is_sequel = any(kw.replace("-", " ") in storyline
                                for kw in _SEQUEL_KEYWORDS)
            if is_remake is None:
                is_remake = any(kw.replace("-", " ") in storyline
                                for kw in _REMAKE_KEYWORDS)

        return is_sequel, is_remake

    def _fetch_crew_size(self, imdb_id: str) -> Optional[int]:
        """
        Fetch the IMDb full credits page and count all credited crew members.
        Returns total count or None on failure.
        """
        try:
            url  = IMDB_CREDITS_URL.format(imdb_id=imdb_id)
            html = self._http.get(url)
            if not html:
                return None
            soup = BeautifulSoup(html, "html.parser")

            # Each crew member appears as a <tr> inside a credits table
            # Cast + crew tables all use class "cast_list" or plain <table>
            total = 0
            for table in soup.find_all("table"):
                rows = table.find_all("tr")
                # Filter out header rows (no <td> with actual name links)
                for row in rows:
                    tds = row.find_all("td")
                    if len(tds) >= 2:
                        total += 1

            return total if total > 0 else None
        except Exception as exc:
            logger.debug("[IMDb] crew_size fetch failed for %s: %s", imdb_id, exc)
            return None

    @staticmethod
    def _parse_runtime(text: str) -> Optional[int]:
        """Parse '2 hours 22 minutes' or '142 min' to integer minutes."""
        h = re.search(r"(\d+)\s*h", text)
        m = re.search(r"(\d+)\s*m", text)
        if h or m:
            return int(h.group(1) if h else 0) * 60 + int(m.group(1) if m else 0)
        plain = re.search(r"(\d+)", text)
        return int(plain.group(1)) if plain else None

    @staticmethod
    def _apply_to_record(rec, data: Dict) -> None:
        def setif(attr, val):
            if val is not None and not getattr(rec, attr, None):
                setattr(rec, attr, val)

        setif("imdb_id",             data.get("imdb_id"))
        setif("imdb_rating",         data.get("imdb_rating"))
        setif("imdb_votes",          data.get("imdb_votes"))
        setif("runtime_minutes",     data.get("runtime_minutes"))
        setif("genres",              data.get("genres"))
        setif("release_date",        data.get("release_date"))
        setif("box_office_worldwide",data.get("box_office_worldwide"))
        setif("crew_size",           data.get("crew_size"))

        # is_sequel / is_remake: always set (even False) if we got a value
        if data.get("is_sequel") is not None and rec.is_sequel is None:
            rec.is_sequel = data["is_sequel"]
        if data.get("is_remake") is not None and rec.is_remake is None:
            rec.is_remake = data["is_remake"]

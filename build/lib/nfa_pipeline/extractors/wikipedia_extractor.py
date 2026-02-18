"""
nfa_pipeline/extractors/wikipedia_extractor.py
------------------------------------------------
Primary source: Wikipedia ceremony pages for NFA awards.

URL pattern (verified):
  film_year=2000 → https://en.wikipedia.org/wiki/48th_National_Film_Awards
  film_year=2023 → https://en.wikipedia.org/wiki/71st_National_Film_Awards

Formula:  ceremony_number = film_year - 1952
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, Tag

from ..models import AwardRecord, YearPage

logger = logging.getLogger(__name__)

WIKIPEDIA_BASE = "https://en.wikipedia.org/wiki"


def ceremony_number(film_year: int) -> int:
    return film_year - 1952


def ordinal_suffix(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def ceremony_url(film_year: int) -> str:
    n = ceremony_number(film_year)
    s = ordinal_suffix(n)
    return f"{WIKIPEDIA_BASE}/{n}{s}_National_Film_Awards"


_GOLDEN = re.compile(r"golden\s+lotus|swarna\s+kamal|official\s+name:\s*swarna", re.I)
_SILVER = re.compile(r"silver\s+lotus|rajat\s+kamal|official\s+name:\s*rajat", re.I)
_CERT   = re.compile(r"certificate\s+of\s+merit|special\s+jury|special\s+mention", re.I)
_FEATURE    = re.compile(r"feature\s+film", re.I)
_NONFEATURE = re.compile(r"non.?feature", re.I)
_DIRECTOR_RE = re.compile(r"[Dd]irector\s*:\s*(.+?)(?:\s{2,}|\n|$)")


def infer_award_type(text: str) -> Optional[str]:
    if _GOLDEN.search(text): return "Golden Lotus"
    if _SILVER.search(text): return "Silver Lotus"
    if _CERT.search(text):   return "Certificate of Merit"
    return None


class WikipediaExtractor:
    """Extracts NFA award data from Wikipedia ceremony pages."""

    def __init__(self, http_client):
        self._http = http_client

    def extract_year(self, film_year: int) -> YearPage:
        url = ceremony_url(film_year)
        n   = ceremony_number(film_year)
        logger.info("[Wikipedia] film_year=%d → %d%s NFA → %s",
                    film_year, n, ordinal_suffix(n), url)

        html = self._http.get(url)
        if not html:
            return YearPage(year=film_year, url=url, html="",
                            http_status=0, parse_error="HTTP fetch returned None",
                            source="wikipedia")

        records, err = self._parse(html, film_year, url)
        page = YearPage(year=film_year, url=url, html=html,
                        scraped_at=datetime.utcnow(),
                        records=records, parse_error=err,
                        source="wikipedia")
        html_hash = hashlib.md5(html.encode()).hexdigest()
        for r in records:
            r.source_url    = url
            r.raw_html_hash = html_hash
            r.scraped_at    = page.scraped_at
            r.data_source   = "wikipedia"
        logger.info("[Wikipedia] film_year=%d: %d records%s",
                    film_year, len(records), f" [WARN:{err}]" if err else "")
        return page

    def _parse(self, html: str, film_year: int, url: str
               ) -> Tuple[List[AwardRecord], Optional[str]]:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(["sup", "span"],
                                  class_=["mw-editsection", "reference"]):
            tag.decompose()

        records = self._parse_structured(soup, film_year)
        if records:
            return records, None

        records = self._parse_any_table(soup, film_year)
        if records:
            return records, "generic-table fallback"

        return [], "No award data found"

    # ── Structured Wikipedia tables ───────────────────────────────────────────

    def _parse_structured(self, soup: BeautifulSoup,
                          film_year: int) -> List[AwardRecord]:
        records = []
        section    = "Feature Films"
        award_type = None

        for elem in soup.find_all(["h2", "h3", "h4", "table"]):
            text = elem.get_text(separator=" ", strip=True)

            if elem.name in ("h2", "h3", "h4"):
                if _FEATURE.search(text) and not _NONFEATURE.search(text):
                    section = "Feature Films"
                elif _NONFEATURE.search(text):
                    section = "Non-Feature Films"
                inferred = infer_award_type(text)
                if inferred:
                    award_type = inferred
            else:
                records.extend(
                    self._parse_table(elem, film_year, section, award_type))
        return records

    def _parse_table(self, table: Tag, film_year: int,
                     section: str, default_type: Optional[str]) -> List[AwardRecord]:
        rows = table.find_all("tr")
        if len(rows) < 2:
            return []
        headers = [c.get_text(" ", strip=True).lower()
                   for c in rows[0].find_all(["th", "td"])]
        if not any(k in " ".join(headers)
                   for k in ("award", "film", "awardee", "category")):
            return []
        col = self._map_cols(headers)
        if not col:
            return []

        records = []
        cur_type = default_type
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            if len(cells) == 1:
                t = infer_award_type(cells[0].get_text(strip=True))
                if t:
                    cur_type = t
                continue
            r = self._row_to_record(cells, col, film_year, section, cur_type)
            if r:
                records.append(r)
        return records

    def _map_cols(self, headers: List[str]) -> Dict[str, int]:
        m: Dict[str, int] = {}
        for i, h in enumerate(headers):
            if any(k in h for k in ("name of award", "award category", "category")):
                m.setdefault("category", i)
            elif "award" in h and "awardee" not in h and "awarded" not in h:
                m.setdefault("category", i)
            if any(k in h for k in ("name of film", "film title", "film", "movie")):
                m.setdefault("film_title", i)
            if "language" in h:
                m.setdefault("film_language", i)
            if any(k in h for k in ("awardee", "winner", "recipient", "awarded to")):
                m.setdefault("awardee", i)
            elif h.strip() == "name":
                m.setdefault("awardee", i)
            if "cash" in h or "prize" in h:
                m.setdefault("cash_prize", i)
            if h.strip() == "director":
                m.setdefault("director", i)
        return m

    def _row_to_record(self, cells, col, film_year, section,
                       default_type) -> Optional[AwardRecord]:
        def ct(key):
            idx = col.get(key)
            if idx is not None and idx < len(cells):
                return cells[idx].get_text(" ", strip=True) or None
            return None

        category   = ct("category")
        film_title = ct("film_title")
        if not category and not film_title:
            return None

        raw_awardee = ct("awardee") or ""
        director    = ct("director")
        if director is None:
            dm = _DIRECTOR_RE.search(raw_awardee)
            if dm:
                director    = dm.group(1).strip()
                raw_awardee = _DIRECTOR_RE.sub("", raw_awardee).strip() or None

        awardee = re.sub(r"^[Dd]irector\s*:\s*", "", raw_awardee or "").strip() or None
        award_type = infer_award_type(category or "") or default_type

        return AwardRecord(
            ceremony_year   = film_year + 1,
            ceremony_number = ceremony_number(film_year),
            film_year       = film_year,
            category        = (category or "Unknown").strip(),
            film_title      = (film_title or "Unknown").strip(),
            film_language   = ct("film_language"),
            director        = director,
            awardee         = awardee,
            award_type      = award_type,
            cash_prize      = ct("cash_prize"),
            data_source     = "wikipedia",
        )

    def _parse_any_table(self, soup, film_year):
        records = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            headers = [c.get_text(strip=True).lower()
                       for c in rows[0].find_all(["th", "td"])]
            col = self._map_cols(headers)
            if not col:
                continue
            for row in rows[1:]:
                cells = row.find_all("td")
                if not cells:
                    continue
                r = self._row_to_record(cells, col, film_year,
                                        "Feature Films", None)
                if r:
                    records.append(r)
        return records

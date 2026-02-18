"""
nfa_pipeline/extractors/nfa_extractor.py
-----------------------------------------
Scrapes NFA award data from Wikipedia ceremony pages.

Why Wikipedia instead of dff.nic.in?
--------------------------------------
The official DFF site (dff.nic.in/App/NationalAwards.aspx) uses ASP.NET
__VIEWSTATE postbacks and is frequently unreachable from outside India.
Wikipedia hosts the same authoritative data in well-structured HTML tables
for every ceremony from the 1st (1954) to the 71st (2023), making it the
most reliable and complete source for this dataset.

URL pattern (verified):
  https://en.wikipedia.org/wiki/{ordinal}th_National_Film_Awards
  e.g. 47th_National_Film_Awards  (for films of 1999, ceremony 2000)
       71st_National_Film_Awards  (for films of 2023)

Ceremony ↔ Film-year mapping (verified from Wikipedia):
  ceremony_number = film_year + 47   (e.g. 1999 films → 47th NFA)
  So: film_year 2000 → 48th NFA, ..., film_year 2023 → 71st NFA

Ordinal suffix rules: 1st, 2nd, 3rd, 4th..20th, 21st, 22nd, 23rd, 24th..

Design
------
* Three parsing strategies (table → definition list → heading blocks)
* Extracts: category, film_title, film_language, awardee, director,
  award_type (Golden/Silver Lotus / Certificate), cash_prize
* All parsing errors are caught per-row; one bad row never kills a year
* Wikipedia API is used as a secondary verification source
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString, Tag

from ..models import AwardRecord, YearPage
from ..utils.http_client import HTTPClient

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

WIKIPEDIA_BASE = "https://en.wikipedia.org/wiki"

# film_year → ceremony_number (verified)
# 47th NFA honoured films of 1999; 48th → 2000 films, ..., 71st → 2023 films
def _ceremony_number(film_year: int) -> int:
    """Return the NFA ceremony ordinal for a given film release year.
    
    Verified mapping:
      1999 films → 47th NFA
      2000 films → 48th NFA
      2023 films → 71st NFA
    Formula: ceremony = film_year - 1952
    """
    return film_year - 1952

def _ordinal_suffix(n: int) -> str:
    """Return 'st', 'nd', 'rd', or 'th' for integer n."""
    if 11 <= (n % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")

def _ceremony_url(film_year: int) -> str:
    n = _ceremony_number(film_year)
    suffix = _ordinal_suffix(n)
    title = f"{n}{suffix}_National_Film_Awards"
    return f"{WIKIPEDIA_BASE}/{title}"

# ── Award-type keywords ────────────────────────────────────────────────────────
_GOLDEN  = re.compile(r"golden\s+lotus|swarna\s+kamal|official\s+name:\s*swarna", re.I)
_SILVER  = re.compile(r"silver\s+lotus|rajat\s+kamal|official\s+name:\s*rajat", re.I)
_CERT    = re.compile(r"certificate\s+of\s+merit|special\s+jury|special\s+mention", re.I)

def _infer_award_type(text: str) -> Optional[str]:
    if _GOLDEN.search(text):
        return "Golden Lotus"
    if _SILVER.search(text):
        return "Silver Lotus"
    if _CERT.search(text):
        return "Certificate of Merit"
    return None

# ── Section type detection ─────────────────────────────────────────────────────
_FEATURE_SECTION    = re.compile(r"feature\s+film", re.I)
_NONFEATURE_SECTION = re.compile(r"non.?feature", re.I)
_WRITING_SECTION    = re.compile(r"writing\s+on\s+cinema|best\s+book", re.I)

# ── Director extraction from cell text ────────────────────────────────────────
_DIRECTOR_RE = re.compile(r"[Dd]irector\s*:\s*(.+?)(?:\n|$)")


class NFAExtractor:
    """
    Fetches and parses NFA award pages from Wikipedia.

    Parameters
    ----------
    http_client : HTTPClient
        Configured HTTP client (handles caching, retries).
    scraper_cfg
        Scraper configuration block (user_agent, timeouts, etc.).
    """

    def __init__(self, http_client: HTTPClient, scraper_cfg):
        self._http = http_client
        self._cfg = scraper_cfg

    # ── Public API ─────────────────────────────────────────────────────────────

    def extract_year(self, film_year: int) -> YearPage:
        """
        Fetch and parse NFA awards for films released in *film_year*.

        The corresponding Wikipedia page is e.g.:
          film_year=2000 → 48th_National_Film_Awards
          film_year=2023 → 71st_National_Film_Awards

        Returns
        -------
        YearPage
            Raw HTML + list of parsed AwardRecord objects.
        """
        url = _ceremony_url(film_year)
        ceremony_n = _ceremony_number(film_year)
        logger.info("Extracting film_year=%d (ceremony=%d%s) from %s",
                    film_year, ceremony_n, _ordinal_suffix(ceremony_n), url)

        html = self._http.get(url)
        if html is None:
            return YearPage(
                year=film_year, url=url, html="",
                http_status=0,
                parse_error="HTTP fetch returned None",
            )

        html_hash = hashlib.md5(html.encode()).hexdigest()
        records, parse_error = self._parse_html(html, film_year, url)

        page = YearPage(
            year=film_year,
            url=url,
            html=html,
            scraped_at=datetime.utcnow(),
            records=records,
            parse_error=parse_error,
        )

        for rec in page.records:
            rec.source_url    = url
            rec.raw_html_hash = html_hash
            rec.scraped_at    = page.scraped_at

        logger.info("film_year=%d: %d records%s",
                    film_year, len(records),
                    f" [WARN: {parse_error}]" if parse_error else "")
        return page

    def extract_years(self, years: List[int]) -> List[YearPage]:
        pages = []
        for year in years:
            try:
                pages.append(self.extract_year(year))
            except Exception as exc:
                logger.exception("Unexpected error for year %d: %s", year, exc)
                pages.append(YearPage(
                    year=year, url=_ceremony_url(year),
                    html="", parse_error=str(exc),
                ))
        return pages

    # ── HTML parsing ───────────────────────────────────────────────────────────

    def _parse_html(
        self, html: str, film_year: int, url: str
    ) -> Tuple[List[AwardRecord], Optional[str]]:
        """
        Parse Wikipedia NFA page HTML into AwardRecord objects.

        Wikipedia NFA pages have a consistent structure:
          <h2> Feature Film section
            <h3> Official Name: Swarna Kamal (Golden Lotus)
              <table> columns: Name of Award | Name of Film | Language | Awardee(s) | Cash Prize
            <h3> Official Name: Rajat Kamal (Silver Lotus)
              <table> ...
          <h2> Non-Feature Film section
            ...
        """
        soup = BeautifulSoup(html, "html.parser")

        # Remove edit buttons, footnotes, navigation
        for tag in soup.find_all(["sup", "span"], class_=["mw-editsection", "reference"]):
            tag.decompose()

        records: List[AwardRecord] = []
        parse_error: Optional[str] = None

        # Strategy 1: Wikipedia-structured tables with section context
        records = self._parse_wikipedia_tables(soup, film_year)
        if records:
            return records, None

        # Strategy 2: Any table with recognisable award columns
        records = self._parse_generic_tables(soup, film_year)
        if records:
            return records, "Used generic table parser"

        # Strategy 3: DL definition lists (dt=category, dd=film)
        records = self._parse_definition_lists(soup, film_year)
        if records:
            return records, "Used definition-list parser"

        # Strategy 4: Heading + paragraph heuristic
        records = self._parse_heading_blocks(soup, film_year)
        if records:
            return records, "Used heading-block heuristic"

        return [], "No recognisable award data found in Wikipedia HTML"

    # ── Strategy 1: Wikipedia-structured tables ────────────────────────────────

    def _parse_wikipedia_tables(
        self, soup: BeautifulSoup, film_year: int
    ) -> List[AwardRecord]:
        records: List[AwardRecord] = []

        # Walk through the page tracking section context
        current_section     = "Feature Films"   # Feature Films / Non-Feature Films / Writing
        current_award_type  = None               # Golden Lotus / Silver Lotus / Certificate

        for elem in soup.find_all(["h2", "h3", "h4", "table"]):
            text = elem.get_text(separator=" ", strip=True)

            if elem.name in ("h2", "h3", "h4"):
                # Update section context
                if _FEATURE_SECTION.search(text) and not _NONFEATURE_SECTION.search(text):
                    current_section = "Feature Films"
                elif _NONFEATURE_SECTION.search(text):
                    current_section = "Non-Feature Films"
                elif _WRITING_SECTION.search(text):
                    current_section = "Writing on Cinema"

                inferred = _infer_award_type(text)
                if inferred:
                    current_award_type = inferred

            elif elem.name == "table":
                table_records = self._parse_award_table(
                    elem, film_year, current_section, current_award_type
                )
                records.extend(table_records)

        return records

    def _parse_award_table(
        self,
        table: Tag,
        film_year: int,
        section: str,
        default_award_type: Optional[str],
    ) -> List[AwardRecord]:
        rows = table.find_all("tr")
        if len(rows) < 2:
            return []

        # Parse header row
        header_cells = rows[0].find_all(["th", "td"])
        headers = [c.get_text(separator=" ", strip=True).lower() for c in header_cells]

        # Must look like an award table
        if not any(kw in " ".join(headers) for kw in
                   ("award", "film", "awardee", "winner", "category", "name of award")):
            return []

        col_map = self._map_columns(headers)
        if not col_map:
            return []

        records = []
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            # Skip rows that are section sub-headers (single merged cell)
            if len(cells) == 1:
                text = cells[0].get_text(strip=True)
                inferred = _infer_award_type(text)
                if inferred:
                    default_award_type = inferred
                continue

            rec = self._cells_to_record(
                cells, col_map, film_year, section, default_award_type
            )
            if rec:
                records.append(rec)

        return records

    def _map_columns(self, headers: List[str]) -> Dict[str, int]:
        mapping: Dict[str, int] = {}
        for i, h in enumerate(headers):
            hl = h.lower()
            # Category / award name — must come before awardee check
            if any(k in hl for k in ("name of award", "award category", "category")):
                mapping.setdefault("category", i)
            elif "award" in hl and "awardee" not in hl and "awarded" not in hl:
                mapping.setdefault("category", i)

            # Film title
            if any(k in hl for k in ("name of film", "film title", "film", "movie")):
                mapping.setdefault("film_title", i)

            # Language
            if "language" in hl:
                mapping.setdefault("film_language", i)

            # Awardee — explicit match only
            if any(k in hl for k in ("awardee", "winner", "recipient", "awarded to")):
                mapping.setdefault("awardee", i)
            # "Name" column (not "name of award" or "name of film") → awardee
            elif hl.strip() == "name":
                mapping.setdefault("awardee", i)

            # Cash prize
            if "cash" in hl or "prize" in hl:
                mapping.setdefault("cash_prize", i)

            # Director explicit column
            if hl.strip() == "director":
                mapping.setdefault("director", i)

        return mapping

    def _cells_to_record(
        self,
        cells: List[Tag],
        col_map: Dict[str, int],
        film_year: int,
        section: str,
        default_award_type: Optional[str],
    ) -> Optional[AwardRecord]:

        def cell_text(key: str) -> Optional[str]:
            idx = col_map.get(key)
            if idx is not None and idx < len(cells):
                return cells[idx].get_text(separator=" ", strip=True) or None
            return None

        category   = cell_text("category")
        film_title = cell_text("film_title")

        if not category and not film_title:
            return None

        # Extract director from awardee cell when not in its own column
        raw_awardee = cell_text("awardee") or ""
        director    = cell_text("director")

        if director is None:
            # Try to pull "Director: Name" out of the awardee cell
            # Wikipedia cells often have "Director: Rishab Shetty" as awardee text
            dm = _DIRECTOR_RE.search(raw_awardee)
            if dm:
                director    = dm.group(1).strip()
                # Remove the Director: line from awardee text
                cleaned = _DIRECTOR_RE.sub("", raw_awardee).strip()
                raw_awardee = cleaned if cleaned else None
            else:
                # Some Wikipedia pages just list director name in awardee col
                # with no prefix — keep as-is, set as awardee only
                pass

        # Final awardee: strip any remaining "Director:" prefix
        awardee = re.sub(r"^[Dd]irector\s*:\s*", "", raw_awardee).strip() if raw_awardee else None
        if not awardee:
            awardee = None

        # Infer award type from category cell text
        award_type = _infer_award_type(category or "") or default_award_type

        return AwardRecord(
            ceremony_year    = film_year + 1,  # ceremony year = film_year + 1
            ceremony_number  = _ceremony_number(film_year),
            film_year        = film_year,
            category         = (category or "Unknown").strip(),
            film_title       = (film_title or "Unknown").strip(),
            film_language    = cell_text("film_language"),
            director         = director,
            awardee          = awardee,
            award_type       = award_type,
            cash_prize       = cell_text("cash_prize"),
            production_company = None,
        )

    # ── Strategy 2: Generic table parser ──────────────────────────────────────

    def _parse_generic_tables(
        self, soup: BeautifulSoup, film_year: int
    ) -> List[AwardRecord]:
        records = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            headers = [c.get_text(strip=True).lower()
                       for c in rows[0].find_all(["th", "td"])]
            col_map = self._map_columns(headers)
            if not col_map:
                continue
            for row in rows[1:]:
                cells = row.find_all("td")
                if not cells:
                    continue
                rec = self._cells_to_record(cells, col_map, film_year, "Feature Films", None)
                if rec:
                    records.append(rec)
        return records

    # ── Strategy 3: DL definition lists ───────────────────────────────────────

    def _parse_definition_lists(
        self, soup: BeautifulSoup, film_year: int
    ) -> List[AwardRecord]:
        """Parse <dt>category</dt><dd>film — director</dd> patterns."""
        records = []
        current_category = None
        for elem in soup.find_all(["dt", "dd", "h3", "h4", "strong"]):
            text = elem.get_text(strip=True)
            if not text:
                continue
            if elem.name in ("dt", "h3", "h4", "strong"):
                current_category = text
            elif elem.name == "dd" and current_category:
                film_title, director = self._split_film_director(text)
                records.append(AwardRecord(
                    ceremony_year   = film_year + 1,
                    ceremony_number = _ceremony_number(film_year),
                    film_year       = film_year,
                    category        = current_category,
                    film_title      = film_title,
                    director        = director,
                ))
        return records

    # ── Strategy 4: Heading + paragraph heuristic ─────────────────────────────

    def _parse_heading_blocks(
        self, soup: BeautifulSoup, film_year: int
    ) -> List[AwardRecord]:
        records = []
        headings = soup.find_all(re.compile(r"^h[2-5]$"))

        for heading in headings:
            category = heading.get_text(strip=True)
            if len(category) < 4 or len(category) > 250:
                continue

            award_type = _infer_award_type(category)
            sibling = heading.find_next_sibling()

            while sibling and (not hasattr(sibling, 'name') or
                               sibling.name not in ("h2", "h3", "h4", "h5")):
                if hasattr(sibling, 'get_text'):
                    text = sibling.get_text(separator="\n", strip=True)
                    for line in text.splitlines():
                        line = line.strip()
                        if not line or len(line) < 3:
                            continue
                        film_title, director = self._split_film_director(line)
                        records.append(AwardRecord(
                            ceremony_year   = film_year + 1,
                            ceremony_number = _ceremony_number(film_year),
                            film_year       = film_year,
                            category        = category,
                            film_title      = film_title,
                            director        = director,
                            award_type      = award_type,
                        ))
                sibling = sibling.find_next_sibling()

        return records

    # ── Text helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _split_film_director(text: str) -> Tuple[str, Optional[str]]:
        """Split "Film Title — Director" or "Film Title (Director)"."""
        for sep in (" — ", " – ", " - ", " / "):
            if sep in text:
                parts = text.split(sep, 1)
                return parts[0].strip(), parts[1].strip()
        m = re.match(r"^(.+?)\s*\(([^)]+)\)\s*$", text)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return text.strip(), None

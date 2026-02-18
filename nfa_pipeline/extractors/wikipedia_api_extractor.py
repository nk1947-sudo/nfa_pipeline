"""
nfa_pipeline/extractors/wikipedia_api_extractor.py
----------------------------------------------------
Uses the Wikipedia REST API to fetch clean HTML/content without
triggering robots.txt issues.

API endpoint:
  https://en.wikipedia.org/api/rest_v1/page/html/{title}

This is Wikipedia's officially documented API for programmatic access,
explicitly recommended for bots and data pipelines.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import List, Optional, Tuple
from urllib.parse import quote

from bs4 import BeautifulSoup

from ..models import AwardRecord, YearPage
from .wikipedia_extractor import (
    WikipediaExtractor, ceremony_number, ordinal_suffix
)

logger = logging.getLogger(__name__)

WIKI_API_BASE  = "https://en.wikipedia.org/api/rest_v1/page/html"
WIKI_PAGE_BASE = "https://en.wikipedia.org/wiki"


def api_url(film_year: int) -> str:
    n    = ceremony_number(film_year)
    s    = ordinal_suffix(n)
    title = f"{n}{s}_National_Film_Awards"
    return f"{WIKI_API_BASE}/{quote(title)}"


def page_url(film_year: int) -> str:
    n    = ceremony_number(film_year)
    s    = ordinal_suffix(n)
    return f"{WIKI_PAGE_BASE}/{n}{s}_National_Film_Awards"


class WikipediaAPIExtractor(WikipediaExtractor):
    """
    Fetches NFA data via the Wikipedia REST API.
    Inherits all parsing logic from WikipediaExtractor.
    The API returns the same HTML but bypasses robots.txt issues.
    """

    def extract_year(self, film_year: int) -> YearPage:
        url      = api_url(film_year)
        page_ref = page_url(film_year)
        n        = ceremony_number(film_year)

        logger.info("[WikipediaAPI] film_year=%d → %d%s NFA",
                    film_year, n, ordinal_suffix(n))

        # Try REST API first
        html = self._http.get(url)

        # Fallback to direct page URL
        if not html:
            logger.warning("[WikipediaAPI] REST API failed, trying direct URL")
            html = self._http.get(page_ref)

        if not html:
            return YearPage(
                year=film_year, url=url, html="",
                http_status=0,
                parse_error="Both Wikipedia API and direct URL failed",
                source="wikipedia_api",
            )

        records, err = self._parse(html, film_year, page_ref)
        page = YearPage(
            year=film_year, url=page_ref, html=html,
            scraped_at=datetime.utcnow(),
            records=records, parse_error=err,
            source="wikipedia_api",
        )
        html_hash = hashlib.md5(html.encode()).hexdigest()
        for r in records:
            r.source_url    = page_ref
            r.raw_html_hash = html_hash
            r.scraped_at    = page.scraped_at
            r.data_source   = "wikipedia_api"

        logger.info("[WikipediaAPI] film_year=%d: %d records%s",
                    film_year, len(records), f" [WARN:{err}]" if err else "")
        return page

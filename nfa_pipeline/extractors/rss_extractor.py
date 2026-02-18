"""
nfa_pipeline/extractors/rss_extractor.py
------------------------------------------
RSS feed fallback for NFA award records that OMDb cannot enrich.

When OMDb returns no data for a film title, this extractor searches
a configurable list of news RSS feeds (Google News, NDTV, Times of India,
etc.) and returns supplementary metadata extracted from matching articles.

Fields returned (when found):
  plot_summary  – article description / snippet
  source_url    – URL of the matching news article
  release_date  – publication date of the article (ISO format)

Usage:
    from nfa_pipeline.extractors.rss_extractor import RSSExtractor

    rss = RSSExtractor()
    data = rss.fetch("Aattam", film_year=2023)
    if data:
        rec.plot_summary = data.get("plot_summary")
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from difflib import SequenceMatcher
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

# ── Default RSS feed URLs ─────────────────────────────────────────────────────
# Google News RSS supports a `q=` query parameter for keyword search.
# Additional feeds can be added by the user via the constructor.

_GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
_NDTV_MOVIES_RSS = "https://feeds.feedburner.com/ndtvmovies"
_TOI_ENTERTAINMENT_RSS = "https://timesofindia.indiatimes.com/rssfeeds/-2128936835.cms"

DEFAULT_FEED_URLS: List[str] = [
    _GOOGLE_NEWS_RSS,
    _NDTV_MOVIES_RSS,
    _TOI_ENTERTAINMENT_RSS,
]

# Minimum title similarity to accept an RSS article as a match
_MIN_SIMILARITY = 0.45

# Maximum number of feed items to inspect per feed
_MAX_ITEMS = 30

# Request timeout in seconds
_TIMEOUT = 8


class RSSExtractor:
    """
    Fetches supplementary film metadata from news RSS feeds.

    Parameters
    ----------
    feed_urls : list[str], optional
        RSS feed URL templates. URLs containing ``{query}`` will have the
        film title substituted in. Others are fetched as-is and their items
        are searched for the film title.
    request_delay : float
        Seconds to wait between feed requests (polite crawling).
    """

    def __init__(
        self,
        feed_urls: Optional[List[str]] = None,
        request_delay: float = 0.5,
    ):
        self._feed_urls = feed_urls if feed_urls is not None else DEFAULT_FEED_URLS
        self._delay = request_delay
        self._cache: Dict[str, Optional[Dict]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, film_title: str, film_year: Optional[int] = None) -> Optional[Dict]:
        """
        Search RSS feeds for articles about *film_title*.

        Returns a dict with keys ``plot_summary``, ``source_url``,
        ``release_date`` (all may be None), or None if nothing was found.
        """
        if not film_title or len(film_title) < 3:
            return None

        cache_key = f"{film_title}::{film_year}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        result = self._search_feeds(film_title, film_year)
        self._cache[cache_key] = result
        return result

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _search_feeds(
        self, film_title: str, film_year: Optional[int]
    ) -> Optional[Dict]:
        """Iterate over configured feeds and return the best matching item."""
        query = urllib.parse.quote_plus(
            f"{film_title} Indian film" + (f" {film_year}" if film_year else "")
        )

        best_score = 0.0
        best_result: Optional[Dict] = None

        for feed_url_template in self._feed_urls:
            # Substitute query into template if it contains {query}
            url = (
                feed_url_template.format(query=query)
                if "{query}" in feed_url_template
                else feed_url_template
            )

            items = self._fetch_feed(url)
            if not items:
                time.sleep(self._delay)
                continue

            for item in items[:_MAX_ITEMS]:
                score = self._score_item(item, film_title, film_year)
                if score > best_score:
                    best_score = score
                    best_result = item

            time.sleep(self._delay)

        if best_result and best_score >= _MIN_SIMILARITY:
            logger.debug(
                "[RSS] Match for %r (score=%.2f): %s",
                film_title, best_score, best_result.get("source_url"),
            )
            return {
                "plot_summary": best_result.get("description"),
                "source_url":   best_result.get("source_url"),
                "release_date": best_result.get("pub_date"),
            }

        logger.debug("[RSS] No match found for %r", film_title)
        return None

    def _fetch_feed(self, url: str) -> List[Dict]:
        """Download and parse an RSS/Atom feed. Returns a list of item dicts."""
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; NFA-Pipeline/1.0; "
                        "+https://github.com/nfa-pipeline)"
                    )
                },
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                raw = resp.read()
        except Exception as exc:
            logger.debug("[RSS] Failed to fetch %s: %s", url, exc)
            return []

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            logger.debug("[RSS] XML parse error for %s: %s", url, exc)
            return []

        # Support both RSS 2.0 (<channel><item>) and Atom (<entry>)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items: List[Dict] = []

        # RSS 2.0
        for item in root.iter("item"):
            items.append(self._parse_rss_item(item))

        # Atom
        if not items:
            for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
                items.append(self._parse_atom_entry(entry, ns))

        return items

    @staticmethod
    def _parse_rss_item(item: ET.Element) -> Dict:
        def _text(tag: str) -> Optional[str]:
            el = item.find(tag)
            return el.text.strip() if el is not None and el.text else None

        return {
            "title":       _text("title"),
            "description": _text("description"),
            "source_url":  _text("link"),
            "pub_date":    _parse_date(_text("pubDate")),
        }

    @staticmethod
    def _parse_atom_entry(entry: ET.Element, ns: Dict) -> Dict:
        def _text(tag: str) -> Optional[str]:
            el = entry.find(tag, ns)
            return el.text.strip() if el is not None and el.text else None

        link_el = entry.find("{http://www.w3.org/2005/Atom}link")
        link = link_el.get("href") if link_el is not None else None

        return {
            "title":       _text("{http://www.w3.org/2005/Atom}title"),
            "description": _text("{http://www.w3.org/2005/Atom}summary"),
            "source_url":  link,
            "pub_date":    _parse_date(
                _text("{http://www.w3.org/2005/Atom}published")
                or _text("{http://www.w3.org/2005/Atom}updated")
            ),
        }

    @staticmethod
    def _score_item(
        item: Dict, film_title: str, film_year: Optional[int]
    ) -> float:
        """
        Return a similarity score [0, 1] between the RSS item and the film.

        Scoring:
          - Title similarity (SequenceMatcher) is the primary signal.
          - Bonus if the film year appears in the article title or description.
        """
        article_title = item.get("title") or ""
        description   = item.get("description") or ""
        combined      = f"{article_title} {description}".lower()

        # Base similarity against article title
        ratio = SequenceMatcher(
            None, film_title.lower(), article_title.lower()
        ).ratio()

        # Also check if film title appears as a substring in the combined text
        if film_title.lower() in combined:
            ratio = max(ratio, 0.5)

        # Year bonus
        if film_year and str(film_year) in combined:
            ratio = min(ratio + 0.1, 1.0)

        return ratio


# ── Utility ───────────────────────────────────────────────────────────────────

_DATE_FORMATS = [
    "%a, %d %b %Y %H:%M:%S %z",   # RFC 2822 (RSS)
    "%a, %d %b %Y %H:%M:%S GMT",
    "%Y-%m-%dT%H:%M:%SZ",          # ISO 8601 (Atom)
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d",
]


def _parse_date(raw: Optional[str]) -> Optional[str]:
    """Parse a date string from RSS/Atom and return ISO-format date, or None."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Last-resort: extract 4-digit year
    m = re.search(r"\b(\d{4})\b", raw)
    return m.group(1) if m else None

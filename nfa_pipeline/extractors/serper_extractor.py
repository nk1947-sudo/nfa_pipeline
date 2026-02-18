"""
nfa_pipeline/extractors/serper_extractor.py
---------------------------------------------
Uses the Serper API (Google Search) to discover NFA award pages
and supplement missing data.

Serper API: https://serper.dev
Get a free key at serper.dev (2,500 free searches/month).
Set env var: NFA_SERPER_API_KEY=your_key_here

Role in pipeline:
  1. Search for each NFA ceremony to find authoritative URLs
  2. Verify/fill gaps when Wikipedia parsing yields incomplete data
  3. Find director/language info for films not in Wikipedia tables
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SERPER_ENDPOINT = "https://google.serper.dev/search"


class SerperExtractor:
    """
    Uses Serper (Google Search API) to find and verify NFA data.

    Parameters
    ----------
    http_client : HTTPClient
        Configured HTTP client.
    api_key : str, optional
        Serper API key. Falls back to NFA_SERPER_API_KEY env var.
    """

    def __init__(self, http_client, api_key: Optional[str] = None):
        self._http    = http_client
        self._api_key = api_key or os.environ.get("NFA_SERPER_API_KEY", "")
        if not self._api_key:
            logger.warning("[Serper] No API key set — Serper enrichment disabled. "
                           "Set NFA_SERPER_API_KEY env var to enable.")

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def search_ceremony(self, film_year: int) -> List[Dict]:
        """
        Search for NFA ceremony awards for a given film year.
        Returns list of search result dicts with title, link, snippet.
        """
        if not self.enabled:
            return []

        from nfa_pipeline.extractors.wikipedia_extractor import ceremony_number, ordinal_suffix
        n     = ceremony_number(film_year)
        s     = ordinal_suffix(n)
        query = f"{n}{s} National Film Awards India winners {film_year} list"

        logger.info("[Serper] Searching: %s", query)
        return self._search(query)

    def search_film_director(self, film_title: str, film_year: int) -> Optional[str]:
        """Search for a film's director when not found in primary sources."""
        if not self.enabled:
            return None
        query  = f'"{film_title}" Indian film {film_year} director'
        results = self._search(query, num=3)
        for r in results:
            snippet = r.get("snippet", "")
            # Try to extract director from snippet
            m = re.search(r"[Dd]irected\s+by\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
                          snippet)
            if m:
                return m.group(1).strip()
        return None

    def search_film_language(self, film_title: str, film_year: int) -> Optional[str]:
        """Search for a film's language when not found in primary sources."""
        if not self.enabled:
            return None
        known_languages = [
            "Hindi", "Tamil", "Telugu", "Malayalam", "Kannada",
            "Bengali", "Marathi", "Odia", "Punjabi", "Assamese",
            "Gujarati", "Urdu", "Manipuri", "Maithili", "Konkani",
        ]
        query   = f'"{film_title}" Indian film {film_year} language'
        results = self._search(query, num=3)
        for r in results:
            text = (r.get("title", "") + " " + r.get("snippet", "")).lower()
            for lang in known_languages:
                if lang.lower() in text:
                    return lang
        return None

    def _search(self, query: str, num: int = 10) -> List[Dict]:
        """Execute a Serper API search and return organic results."""
        import requests
        try:
            resp = requests.post(
                SERPER_ENDPOINT,
                headers={
                    "X-API-KEY":    self._api_key,
                    "Content-Type": "application/json",
                },
                json={"q": query, "num": num, "gl": "in", "hl": "en"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("organic", [])
            logger.debug("[Serper] Query=%r → %d results", query, len(results))
            return results
        except Exception as exc:
            logger.warning("[Serper] Search failed for %r: %s", query, exc)
            return []


import re  # noqa: E402 — needed by search_film_director

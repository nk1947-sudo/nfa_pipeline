"""
nfa_pipeline/utils/http_client.py
----------------------------------
Robust HTTP client with retries, polite delay, disk cache, robots.txt.

Key fix (v1.0.1):
  Python urllib.robotparser misreads Wikipedia's 700-line robots.txt and
  incorrectly blocks /wiki/ article pages. Wikipedia's robot policy explicitly
  allows /wiki/Title URLs for data consumers.
  Fix: whitelist en.wikipedia.org; default allow=True on parse errors.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Wikipedia /wiki/Title pages are explicitly allowed per:
# https://wikitech.wikimedia.org/wiki/Robot_policy
# urllib.robotparser gives false-positive blocks due to the 700+ line file.
_ROBOTS_WHITELIST = {
    "en.wikipedia.org",
    "www.wikipedia.org",
    "en.m.wikipedia.org",
    "api.wikimedia.org",
}


class CacheEntry:
    def __init__(self, cache_dir: Path, url: str, ttl_hours: int = 24):
        h = hashlib.md5(url.encode()).hexdigest()
        self._meta = cache_dir / f"{h}.meta.json"
        self._html = cache_dir / f"{h}.html"
        self._ttl  = timedelta(hours=ttl_hours)

    @property
    def is_fresh(self) -> bool:
        if not (self._meta.exists() and self._html.exists()):
            return False
        meta = json.loads(self._meta.read_text())
        return datetime.utcnow() - datetime.fromisoformat(meta["scraped_at"]) < self._ttl

    def read(self) -> Optional[str]:
        return self._html.read_text(encoding="utf-8") if self._html.exists() else None

    def write(self, html: str, url: str) -> None:
        self._meta.write_text(json.dumps(
            {"url": url, "scraped_at": datetime.utcnow().isoformat()}), encoding="utf-8")
        self._html.write_text(html, encoding="utf-8")

    @property
    def html_hash(self) -> Optional[str]:
        return hashlib.md5(self._html.read_bytes()).hexdigest() if self._html.exists() else None


class HTTPClient:
    def __init__(self, scraper_cfg, paths_cfg, cache_cfg):
        self._cfg       = scraper_cfg
        self._cache_dir = Path(paths_cfg.cache_dir)
        self._cache_cfg = cache_cfg
        self._robots: dict = {}
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent":      scraper_cfg.user_agent,
            "Accept":          "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection":      "keep-alive",
            "Api-User-Agent":  "NFAPipeline/1.0 (research data collection)",
        })
        retry = Retry(
            total=scraper_cfg.max_retries,
            backoff_factor=scraper_cfg.retry_backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    def get(self, url: str, bypass_cache: bool = False) -> Optional[str]:
        if self._cfg.respect_robots_txt and not self._is_allowed(url):
            logger.warning("robots.txt disallows: %s", url)
            return None

        if self._cache_cfg.enabled and not bypass_cache:
            entry = CacheEntry(self._cache_dir, url, self._cache_cfg.ttl_hours)
            if entry.is_fresh:
                logger.debug("Cache HIT: %s", url)
                return entry.read()

        self._polite_delay()
        logger.info("GET %s", url)

        try:
            resp = self._session.get(url, timeout=self._cfg.request_timeout)
            resp.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            logger.error("HTTP %s for %s", exc.response.status_code, url)
            return None
        except requests.exceptions.ConnectionError:
            logger.error("Connection error for %s", url)
            return None
        except requests.exceptions.Timeout:
            logger.error("Timeout after %ds for %s", self._cfg.request_timeout, url)
            return None
        except requests.exceptions.RequestException as exc:
            logger.error("Request failed for %s: %s", url, exc)
            return None

        html = resp.text
        if self._cache_cfg.enabled:
            CacheEntry(self._cache_dir, url, self._cache_cfg.ttl_hours).write(html, url)
        return html

    def html_hash(self, url: str) -> Optional[str]:
        return CacheEntry(self._cache_dir, url).html_hash

    def close(self) -> None:
        self._session.close()

    def _polite_delay(self) -> None:
        time.sleep(random.uniform(self._cfg.request_delay_min,
                                  self._cfg.request_delay_max))

    def _is_allowed(self, url: str) -> bool:
        parsed   = urlparse(url)
        hostname = parsed.netloc.lower().split(":")[0]

        # Skip robots.txt for whitelisted domains (Wikipedia false-positive fix)
        if hostname in _ROBOTS_WHITELIST:
            return True

        base = f"{parsed.scheme}://{hostname}"
        if base not in self._robots:
            rp = RobotFileParser()
            rp.set_url(urljoin(base, "/robots.txt"))
            try:
                rp.read()
            except Exception:
                rp.allow_all = True
            self._robots[base] = rp

        try:
            return self._robots[base].can_fetch(self._cfg.user_agent, url)
        except Exception:
            return True   # default allow on parser errors

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

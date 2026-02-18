"""
config/config.py
----------------
Centralised configuration loader.

Loads settings.yaml, applies environment variable overrides,
and exposes a typed Config dataclass throughout the pipeline.

Usage
-----
    from nfa_pipeline.config import get_config
    cfg = get_config()
    print(cfg.scraper.base_url)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# ── Singleton cache ──────────────────────────────────────────────────────────
_CONFIG_INSTANCE: Optional["Config"] = None

# Search for settings.yaml in candidate locations (most-specific first):
#   1. <project-root>/config/settings.yaml   ← standard layout
#   2. <package-dir>/settings.yaml           ← legacy / bundled fallback
def _find_default_config() -> Path:
    candidates = [
        Path(__file__).parent.parent / "config" / "settings.yaml",  # project root/config/
        Path(__file__).parent / "settings.yaml",                     # inside package
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]  # return preferred path even if missing (triggers warning)

_DEFAULT_CONFIG_PATH = _find_default_config()


# ── Dataclass hierarchy ──────────────────────────────────────────────────────

@dataclass
class ScraperConfig:
    base_url: str = "https://en.wikipedia.org"
    awards_url: str = "https://en.wikipedia.org/wiki/{ordinal}_National_Film_Awards"
    start_year: int = 2000
    end_year: int = 2023
    request_timeout: int = 30
    request_delay_min: float = 1.0
    request_delay_max: float = 3.0
    max_retries: int = 3
    retry_backoff_factor: float = 2.0
    user_agent: str = "NFA-Pipeline/1.0"
    max_concurrent_requests: int = 3
    respect_robots_txt: bool = True


@dataclass
class PathsConfig:
    data_dir: str = "data"
    raw_dir: str = "data/raw"
    processed_dir: str = "data/processed"
    logs_dir: str = "data/logs"
    cache_dir: str = "data/cache"
    output_excel: str = "data/processed/nfa_awards.xlsx"
    output_csv: str = "data/processed/nfa_awards.csv"
    output_json: str = "data/processed/nfa_awards.json"
    checkpoint_file: str = "data/cache/checkpoint.json"


@dataclass
class TransformerConfig:
    strip_whitespace: bool = True
    normalize_unicode: bool = True
    title_case_fields: List[str] = field(default_factory=list)
    language_aliases: Dict[str, str] = field(default_factory=dict)


@dataclass
class ValidatorConfig:
    required_fields: List[str] = field(default_factory=lambda: ["ceremony_year", "category", "film_title"])
    year_min: int = 1954
    year_max: int = 2030
    max_film_title_length: int = 300
    max_director_length: int = 200
    expected_awards_per_year_min: int = 30
    expected_awards_per_year_max: int = 200
    completeness_threshold: float = 0.70


@dataclass
class SQLiteConfig:
    enabled: bool = True
    path: str = "data/processed/nfa_awards.db"
    pragmas: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExcelConfig:
    enabled: bool = True
    freeze_panes: bool = True
    auto_filter: bool = True
    sheet_name: str = "NFA Awards"
    summary_sheet: bool = True


@dataclass
class CSVConfig:
    enabled: bool = True
    encoding: str = "utf-8-sig"
    delimiter: str = ","


@dataclass
class JSONConfig:
    enabled: bool = True
    indent: int = 2


@dataclass
class StorageConfig:
    sqlite: SQLiteConfig = field(default_factory=SQLiteConfig)
    excel: ExcelConfig = field(default_factory=ExcelConfig)
    csv: CSVConfig = field(default_factory=CSVConfig)
    json: JSONConfig = field(default_factory=JSONConfig)
    per_run_output_folder: bool = True


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s"
    date_format: str = "%Y-%m-%d %H:%M:%S"


@dataclass
class CacheConfig:
    enabled: bool = True
    ttl_hours: int = 24
    strategy: str = "disk"


@dataclass
class EnrichmentConfig:
    """API keys and settings for enrichment sources."""
    # OMDb API (https://www.omdbapi.com — 1000 req/day free)
    omdb_api_key:   str  = ""
    omdb_enabled:   bool = True
    omdb_delay:     float = 0.15

    # Serper API (https://serper.dev — 2500 searches/month free)
    serper_api_key: str  = ""
    serper_enabled: bool = True

    # IMDb scraper (no key needed, polite scraping)
    imdb_enabled:   bool = True
    imdb_delay:     float = 1.0


@dataclass
class Config:
    scraper:     ScraperConfig     = field(default_factory=ScraperConfig)
    paths:       PathsConfig       = field(default_factory=PathsConfig)
    transformer: TransformerConfig = field(default_factory=TransformerConfig)
    validator:   ValidatorConfig   = field(default_factory=ValidatorConfig)
    storage:     StorageConfig     = field(default_factory=StorageConfig)
    logging:     LoggingConfig     = field(default_factory=LoggingConfig)
    cache:       CacheConfig       = field(default_factory=CacheConfig)
    enrichment:  EnrichmentConfig  = field(default_factory=EnrichmentConfig)


# ── Loader ───────────────────────────────────────────────────────────────────

def _dict_to_dataclass(cls, data: Dict[str, Any]):
    """Recursively map a dict onto a dataclass, skipping unknown keys."""
    import dataclasses

    if not dataclasses.is_dataclass(cls):
        return data

    kwargs: Dict[str, Any] = {}
    hints = {f.name: f for f in dataclasses.fields(cls)}

    for fname, field_obj in hints.items():
        if fname not in data:
            continue
        val = data[fname]
        ftype = field_obj.type

        # Resolve forward references stored as string annotations
        if isinstance(ftype, str):
            ftype = eval(ftype, globals())  # noqa: S307 – internal use only

        # Recurse into nested dataclasses
        if dataclasses.is_dataclass(ftype) and isinstance(val, dict):
            kwargs[fname] = _dict_to_dataclass(ftype, val)
        else:
            kwargs[fname] = val

    return cls(**kwargs)


def _apply_env_overrides(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply environment variable overrides.

    Format: NFA__SECTION__KEY=value
    Example: NFA__SCRAPER__START_YEAR=2010
    """
    prefix = "NFA__"
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        parts = env_key[len(prefix):].lower().split("__")
        if len(parts) != 2:
            continue
        section, key = parts
        if section in raw and isinstance(raw[section], dict):
            # Attempt numeric / bool coercion
            coerced: Any = env_val
            if env_val.lower() in ("true", "yes"):
                coerced = True
            elif env_val.lower() in ("false", "no"):
                coerced = False
            else:
                try:
                    coerced = int(env_val)
                except ValueError:
                    try:
                        coerced = float(env_val)
                    except ValueError:
                        pass
            raw[section][key] = coerced
            logger.debug("Env override applied: %s.%s = %r", section, key, coerced)

    return raw


def load_config(config_path: Optional[Path] = None) -> Config:
    """
    Load configuration from *config_path* (YAML), apply env overrides,
    and return a typed :class:`Config` object.

    Parameters
    ----------
    config_path : Path, optional
        Path to the YAML settings file.  Defaults to ``config/settings.yaml``
        next to this module.

    Returns
    -------
    Config
        Fully populated configuration dataclass.
    """
    path = config_path or _DEFAULT_CONFIG_PATH

    if not path.exists():
        logger.warning("Config file not found at %s — using defaults", path)
        return Config()

    with open(path, "r", encoding="utf-8") as fh:
        raw: Dict[str, Any] = yaml.safe_load(fh) or {}

    raw = _apply_env_overrides(raw)

    cfg = Config(
        scraper=_dict_to_dataclass(ScraperConfig, raw.get("scraper", {})),
        paths=_dict_to_dataclass(PathsConfig, raw.get("paths", {})),
        transformer=_dict_to_dataclass(TransformerConfig, raw.get("transformer", {})),
        validator=_dict_to_dataclass(ValidatorConfig, raw.get("validator", {})),
        storage=StorageConfig(
            sqlite=_dict_to_dataclass(SQLiteConfig, raw.get("storage", {}).get("sqlite", {})),
            excel=_dict_to_dataclass(ExcelConfig, raw.get("storage", {}).get("excel", {})),
            csv=_dict_to_dataclass(CSVConfig, raw.get("storage", {}).get("csv", {})),
            json=_dict_to_dataclass(JSONConfig, raw.get("storage", {}).get("json", {})),
            per_run_output_folder=raw.get("storage", {}).get("per_run_output_folder", True),
        ),
        logging=_dict_to_dataclass(LoggingConfig, raw.get("logging", {})),
        cache=_dict_to_dataclass(CacheConfig, raw.get("cache", {})),
        enrichment=_dict_to_dataclass(EnrichmentConfig, raw.get("enrichment", {})),
    )

    logger.debug("Configuration loaded from %s", path)
    return cfg


def get_config(config_path: Optional[Path] = None) -> Config:
    """Return the global singleton Config, loading it on first call."""
    global _CONFIG_INSTANCE  # noqa: PLW0603
    if _CONFIG_INSTANCE is None:
        _CONFIG_INSTANCE = load_config(config_path)
    return _CONFIG_INSTANCE


def reset_config() -> None:
    """Reset the singleton (useful for testing)."""
    global _CONFIG_INSTANCE  # noqa: PLW0603
    _CONFIG_INSTANCE = None

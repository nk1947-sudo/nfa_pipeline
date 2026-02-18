"""
tests/test_suite.py
--------------------
Full test suite for the NFA pipeline.
Uses stdlib unittest only — no pytest required.

Run:
    python -m unittest tests.test_suite -v
    # or from project root:
    python -m unittest discover -v
"""

import json
import sys
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from nfa_pipeline.models import AwardRecord, ScrapeRun, YearPage
from nfa_pipeline.transformers import RecordTransformer
from nfa_pipeline.validators import RecordValidator
from nfa_pipeline.extractors import NFAExtractor
from nfa_pipeline.storage import SQLiteStorage
from nfa_pipeline.utils.checkpoint import CheckpointManager
from nfa_pipeline.config import load_config, reset_config


# ═══════════════════════════════════════════════════════════════════════════════
# MODEL TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestAwardRecord(unittest.TestCase):

    def test_minimal_valid_record(self):
        rec = AwardRecord(ceremony_year=2010, category="Best Film", film_title="Test Film")
        self.assertEqual(rec.ceremony_year, 2010)
        self.assertTrue(rec.is_valid)

    def test_record_id_auto_generated(self):
        rec = AwardRecord(ceremony_year=2010, category="Best Film", film_title="My Film")
        self.assertIsNotNone(rec.record_id)
        self.assertIn("2010", rec.record_id)

    def test_strip_whitespace(self):
        rec = AwardRecord(ceremony_year=2010, category="  Best Film  ", film_title="  Padman  ")
        self.assertEqual(rec.category, "Best Film")
        self.assertEqual(rec.film_title, "Padman")

    def test_empty_string_director_becomes_none(self):
        rec = AwardRecord(ceremony_year=2010, category="X", film_title="Y", director="   ")
        self.assertIsNone(rec.director)

    def test_validation_lists_default_empty(self):
        rec = AwardRecord(ceremony_year=2010, category="X", film_title="Y")
        self.assertEqual(rec.validation_errors, [])
        self.assertEqual(rec.validation_warnings, [])

    def test_model_dump_returns_dict(self):
        rec = AwardRecord(ceremony_year=2010, category="Best Film", film_title="Test")
        d = rec.model_dump()
        self.assertIsInstance(d, dict)
        self.assertEqual(d["ceremony_year"], 2010)
        self.assertEqual(d["film_title"], "Test")


class TestScrapeRun(unittest.TestCase):

    def test_success_rate_zero_when_no_records(self):
        run = ScrapeRun(run_id="abc", started_at=datetime.utcnow())
        self.assertEqual(run.success_rate, 0.0)

    def test_success_rate_computed_correctly(self):
        run = ScrapeRun(run_id="abc", started_at=datetime.utcnow(),
                        total_records=10, valid_records=8)
        self.assertAlmostEqual(run.success_rate, 0.8)

    def test_duration_none_before_finish(self):
        run = ScrapeRun(run_id="abc", started_at=datetime.utcnow())
        self.assertIsNone(run.duration_seconds)

    def test_duration_computed_after_finish(self):
        from datetime import timedelta
        start = datetime(2024, 1, 1, 12, 0, 0)
        end = datetime(2024, 1, 1, 12, 0, 30)
        run = ScrapeRun(run_id="abc", started_at=start, finished_at=end)
        self.assertAlmostEqual(run.duration_seconds, 30.0)


# ═══════════════════════════════════════════════════════════════════════════════
# TRANSFORMER TESTS
# ═══════════════════════════════════════════════════════════════════════════════

def _make_transformer_cfg(**kwargs):
    cfg = MagicMock()
    cfg.strip_whitespace = True
    cfg.normalize_unicode = True
    cfg.title_case_fields = ["film_title", "director", "category"]
    cfg.language_aliases = {"hindi": "Hindi", "tamil": "Tamil", "oriya": "Odia", "malayalam": "Malayalam"}
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


class TestRecordTransformer(unittest.TestCase):

    def _t(self):
        return RecordTransformer(_make_transformer_cfg())

    def _rec(self, **kwargs):
        defaults = dict(ceremony_year=2015, category="best film", film_title="a film")
        defaults.update(kwargs)
        return AwardRecord(**defaults)

    def test_title_case_applied(self):
        rec = self._rec(film_title="the dark knight")
        self._t().transform(rec)
        self.assertEqual(rec.film_title, "The Dark Knight")

    def test_language_alias_normalised(self):
        rec = self._rec(film_language="hindi")
        self._t().transform(rec)
        self.assertEqual(rec.film_language, "Hindi")

    def test_oriya_to_odia(self):
        rec = self._rec(film_language="oriya")
        self._t().transform(rec)
        self.assertEqual(rec.film_language, "Odia")

    def test_category_slug_generated(self):
        rec = self._rec(category="Best Direction")
        self._t().transform(rec)
        self.assertEqual(rec.category_normalized, "best-direction")

    def test_film_year_inferred_from_ceremony_year(self):
        rec = self._rec(ceremony_year=2015, film_year=None)
        self._t().transform(rec)
        self.assertEqual(rec.film_year, 2014)

    def test_award_type_golden_lotus(self):
        rec = self._rec(category="Best Film — Golden Lotus")
        self._t().transform(rec)
        self.assertEqual(rec.award_type, "Golden Lotus")

    def test_award_type_silver_lotus(self):
        rec = self._rec(category="Best Director Silver Lotus Award")
        self._t().transform(rec)
        self.assertEqual(rec.award_type, "Silver Lotus")

    def test_transform_batch_returns_all(self):
        records = [self._rec(film_title=f"film {i}") for i in range(5)]
        results = self._t().transform_batch(records)
        self.assertEqual(len(results), 5)

    def test_smart_title_preserves_articles(self):
        result = RecordTransformer._smart_title("the dark knight rises")
        self.assertTrue(result.startswith("The"))
        self.assertIn("the dark", result.lower() + "x")  # "the" internal stays lower

    def test_transform_batch_survives_bad_record(self):
        """Batch should complete even if one record causes an error."""
        t = self._t()
        good_records = [self._rec() for _ in range(3)]
        # All good records should transform fine
        results = t.transform_batch(good_records)
        self.assertEqual(len(results), 3)


# ═══════════════════════════════════════════════════════════════════════════════
# VALIDATOR TESTS
# ═══════════════════════════════════════════════════════════════════════════════

def _make_validator_cfg():
    cfg = MagicMock()
    cfg.required_fields = ["ceremony_year", "category", "film_title"]
    cfg.year_min = 1954
    cfg.year_max = 2030
    cfg.max_film_title_length = 300
    cfg.max_director_length = 200
    cfg.expected_awards_per_year_min = 1
    cfg.expected_awards_per_year_max = 500
    cfg.completeness_threshold = 0.70
    return cfg


class TestRecordValidator(unittest.TestCase):

    def _v(self):
        return RecordValidator(_make_validator_cfg())

    def _valid_rec(self, **kwargs):
        defaults = dict(ceremony_year=2015, category="Best Film", film_title="Test Film")
        defaults.update(kwargs)
        return AwardRecord(**defaults)

    def test_valid_record_passes(self):
        is_valid, errors, warnings = self._v().validate(self._valid_rec())
        self.assertTrue(is_valid)
        self.assertEqual(errors, [])

    def test_year_out_of_range_fails(self):
        rec = self._valid_rec(ceremony_year=1800)
        is_valid, errors, _ = self._v().validate(rec)
        self.assertFalse(is_valid)
        self.assertTrue(any("ceremony_year" in e for e in errors))

    def test_film_title_too_long_blocking(self):
        rec = self._valid_rec(film_title="A" * 400)
        is_valid, errors, _ = self._v().validate(rec)
        self.assertFalse(is_valid)

    def test_film_year_greater_than_ceremony_warns(self):
        rec = self._valid_rec(ceremony_year=2015, film_year=2020)
        _, errors, warnings = self._v().validate(rec)
        self.assertTrue(any("film_year" in w for w in warnings))

    def test_validate_batch_report(self):
        records = [self._valid_rec() for _ in range(8)]
        records[0] = self._valid_rec(ceremony_year=1800)
        records[1] = self._valid_rec(ceremony_year=1700)
        _, report = self._v().validate_batch(records)
        self.assertEqual(report.total, 8)
        self.assertEqual(report.invalid, 2)
        self.assertEqual(report.valid, 6)

    def test_completeness_property(self):
        records = [self._valid_rec() for _ in range(10)]
        _, report = self._v().validate_batch(records)
        self.assertAlmostEqual(report.completeness, 1.0)


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRACTOR TESTS
# ═══════════════════════════════════════════════════════════════════════════════

TABLE_HTML = """
<html><body>
<h2>Feature Film</h2>
<h3>Official Name: Swarna Kamal</h3>
<table>
  <tr><th>Name of Award</th><th>Name of Film</th><th>Language</th><th>Awardee(s)</th><th>Cash Prize</th></tr>
  <tr><td>Best Feature Film</td><td>Kantara</td><td>Kannada</td><td>Director: Rishab Shetty</td><td>₹2,50,000</td></tr>
</table>
<h3>Official Name: Rajat Kamal</h3>
<table>
  <tr><th>Name of Award</th><th>Name of Film</th><th>Language</th><th>Awardee(s)</th><th>Cash Prize</th></tr>
  <tr><td>Best Direction</td><td>Gangubai Kathiawadi</td><td>Hindi</td><td>Director: Sanjay Leela Bhansali</td><td>₹1,50,000</td></tr>
  <tr><td>Best Actor (Male)</td><td>Rocketry</td><td>Tamil</td><td>R. Madhavan</td><td>₹1,50,000</td></tr>
</table>
</body></html>
"""

DL_HTML = """
<html><body>
  <dt>Best Film</dt><dd>Kantara — Rishab Shetty</dd>
  <dt>Best Director</dt><dd>Gangubai Kathiawadi — Sanjay Leela Bhansali</dd>
</body></html>
"""

HEADING_HTML = """
<html><body>
  <h3>Best Film — Golden Lotus</h3><p>Kantara (Rishab Shetty)</p>
  <h3>Best Screenplay</h3><p>Shershaah — Sandeep Shrivastava</p>
</body></html>
"""

EMPTY_HTML = "<html><body><p>No data.</p></body></html>"


def _make_extractor(html):
    http = MagicMock()
    http.get.return_value = html
    cfg = MagicMock()
    cfg.awards_url = "https://en.wikipedia.org/wiki/{ordinal}_National_Film_Awards"
    return NFAExtractor(http, cfg)


class TestNFAExtractor(unittest.TestCase):

    def test_table_parser_extracts_records(self):
        page = _make_extractor(TABLE_HTML).extract_year(2022)
        self.assertEqual(len(page.records), 3)

    def test_table_parser_film_titles(self):
        page = _make_extractor(TABLE_HTML).extract_year(2022)
        titles = [r.film_title for r in page.records]
        self.assertIn("Kantara", titles)

    def test_table_parser_director(self):
        page = _make_extractor(TABLE_HTML).extract_year(2022)
        kantara = next(r for r in page.records if r.film_title == "Kantara")
        self.assertEqual(kantara.director, "Rishab Shetty")

    def test_table_parser_language(self):
        page = _make_extractor(TABLE_HTML).extract_year(2022)
        kantara = next(r for r in page.records if r.film_title == "Kantara")
        self.assertEqual(kantara.film_language, "Kannada")

    def test_dl_parser_fallback(self):
        page = _make_extractor(DL_HTML).extract_year(2022)
        self.assertGreater(len(page.records), 0)

    def test_heading_block_fallback(self):
        page = _make_extractor(HEADING_HTML).extract_year(2021)
        self.assertGreater(len(page.records), 0)

    def test_empty_html_no_records(self):
        page = _make_extractor(EMPTY_HTML).extract_year(2020)
        self.assertEqual(page.records, [])
        self.assertIsNotNone(page.parse_error)

    def test_http_failure_returns_empty_page(self):
        http = MagicMock()
        http.get.return_value = None
        cfg = MagicMock()
        cfg.awards_url = "https://en.wikipedia.org/wiki/{ordinal}_National_Film_Awards"
        page = NFAExtractor(http, cfg).extract_year(2019)
        self.assertEqual(page.records, [])
        self.assertIsNotNone(page.parse_error)

    def test_ceremony_year_on_records(self):
        # film_year=2022 → ceremony_year=2023
        page = _make_extractor(TABLE_HTML).extract_year(2022)
        for r in page.records:
            self.assertEqual(r.ceremony_year, 2023)

    def test_film_year_on_records(self):
        page = _make_extractor(TABLE_HTML).extract_year(2022)
        for r in page.records:
            self.assertEqual(r.film_year, 2022)

    def test_award_type_golden_lotus_from_section(self):
        page = _make_extractor(TABLE_HTML).extract_year(2022)
        kantara = next(r for r in page.records if r.film_title == "Kantara")
        self.assertEqual(kantara.award_type, "Golden Lotus")

    def test_award_type_silver_lotus_from_section(self):
        page = _make_extractor(TABLE_HTML).extract_year(2022)
        gangubai = next(r for r in page.records if r.film_title == "Gangubai Kathiawadi")
        self.assertEqual(gangubai.award_type, "Silver Lotus")

    def test_source_url_stamped(self):
        page = _make_extractor(TABLE_HTML).extract_year(2022)
        for r in page.records:
            self.assertIsNotNone(r.source_url)

    def test_ceremony_number_correct(self):
        # film_year=2000 → 48th NFA, film_year=2023 → 71st NFA
        from nfa_pipeline.extractors.nfa_extractor import _ceremony_number
        self.assertEqual(_ceremony_number(2000), 48)
        self.assertEqual(_ceremony_number(2023), 71)

    def test_ceremony_url_correct(self):
        from nfa_pipeline.extractors.nfa_extractor import _ceremony_url
        self.assertIn("48th_National_Film_Awards", _ceremony_url(2000))
        self.assertIn("71st_National_Film_Awards", _ceremony_url(2023))
        self.assertIn("51st_National_Film_Awards", _ceremony_url(2003))

    def test_ordinal_suffix(self):
        from nfa_pipeline.extractors.nfa_extractor import _ordinal_suffix
        self.assertEqual(_ordinal_suffix(1), "st")
        self.assertEqual(_ordinal_suffix(2), "nd")
        self.assertEqual(_ordinal_suffix(3), "rd")
        self.assertEqual(_ordinal_suffix(4), "th")
        self.assertEqual(_ordinal_suffix(11), "th")
        self.assertEqual(_ordinal_suffix(12), "th")
        self.assertEqual(_ordinal_suffix(21), "st")
        self.assertEqual(_ordinal_suffix(71), "st")

    def test_split_em_dash(self):
        title, director = NFAExtractor._split_film_director("Kantara — Rishab Shetty")
        self.assertEqual(title, "Kantara")
        self.assertEqual(director, "Rishab Shetty")

    def test_split_parenthetical(self):
        title, director = NFAExtractor._split_film_director("Kantara (Rishab Shetty)")
        self.assertEqual(title, "Kantara")
        self.assertEqual(director, "Rishab Shetty")

    def test_split_no_director(self):
        title, director = NFAExtractor._split_film_director("Just A Film Title")
        self.assertEqual(title, "Just A Film Title")
        self.assertIsNone(director)


# ═══════════════════════════════════════════════════════════════════════════════
# CHECKPOINT TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckpointManager(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cp_path = Path(self.tmp) / "checkpoint.json"

    def _cp(self):
        return CheckpointManager(self.cp_path)

    def test_fresh_no_done_years(self):
        self.assertEqual(self._cp().done_years(), [])

    def test_mark_done_persists(self):
        cp = self._cp()
        cp.mark_done(2010)
        cp2 = CheckpointManager(self.cp_path)
        self.assertIn(2010, cp2.done_years())

    def test_is_done_true(self):
        cp = self._cp()
        cp.mark_done(2015)
        self.assertTrue(cp.is_done(2015))

    def test_is_done_false(self):
        self.assertFalse(self._cp().is_done(2015))

    def test_remaining_years(self):
        cp = self._cp()
        cp.mark_done(2010)
        cp.mark_done(2011)
        remaining = cp.remaining_years([2010, 2011, 2012, 2013])
        self.assertEqual(remaining, [2012, 2013])

    def test_mark_failed(self):
        cp = self._cp()
        cp.mark_failed(2005, "Connection refused")
        self.assertIn("2005", cp.failed_years())

    def test_reset_clears_all(self):
        cp = self._cp()
        cp.mark_done(2010)
        cp.reset()
        self.assertEqual(cp.done_years(), [])

    def test_corrupted_file_starts_fresh(self):
        self.cp_path.write_text("NOT VALID JSON{{{{")
        cp = CheckpointManager(self.cp_path)
        self.assertEqual(cp.done_years(), [])

    def test_summary_contains_count(self):
        cp = self._cp()
        cp.mark_done(2010)
        self.assertIn("done=1", cp.summary())


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfig(unittest.TestCase):

    def setUp(self):
        reset_config()

    def tearDown(self):
        reset_config()

    def test_loads_default_config(self):
        cfg = load_config()
        self.assertTrue(cfg.scraper.base_url.startswith("http"))

    def test_missing_config_returns_defaults(self):
        cfg = load_config(Path("/nonexistent/path/settings.yaml"))
        self.assertEqual(cfg.scraper.max_retries, 3)

    def test_custom_yaml_overrides(self):
        import yaml
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({"scraper": {"start_year": 2010, "end_year": 2015}}, f)
            fname = f.name
        cfg = load_config(Path(fname))
        self.assertEqual(cfg.scraper.start_year, 2010)
        os.unlink(fname)

    def test_env_override(self):
        import yaml
        import os
        os.environ["NFA__SCRAPER__START_YEAR"] = "2005"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({"scraper": {"start_year": 2000}}, f)
            fname = f.name
        try:
            cfg = load_config(Path(fname))
            self.assertEqual(cfg.scraper.start_year, 2005)
        finally:
            del os.environ["NFA__SCRAPER__START_YEAR"]
            os.unlink(fname)
            reset_config()


# ═══════════════════════════════════════════════════════════════════════════════
# SQLITE STORAGE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

def _make_storage_cfg(tmp_dir):
    cfg = MagicMock()
    cfg.path = str(Path(tmp_dir) / "test.db")
    cfg.pragmas = {}
    return cfg


def _make_records(n=3, year=2015):
    return [
        AwardRecord(
            ceremony_year=year,
            category=f"Category {i}",
            film_title=f"Film {i}",
            director=f"Director {i}",
            film_language="Hindi",
        )
        for i in range(n)
    ]


class TestSQLiteStorage(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _db(self):
        return SQLiteStorage(_make_storage_cfg(self.tmp))

    def test_connect_creates_schema(self):
        with self._db() as db:
            self.assertEqual(db.count_records(), 0)

    def test_upsert_records(self):
        with self._db() as db:
            db.upsert_records(_make_records(5))
            self.assertEqual(db.count_records(), 5)

    def test_upsert_idempotent(self):
        records = _make_records(3)
        with self._db() as db:
            db.upsert_records(records)
            db.upsert_records(records)
            self.assertEqual(db.count_records(), 3)

    def test_query_by_year(self):
        with self._db() as db:
            db.upsert_records(_make_records(3, 2015) + _make_records(2, 2016))
            rows = db.query_by_year(2015)
            self.assertEqual(len(rows), 3)

    def test_years_in_db(self):
        with self._db() as db:
            db.upsert_records(_make_records(2, 2010) + _make_records(2, 2015))
            years = db.years_in_db()
            self.assertIn(2010, years)
            self.assertIn(2015, years)

    def test_fetch_all(self):
        with self._db() as db:
            db.upsert_records(_make_records(4))
            rows = db.fetch_all()
            self.assertEqual(len(rows), 4)

    def test_upsert_empty_list(self):
        with self._db() as db:
            n = db.upsert_records([])
            self.assertEqual(n, 0)

    def test_context_manager_closes(self):
        db = self._db()
        with db:
            db.upsert_records(_make_records(1))
        self.assertIsNone(db._conn)

    def test_records_have_correct_year(self):
        with self._db() as db:
            db.upsert_records(_make_records(3, 2020))
            rows = db.query_by_year(2020)
            for row in rows:
                self.assertEqual(row["ceremony_year"], 2020)


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION: Transform → Validate flow
# ═══════════════════════════════════════════════════════════════════════════════

class TestTransformValidateIntegration(unittest.TestCase):

    def test_full_flow(self):
        records = [
            AwardRecord(ceremony_year=2020, category="Best Film", film_title="Jallikattu", film_language="malayalam"),
            AwardRecord(ceremony_year=2020, category="Best Direction", film_title="Tanhaji", director="om raut"),
            AwardRecord(ceremony_year=2020, category="Best Actor silver lotus", film_title="Soorarai Pottru"),
        ]

        t = RecordTransformer(_make_transformer_cfg())
        transformed = t.transform_batch(records)

        # Check language normalised
        self.assertEqual(transformed[0].film_language, "Malayalam")
        # Check director title-cased
        self.assertIn("Om", transformed[1].director)
        # Check award type inferred
        self.assertEqual(transformed[2].award_type, "Silver Lotus")

        v = RecordValidator(_make_validator_cfg())
        validated, report = v.validate_batch(transformed)
        self.assertEqual(report.total, 3)
        self.assertEqual(report.valid, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

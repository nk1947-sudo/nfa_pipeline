#!/usr/bin/env python3
"""
nfa_pipeline/__main__.py
--------------------------
Command-line interface for the NFA data pipeline.

Usage examples
--------------
Run all years (2000-2023):
    python -m nfa_pipeline

Run specific years:
    python -m nfa_pipeline --years 2010 2015 2020

Run a year range:
    python -m nfa_pipeline --start-year 2010 --end-year 2015

Force re-scrape (ignore checkpoint):
    python -m nfa_pipeline --force

Verbose debug output:
    python -m nfa_pipeline --log-level DEBUG

Show pipeline status:
    python -m nfa_pipeline --status

Incremental update (fill null fields + scrape missing years):
    python -m nfa_pipeline --incremental

Enrichment only (no re-scraping):
    python -m nfa_pipeline --incremental --enrich-only

Target a specific scan folder:
    python -m nfa_pipeline --incremental --enrich-only --scan-id run_20260219_025541_225a97d9

List available scan folders:
    python -m nfa_pipeline --list-scans
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nfa_pipeline",
        description="National Film Awards (NFA) data scraping pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Year selection
    year_group = parser.add_argument_group("Year selection")
    year_group.add_argument(
        "--years",
        nargs="+",
        type=int,
        metavar="YEAR",
        help="Specific ceremony years to scrape (e.g. 2010 2015 2020)",
    )
    year_group.add_argument(
        "--start-year",
        type=int,
        default=None,
        metavar="YEAR",
        help="First year in range (default: config value)",
    )
    year_group.add_argument(
        "--end-year",
        type=int,
        default=None,
        metavar="YEAR",
        help="Last year in range (default: config value)",
    )

    # Pipeline options
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="FILE",
        help="Path to custom settings.yaml",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Re-scrape even if already checkpointed",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        metavar="LEVEL",
        help="Logging verbosity (default: INFO)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be scraped without actually running",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        default=False,
        help="Show checkpoint status and exit",
    )
    parser.add_argument(
        "--reset-checkpoint",
        action="store_true",
        default=False,
        help="Clear the checkpoint file (forces full re-run on next execution)",
    )

    # Incremental update flags
    parser.add_argument(
        "--incremental",
        action="store_true",
        default=False,
        help="Run incremental update: scrape missing years + fill null fields",
    )
    parser.add_argument(
        "--enrich-only",
        action="store_true",
        default=False,
        help="With --incremental: skip scraping, only fill null fields",
    )
    parser.add_argument(
        "--scan-id",
        default=None,
        metavar="SCAN",
        help=(
            "Target a specific scan folder (e.g. run_20260219_025541_225a97d9). "
            "Use with --incremental to update that scan's DB instead of the canonical one."
        ),
    )
    parser.add_argument(
        "--list-scans",
        action="store_true",
        default=False,
        help="List all available scan folders under data/processed/ and exit",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    from .config import get_config
    cfg = get_config(args.config)

    # List all scan folders and exit
    if args.list_scans:
        from .incremental import IncrementalUpdater
        processed_dir = Path(cfg.paths.processed_dir)
        scans = IncrementalUpdater.list_scans(processed_dir)
        if not scans:
            print("No scan folders found under", processed_dir)
            return 0
        print(f"\nAvailable scan folders ({len(scans)}):\n")
        for s in scans:
            db_status = "  db=YES" if s.get("has_db") else "  db=NO "
            created   = s.get("created_at", "")
            scan_name = s.get("scan_id", "?")
            print(f"  {scan_name:<45}  {created}  {db_status}")
        print()
        return 0

    # Status / reset-checkpoint
    if args.status or args.reset_checkpoint:
        from .utils.checkpoint import CheckpointManager

        cp = CheckpointManager(Path(cfg.paths.checkpoint_file))

        if args.reset_checkpoint:
            cp.reset()
            print("Checkpoint cleared.")
            return 0

        print(cp.summary())
        done = cp.done_years()
        failed = cp.failed_years()
        print(f"Done years  : {done}")
        print(f"Failed years: {list(failed.keys())}")
        return 0

    # Override log level
    from .utils.logging_setup import setup_logging
    setup_logging(
        level=args.log_level,
        log_file=Path(cfg.paths.logs_dir) / "pipeline.log",
    )

    # Incremental update mode
    if args.incremental:
        from .incremental import IncrementalUpdater
        try:
            updater = IncrementalUpdater(config_path=args.config, scan_id=args.scan_id)
            report = updater.run(enrich_only=args.enrich_only)
            print(report)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return 130
        except Exception as exc:
            print(f"FATAL: {exc}", file=sys.stderr)
            return 1
        return 0

    # Resolve years to process
    if args.years:
        years = sorted(set(args.years))
    else:
        start = args.start_year or cfg.scraper.start_year
        end = args.end_year or cfg.scraper.end_year
        if start > end:
            print(f"ERROR: --start-year {start} > --end-year {end}", file=sys.stderr)
            return 1
        years = list(range(start, end + 1))

    # Dry-run
    if args.dry_run:
        print(f"DRY RUN -- would scrape {len(years)} years: {years}")
        return 0

    # Run full pipeline
    from .pipeline import NFAPipeline

    try:
        pipeline = NFAPipeline(config_path=args.config, force_rescrape=args.force)
        run = pipeline.run(years=years)
    except KeyboardInterrupt:
        print("\nInterrupted by user -- checkpoint saved, re-run to continue.")
        return 130
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1

    return 0 if run.status in ("completed", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())

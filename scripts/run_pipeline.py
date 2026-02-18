#!/usr/bin/env python3
"""
scripts/run_pipeline.py
------------------------
Convenience wrapper for running the NFA pipeline with common options.

Usage
-----
    # Full run (2000–2023)
    python scripts/run_pipeline.py

    # Specific years
    python scripts/run_pipeline.py --years 2015 2016 2017

    # Custom year range
    python scripts/run_pipeline.py --start-year 2010 --end-year 2015

    # Force re-scrape
    python scripts/run_pipeline.py --force

    # Debug logging
    python scripts/run_pipeline.py --log-level DEBUG

    # Dry run
    python scripts/run_pipeline.py --dry-run
"""

import sys
import os

# Add project root to path when running directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nfa_pipeline.__main__ import main

if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
The whole weekly job: scrape -> classify -> data.json.

Run with no arguments and it asks which period to collect.
Pass date flags to skip the prompt (for scheduled/unattended runs):

    python run_weekly.py                              # asks
    python run_weekly.py --from 2026-08-10 --to 2026-08-16
    python run_weekly.py --from 2026-08-10 --limit 3   # cheap test
"""
import subprocess, sys
from pathlib import Path

HERE = Path(__file__).parent

SCRAPE_FLAGS  = ("--from", "--to", "--all", "--no-text")
ENRICH_FLAGS  = ("--limit", "--dry-run")


def split_args(argv):
    """Route each flag to the stage that understands it."""
    scrape, enrich, i = [], [], 0
    while i < len(argv):
        a = argv[i]
        takes_value = a in ("--from", "--to", "--limit")
        target = (scrape if a.startswith(SCRAPE_FLAGS)
                  else enrich if a.startswith(ENRICH_FLAGS)
                  else None)
        if target is None:
            print(f"Unknown option: {a}")
            print(f"Valid: {' '.join(SCRAPE_FLAGS + ENRICH_FLAGS)}")
            sys.exit(1)
        target.append(a)
        if takes_value and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            target.append(argv[i + 1])
            i += 1
        i += 1
    return scrape, enrich


def step(name, script, extra):
    print(f"\n{'='*70}\n{name}\n{'='*70}")
    r = subprocess.run([sys.executable, str(HERE / script), *extra])
    if r.returncode != 0:
        print(f"\n{script} stopped (exit {r.returncode}). data.json untouched.")
        sys.exit(r.returncode)


if __name__ == "__main__":
    s_args, e_args = split_args(sys.argv[1:])
    step("STAGE 1 of 2 - scraping 16 regulators", "scrape.py", s_args)
    step("STAGE 2 of 2 - classifying with Claude", "enrich.py", e_args)
    print(f"\n{'='*70}\nDone. data.json is ready to publish.\n{'='*70}")

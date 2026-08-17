#!/usr/bin/env python3
"""
Remove unusable records from data.json.

Drops any record with no regulation_title (the signature of a page whose body
never loaded), and backfills _iso_date from weekly_articles.json so the
newest-first sort works. Writes a .bak first.
"""
import json, shutil, sys
from pathlib import Path

DATA = Path("data.json")
ARTICLES = Path("weekly_articles.json")

if not DATA.exists():
    sys.exit("data.json not found - run this in the pipeline folder.")

records = json.loads(DATA.read_text(encoding="utf-8"))

iso = {}
if ARTICLES.exists():
    for a in json.loads(ARTICLES.read_text(encoding="utf-8")):
        if a.get("url"):
            iso[a["url"]] = a.get("date", "")

keep, dropped = [], []
for r in records:
    if r.get("regulation_title"):
        r.setdefault("_iso_date", iso.get(r.get("source_link", ""), ""))
        keep.append(r)
    else:
        dropped.append(r)

if not dropped:
    print(f"Nothing to prune - all {len(records)} records have titles.")
else:
    shutil.copy(DATA, DATA.with_suffix(".json.bak"))
    print(f"Backed up -> {DATA.with_suffix('.json.bak')}\n")
    print(f"Dropping {len(dropped)} record(s) with no title:")
    for r in dropped:
        print(f"   {r.get('record_id','?')}  {r.get('regulator','?')}  {r.get('source_link','')[:58]}")

keep.sort(key=lambda r: r.get("_iso_date") or "", reverse=True)
for n, r in enumerate(keep, 1):
    r["record_id"] = f"REG-{n:05d}"

DATA.write_text(json.dumps(keep, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\ndata.json now holds {len(keep)} records, newest first.")

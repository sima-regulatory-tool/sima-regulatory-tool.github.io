#!/usr/bin/env python3
"""
STAGE 3 — PUBLISH
Embeds data.json into the dashboard, producing a self-contained index.html.

Why embed rather than fetch: browsers block fetch() under file:// CORS rules,
so a fetching page works on GitHub Pages but breaks the moment someone opens
the local copy by double-clicking it. Embedding works in both places.

  python publish.py                       # data.json + template -> index.html
  python publish.py --check               # report only, write nothing
"""
import json, re, shutil, sys, argparse
from pathlib import Path
from datetime import datetime

DATA = Path("data.json")

# Each template produces its own page from the same data.json. Add a row here
# and it gets built too; a template that isn't present is skipped quietly, so
# you can keep only the versions you actually use.
TARGETS = [
    ("template.html",    "index.html",    "full (archive + Excel export)"),
    ("template_v2.html", "index_v2.html", "lean (no archive, copy to clipboard)"),
]

# codebook key (data.json)  ->  dashboard key (RECORDS)
KEYMAP = {
    "record_id": "Record ID",
    "jurisdiction_country": "Jurisdiction_Country",
    "jurisdiction_prov": "Jurisdiction_Prov",
    "regulator": "Regulator",
    "region": "Region",
    "market_type": "Market Type",
    "regulation_title": "Regulation Title",
    "regulation_type": "Regulation Type",
    "change_category": "Change Category",
    "topic_area": "Topic Area",
    "sub_topic": "Sub-Topic",
    "summary_short": "Summary Short",
    "summary_long": "Summary Long",
    "announcement_date": "Announcement Date",
    "effective_date": "Effective Date",
    "compliance_date": "Compliance Date",
    "comment_deadline": "Comment Deadline",
    "implementation_phase": "Implementation Phase",
    "status": "Status",
    "impact_level": "Impact Level",
    "impact_type": "Impact Type",
    "affected_functions": "Affected Functions",
    "affected_products": "Affected Products",
    "extraterritorial_impact": "Extraterritorial Impact",
    "cross_border_relevance": "Cross-Border Relevance",
    "sima_owner": "SIMA Owner",
    "sima_policy_group": "SIMA Policy Group",
    "action_required": "Action Required",
    "action_description": "Action Description",
    "internal_deadline": "Internal Deadline",
    "status_internal": "Status (Internal)",
    "source_link": "Source Link",
    "source_type": "Source Type",
    "document_type": "Document Type",
    "attachments": "Attachments",
    "notes": "Notes",
    "regulatory_theme": "Regulatory Theme",
    "policy_driver": "Policy Driver",
    "risk_category": "Risk Category",
    "innovation_type": "Innovation Type",
    "complexity_score": "Complexity Score",
    "urgency_score": "Urgency Score",
    "data_confidence": "Data Confidence",
}

DATE_KEYS = {"announcement_date", "effective_date", "compliance_date",
             "comment_deadline", "internal_deadline"}
INT_KEYS  = {"complexity_score", "urgency_score"}

MONTHS = {m: i for i, m in enumerate(
    ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"], 1)}


def to_iso(v):
    """Codebook dates are DD-MMM-YY; the dashboard's parseD() needs YYYY-MM-DD."""
    s = str(v or "").strip()
    if not s or s.lower() == "none":
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{2})", s)
    if m:
        d, mon, y = m.group(1), m.group(2).upper(), int(m.group(3))
        if mon in MONTHS:
            # Two-digit years: 00-79 -> 2000s, 80-99 -> 1900s.
            year = 2000 + y if y < 80 else 1900 + y
            return f"{year:04d}-{MONTHS[mon]:02d}-{int(d):02d}"
    return s          # unrecognised: pass through rather than silently blank it


def to_cell(key, v):
    """Arrays become '; '-joined strings, which is what the dashboard filters on."""
    if key in DATE_KEYS:
        return to_iso(v)
    if isinstance(v, (list, tuple)):
        return "; ".join(str(x).strip() for x in v if str(x).strip())
    if key in INT_KEYS:
        s = str(v or "").strip()
        return int(s) if s.isdigit() else ""
    return "" if v is None else str(v)


def convert(records):
    out, warnings = [], []
    for i, src in enumerate(records, 1):
        row = {}
        for ck, dk in KEYMAP.items():
            row[dk] = to_cell(ck, src.get(ck, ""))
        row["Record ID"] = row["Record ID"] or f"REG-{i:05d}"

        if not row["Announcement Date"]:
            iso = src.get("_iso_date", "")
            if iso:
                row["Announcement Date"] = iso
            else:
                warnings.append(f"{row['Record ID']}: no announcement date "
                                f"- it will not appear in date-filtered views")
        if not row["Regulation Title"]:
            warnings.append(f"{row['Record ID']}: no title - run prune.py")
        out.append(row)
    return out, warnings


def embed(html, rows):
    """Swap the RECORDS array in place, leaving the rest of the file untouched."""
    start = html.find("const RECORDS=[")
    if start == -1:
        sys.exit("Could not find 'const RECORDS=[' in the template.")
    end = html.find("];", start)
    if end == -1:
        sys.exit("Found 'const RECORDS=[' but no closing '];'.")

    payload = json.dumps(rows, ensure_ascii=False, indent=1)
    # A literal </script> inside the JSON would close the tag early and break
    # the page. U+2028/9 are valid JSON but illegal in JS string literals.
    payload = (payload.replace("</", "<\\/")
                      .replace("\u2028", "\\u2028")
                      .replace("\u2029", "\\u2029"))

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = (f"const RECORDS=/* {len(rows)} records embedded {stamp} "
             f"by publish.py - do not edit by hand */\n{payload}")
    # end points at the template's "];". The payload from json.dumps already
    # carries its own closing "]", so skip that bracket and keep the ";".
    return html[:start] + block + html[end + 1:]


def main():
    ap = argparse.ArgumentParser(
        description="Embed data.json into every dashboard template present.")
    ap.add_argument("--check", action="store_true",
                    help="report only, write nothing")
    ap.add_argument("--only", metavar="TEMPLATE",
                    help="build just this one template (e.g. template_v2.html)")
    args = ap.parse_args()

    if not DATA.exists():
        sys.exit(f"{DATA} not found. Run this in the pipeline folder.")

    records = json.loads(DATA.read_text(encoding="utf-8"))
    rows, warnings = convert(records)
    print(f"{len(rows)} records converted to dashboard schema.")

    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for w in warnings:
            print(f"  - {w}")

    dates = sorted(r["Announcement Date"] for r in rows if r["Announcement Date"])
    if dates:
        print(f"Date range: {dates[0]} to {dates[-1]}")

    targets = TARGETS
    if args.only:
        targets = [t for t in TARGETS if t[0] == args.only]
        if not targets:
            names = ", ".join(t[0] for t in TARGETS)
            sys.exit(f"--only {args.only} is not a known template. Known: {names}")

    present = [t for t in targets if Path(t[0]).exists()]
    missing = [t for t in targets if not Path(t[0]).exists()]

    if not present:
        sys.exit("No templates found. Expected at least one of: "
                 + ", ".join(t[0] for t in targets))

    print()
    built = []
    for tpl_name, out_name, desc in present:
        tpl, out = Path(tpl_name), Path(out_name)
        if args.check:
            # Parse the template far enough to prove the swap would work,
            # rather than reporting success without checking.
            try:
                embed(tpl.read_text(encoding="utf-8"), rows)
                print(f"  OK      {tpl_name:20} -> {out_name:16} {desc}")
            except SystemExit as e:
                print(f"  FAILED  {tpl_name:20} {e}")
            continue

        html = embed(tpl.read_text(encoding="utf-8"), rows)
        if out.exists():
            shutil.copy(out, out.with_suffix(out.suffix + ".bak"))
        out.write_text(html, encoding="utf-8")
        print(f"  wrote   {out_name:16} {len(html):>9,} bytes   {desc}")
        built.append(out_name)

    for tpl_name, out_name, desc in missing:
        print(f"  skipped {tpl_name:20} (not in this folder)")

    if args.check:
        print("\n--check: nothing written.")
    else:
        print(f"\n{len(built)} page(s) ready to commit: {', '.join(built)}")


if __name__ == "__main__":
    main()

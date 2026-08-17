#!/usr/bin/env python3
"""
STAGE 2 — CLASSIFY
Reads weekly_articles.json, sends each article to Claude with the codebook
system prompt, and merges the resulting records into data.json.

Two changes from the extract_records notebook:
  1. Writes data.json (what the site reads), not Excel. openpyxl is gone.
  2. Merges instead of overwriting — already-classified URLs are skipped,
     so re-running costs nothing and never double-charges you.
"""
import os, re, io, sys, json, argparse
from pathlib import Path
from anthropic import Anthropic
import requests
from bs4 import BeautifulSoup


SYSTEM_PROMPT = r"""
You are a regulatory-change analyst. Read ONE regulatory document and return ONE JSON record matching the codebook below.

RULES
- Use only the document + supplied metadata; never fabricate values, dates, names or URLs.
- single = one value; multi = array; NEVER put >1 value in a single field (extras -> regulatory_theme). boolean = Yes/No; date = DD-MMM-YY; integer = 1-5.
- Unknown -> "" (or "none" for dates) and lower data_confidence. 'market_type=All' excludes all other markets.
- Enforcement / alerts / sanctions are NOT rule changes: topic_area=Enforcement, regulation_type & document_type=Notice, leave change_category & implementation_phase blank, score from the FIRM's action (usually low/Monitor).
- extraterritorial_impact = the rule's own legal reach abroad; cross_border_relevance = relevance to us elsewhere. Judge separately.
- Internal Handling is human-assigned: leave blank, except status_internal="Not Started" and infer action_required.
- Return JSON only — one object keyed exactly by the field keys below. No prose, no markdown.

FIELDS — key (type, required) [allowed] : note

# Core Metadata
- record_id (system,opt) : blank; system-assigned
- jurisdiction_country (text,req) : actual country (real ISO name), not a bucket
- jurisdiction_prov (text,opt) : province/state if sub-national; else blank
- regulator (text,req) : actual issuing body by its own name (e.g. Canadian Investment Regulatory Organization (CIRO), Alberta Securities Commission (ASC), SEC, ESMA, FCA, AMF)
- region (single,req) [North America, Europe, APAC, LATAM, Global]
- market_type (multi,req) [Equity, Fixed Income, Derivatives, Commodities, Foreign Exchange (FX), Crypto / Digital Assets, Investment Funds, Structured Products, Money Market, Securitized Products, All] : 'All' is exclusive

# Regulatory Change Details
- regulation_title (text,req) : official title as published
- regulation_type (single,req) [Rule, Regulation, Guidance, Notice, Policy Statement, Legislation, Standard, Bulletin] : enforcement press release -> Notice
- change_category (single,req) [New, Amendment, Repeal, Consolidation, Consultation, Restatement, Technical Correction] : blank for enforcement / non-rulemaking
- topic_area (single,req) [Disclosure, Market Structure, Investor Protection, ESG / Sustainability, Enforcement, Digital Assets / Crypto, AML / Financial Crime, Conduct & Culture, Prudential / Capital, Operational Resilience, Registration & Licensing, Trade & Transaction Reporting, Custody & Client Assets, Other] : single primary subject; extras -> regulatory_theme
- sub_topic (single,opt) [SRO Rules, Liquidity Risk Management, Best Execution, Client Reporting, Margin Requirements, Custody, Conflicts of Interest, Trade Reporting, Disclosure Requirements, Other] : valid for chosen topic_area; blank if none
- summary_short (text,req) : one-line summary, <=280 chars
- summary_long (text,opt) : <=2000 chars

# Timeline & Status
- announcement_date (date,req)
- effective_date (date,opt) : or 'none'
- compliance_date (date,opt) : or 'none'
- comment_deadline (date,opt) : or 'none'
- implementation_phase (single,req) [Proposed, Consultation, Finalized, In Force, Phased Implementation, Repealed] : blank for non-rulemaking
- status (single,req) [Active, Pending, Superseded, Withdrawn, Closed]

# Impact Assessment
- impact_level (single,req) [High, Medium, Low] : High=mandatory/firm-wide/deadline; Medium=one function/moderate; Low=info/no change
- impact_type (multi,req) [Operational, Compliance, Strategic, Financial, Legal, Reputational, Technology / Systems, Client / Market-facing] : all that apply; >=1
- affected_functions (multi,opt) : functions touched (Trading, Ops, Legal, Compliance, Risk, IT, …); free text
- affected_products (multi,opt) : free text
- extraterritorial_impact (boolean,opt) [Yes, No] : rule's own legal reach outside its jurisdiction; silent -> No
- cross_border_relevance (boolean,opt) [Yes, No] : relevant to us elsewhere (incl. cross-regulator cooperation); NOT the rule's legal reach

# Internal Handling
- sima_owner (single,req) : human-assigned; leave blank
- sima_policy_group (single,req) [AMVI, Capital Markets, Wealth Management Dealer, Asset Management, Taxation] : only if it clearly maps; else blank
- action_required (single,req) [Yes, No, Monitor]
- action_description (text,opt) : <=500 chars
- internal_deadline (date,opt) : blank
- status_internal (single,req) [Not Started, In Progress, Under Review, Submitted, Complete, On Hold] : default Not Started

# Source & Documentation
- source_link (url,req) : from metadata; never fabricate
- source_type (single,req) [Regulator, Government, Industry Association, Law Firm, News / Media, Internal, Other]
- document_type (single,req) [Consultation, Request for Comment, Proposed Amendments, Final Rule, Guidance Note, Notice, Staff Notice, Bulletin, Other]
- attachments (list,opt) : blank
- notes (text,opt) : blank unless a note is warranted

# Analytical Tagging
- regulatory_theme (multi,opt) [Market Structure Reform, Investor Protection, ESG / Sustainability, Digital Assets, AML / Financial Crime, Operational Resilience, Conduct & Culture, Disclosure & Reporting]
- policy_driver (single,opt) [Crisis, Technology, Political, Market Event, International Harmonization, Investor Harm]
- risk_category (single,opt) [Conduct, Prudential, Market]
- innovation_type (single,opt) [AI, Crypto, Digital Assets, None]
- complexity_score (integer,opt) : 1=trivial … 3=several interacting/one function-wide … 5=sweeping multi-rulebook reform
- urgency_score (integer,opt) : deadline+action: 1=none/monitor, 3=3-6mo, 5=imminent/triggered
- data_confidence (single,opt) [High, Medium, Low] : High=verified primary; Medium=secondary/inferred; Low=unverified; drop a band if a required field was guessed
"""

# =====================================================================
# Config
# =====================================================================
IN_JSON   = "weekly_articles.json"     # from scrape.py
OUT_JSON  = "data.json"                # what the website reads
MODEL     = "claude-haiku-4-5"         # haiku = cheap; sonnet = better on edge cases
MAX_TOKENS     = 2500
DOC_CHAR_LIMIT = 40_000
MIN_TEXT_CHARS = 300      # below this, there is nothing to classify

# Pages that render their body in JavaScript return a short placeholder to
# requests. It is non-empty, so `text or summary` treats it as real content
# and you pay for an API call that can only produce an empty record.
JUNK_MARKERS = (
    "enable javascript",
    "javascript is required",
    "please enable js",
    "your browser does not support",
)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# The 43 codebook keys, in order. These are now the canonical field names —
# data.json uses these exact snake_case keys and index.html reads them.
# (The notebook mapped them to Excel labels like "Jurisdiction (Country)";
# that mapping is gone, along with the Excel dependency.)
FIELDS = [
    "record_id", "jurisdiction_country", "jurisdiction_prov", "regulator",
    "region", "market_type", "regulation_title", "regulation_type",
    "change_category", "topic_area", "sub_topic", "summary_short",
    "summary_long", "announcement_date", "effective_date", "compliance_date",
    "comment_deadline", "implementation_phase", "status", "impact_level",
    "impact_type", "affected_functions", "affected_products",
    "extraterritorial_impact", "cross_border_relevance", "sima_owner",
    "sima_policy_group", "action_required", "action_description",
    "internal_deadline", "status_internal", "source_link", "source_type",
    "document_type", "attachments", "notes", "regulatory_theme",
    "policy_driver", "risk_category", "innovation_type", "complexity_score",
    "urgency_score", "data_confidence",
]

client = Anthropic()   # reads ANTHROPIC_API_KEY from the environment


# =====================================================================
# Helpers
# =====================================================================
def parse_reply(text):
    text = re.sub(r"```json|```", "", text or "").strip()
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        raise ValueError("No JSON object in reply:\n" + text[:300])
    return json.loads(text[s:e + 1])


def fetch_text(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    if "pdf" in r.headers.get("Content-Type", "").lower() or url.lower().endswith(".pdf"):
        import pdfplumber
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            return "\n\n".join((p.extract_text() or "") for p in pdf.pages).strip()
    soup = BeautifulSoup(r.text, "html.parser")
    c = soup.select_one("main") or soup.select_one("article") or soup.body
    for t in c.select("script, style, nav, header, footer, form"):
        t.decompose()
    ps = [p.get_text(" ", strip=True) for p in c.find_all("p")]
    return "\n\n".join(p for p in ps if p) or c.get_text("\n", strip=True)


def extract(text, meta):
    """One document -> one codebook record."""
    doc = (text or "").strip()[:DOC_CHAR_LIMIT]
    known = ("Known metadata (authoritative):\n"
             f"- source_link: {meta.get('url','')}\n"
             f"- announcement_date: {meta.get('date','')}\n"
             f"- issuing body / source: {meta.get('source','')}\n\n")
    user = f"{known}Document:\n<<<\n{doc}\n>>>\n\nReturn the completed codebook record as JSON."
    resp = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    rec = parse_reply("".join(b.text for b in resp.content if b.type == "text"))
    rec["source_link"] = meta.get("url", "") or rec.get("source_link", "")
    return {k: normalize(rec.get(k, "")) for k in FIELDS}


def normalize(v):
    """Lists stay lists (the dashboard filters on them); everything else -> string."""
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    if v is None:
        return ""
    return str(v)


def usable_text(article):
    """Return the best classifiable text for an article, or None with a reason."""
    text = (article.get("text") or "").strip()
    summary = (article.get("summary") or "").strip()

    if text and any(m in text.lower() for m in JUNK_MARKERS):
        text = ""                      # JS placeholder, not content
    if len(text) < MIN_TEXT_CHARS and len(summary) > len(text):
        text = summary                 # RSS summary beats a stub

    if len(text) < MIN_TEXT_CHARS:
        return None, f"only {len(text)} chars of text"
    return text, None


def load_existing(path):
    """data.json accumulates. This is a horizon scanner, not a weekly snapshot —
    overwriting each run would throw away every prior week."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"WARNING: could not read {path} ({e}) — starting a fresh file.")
        return []


# =====================================================================
# Main
# =====================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="only process the first N articles (cost guard for testing)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be sent to the API, make no calls")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.")
        print("  Windows:  setx ANTHROPIC_API_KEY \"sk-ant-...\"  (reopen the terminal)")
        print("  Mac:      export ANTHROPIC_API_KEY=\"sk-ant-...\"  in ~/.zshrc")
        sys.exit(1)

    items = json.loads(Path(IN_JSON).read_text(encoding="utf-8"))
    existing = load_existing(OUT_JSON)
    seen = {r.get("source_link") for r in existing if r.get("source_link")}

    todo = [a for a in items if a.get("url") and a["url"] not in seen]
    skipped = len(items) - len(todo)
    if args.limit:
        todo = todo[:args.limit]

    print(f"{len(items)} scraped | {skipped} already in {OUT_JSON} | {len(todo)} to classify")
    if args.dry_run:
        for a in todo:
            print(f"  would send: {a.get('source')} {a.get('title','')[:60]}")
        return
    if not todo:
        print("Nothing new. data.json unchanged.")
        return

    new, failures = [], []
    for i, a in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {a.get('source','?')}: {a.get('title','')[:55]}")
        text, why = usable_text(a)
        if text is None and a.get("url"):
            try:
                a["text"] = fetch_text(a["url"])   # retry once, directly
                text, why = usable_text(a)
            except Exception as e:
                why = f"fetch failed: {e}"
        if text is None:
            print(f"    SKIPPED ({why}) - no API call made")
            failures.append((a, why))
            continue
        try:
            rec = extract(text, a)
            # A record with no title means the model had nothing to work with.
            # Better to skip it than to publish a blank row.
            if not rec.get("regulation_title"):
                print("    SKIPPED (model returned no title)")
                failures.append((a, "empty result"))
                continue
            rec["_iso_date"] = a.get("date", "")
            # Carry cross-posting through: this notice was also published by
            # these other regulators, which is worth knowing in the archive.
            if a.get("also_published_by"):
                rec["_also_published_by"] = sorted(set(a["also_published_by"]))
            new.append(rec)
        except Exception as e:
            print(f"    FAILED: {e}")
            failures.append((a, str(e)))

    if not new:
        print("\nNo records produced. data.json unchanged.")
        sys.exit(1 if failures else 0)

    combined = existing + new
    for n, rec in enumerate(combined, 1):          # renumber so IDs stay contiguous
        rec["record_id"] = rec.get("record_id") or f"REG-{n:05d}"

    # announcement_date is DD-MMM-YY per the codebook, which sorts
    # alphabetically ("16-AUG-26" before "02-JUL-26"). Sort on the ISO date
    # carried over from the scraper instead.
    combined.sort(key=lambda r: r.get("_iso_date") or "", reverse=True)
    Path(OUT_JSON).write_text(
        json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nAdded {len(new)} records. {OUT_JSON} now holds {len(combined)}.")
    if failures:
        print(f"\n{len(failures)} skipped:")
        for a, why in failures:
            print(f"   - {a.get('source','?')} {a.get('title','')[:45]} ({why})")


if __name__ == "__main__":
    main()

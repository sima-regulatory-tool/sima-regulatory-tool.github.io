#!/usr/bin/env python3
"""
STAGE 1 — SCRAPE
Runs all 16 regulator sources, keeps articles dated on/after the cutoff,
fetches each article's full text, and writes weekly_articles.json.

Converted from the weekly_roundup notebook. The scraper functions below are
unchanged from your notebook; only the config and the runner are new.
"""
import sys, asyncio, argparse
from datetime import datetime, timedelta

# =====================================================================
# Weekly regulatory roundup — shared config + helpers
# Covers all 13 sources built in this project.
# =====================================================================
import io, re, json, time, unicodedata
from difflib import SequenceMatcher
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup

# --- WHAT COUNTS AS "THIS WEEK" --------------------------------------
# CUTOFF is computed at run time — see cutoff_for() below.
OUTPUT_JSON = "weekly_articles.json"
DIGEST_MD   = "weekly_digest.md"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# A bare User-Agent is no longer enough for sites behind bot protection —
# CIRO started returning 403 to requests that send only a UA. These are the
# headers a real Chrome sends; supplying them makes the request look ordinary.
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-CA,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}

# Near-duplicate detection. CSA notices are republished verbatim by several
# provincial regulators, so the same alert arrives with an identical title from
# four different sites. Merging before the API call is what saves money.
#
# 0.90 is deliberately strict. Real titles in this corpus either match exactly
# (1.00) or differ meaningfully — "six-year disqualification order" vs
# "13-year disqualification orders" scores 0.71 and must stay separate. There
# is nothing in between, so the threshold has a wide safety margin.
SIMILARITY = 0.90
DATE_SLACK = 3        # days apart before two similar titles count as unrelated

RETRY_ON   = (429, 500, 502, 503, 504)
MAX_TRIES  = 3
PAUSE      = 1.0        # seconds between requests, to stay a polite client

SESSION = requests.Session()
SESSION.headers.update(BROWSER_HEADERS)


def get(url, *, referer=None, extra_headers=None, timeout=30, **kw):
    """GET with browser headers, retry-with-backoff, and a courtesy pause.

    Retries only transient conditions. A 403 or 404 is a real answer and
    retrying it just wastes time, so those raise immediately.
    """
    headers = {}
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin"
    if extra_headers:
        headers.update(extra_headers)

    last = None
    for attempt in range(1, MAX_TRIES + 1):
        try:
            r = SESSION.get(url, headers=headers, timeout=timeout, **kw)
            if r.status_code in RETRY_ON and attempt < MAX_TRIES:
                time.sleep(PAUSE * 2 ** attempt)
                continue
            r.raise_for_status()
            time.sleep(PAUSE)
            return r
        except requests.exceptions.RequestException as e:
            last = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status and status not in RETRY_ON:
                raise                       # permanent — do not retry
            if attempt == MAX_TRIES:
                raise
            time.sleep(PAUSE * 2 ** attempt)
    raise last


def post(url, *, timeout=30, **kw):
    """POST counterpart, for the sources with JSON/DataTables APIs."""
    last = None
    for attempt in range(1, MAX_TRIES + 1):
        try:
            r = SESSION.post(url, timeout=timeout, **kw)
            if r.status_code in RETRY_ON and attempt < MAX_TRIES:
                time.sleep(PAUSE * 2 ** attempt)
                continue
            r.raise_for_status()
            time.sleep(PAUSE)
            return r
        except requests.exceptions.RequestException as e:
            last = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status and status not in RETRY_ON:
                raise
            if attempt == MAX_TRIES:
                raise
            time.sleep(PAUSE * 2 ** attempt)
    raise last


# Pages that render their body in JavaScript return a short stub to requests.
JS_STUB_MARKERS = ("enable javascript", "javascript is required",
                   "please enable js", "you need to enable")
# SEC asks bots to identify themselves — put a real contact email here:
SEC_UA = "horizon-scan/1.0 (your-email@example.com)"


def to_iso(raw):
    """Convert any scraper's date string to 'YYYY-MM-DD' ('' if unknown).
    Handles ISO, YYYY.MM.DD, M/D/YYYY, 'Month D, YYYY', 'D Month YYYY',
    'Jan. 9, 2026', '03-Dec-15', and messy feed strings like
    'Monday, June 22, 2026 - 11:44'."""
    if not raw:
        return ""
    s = str(raw).strip()
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)              # ISO
    if m:
        return m.group(0)
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", s)        # NL 2025.11.05
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    c = re.sub(r"\.", " ", s)                            # "May.24, 2023" -> "May 24, 2023"
    c = re.sub(r"\bSept\b", "Sep", c)                    # MB uses "Sept"
    c = re.sub(r"\s+", " ", c).strip()
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y",
                "%d %B %Y", "%d %b %Y", "%d-%b-%y", "%b %d %Y"):
        try:
            return datetime.strptime(c, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = re.search(r"([A-Za-z]{3,9}\.?\s+\d{1,2},\s*\d{4})", s)   # "...June 22, 2026..."
    if m:
        cc = re.sub(r"\.", " ", m.group(1))
        cc = re.sub(r"\bSept\b", "Sep", cc)
        cc = re.sub(r"\s+", " ", cc).strip()
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(cc, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    m = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", s)       # "3 Jun 2026"
    if m:
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(m.group(1), fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return ""


def rss_to_iso(raw):
    """RFC-822 RSS pubDate -> ISO (used by the OSC feed)."""
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except Exception:
        return to_iso(raw)


def fetch_text(url, referer=None):
    """Download an article (HTML or PDF) and return its body text."""
    r = get(url, referer=referer, timeout=60)
    ctype = r.headers.get("Content-Type", "").lower()
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        import pdfplumber
        parts = []
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            for pg in pdf.pages:
                parts.append(pg.extract_text() or "")
        return "\n\n".join(parts).strip()
    soup = BeautifulSoup(r.text, "html.parser")
    container = soup.select_one("main") or soup.select_one("article") or soup.body
    for t in container.select("script, style, nav, header, footer, form"):
        t.decompose()
    ps = [p.get_text(" ", strip=True) for p in container.find_all("p")]
    body = "\n\n".join(p for p in ps if p) or container.get_text("\n", strip=True)
    if body and any(m in body.lower() for m in JS_STUB_MARKERS) and len(body) < 400:
        return ""      # JS placeholder, not content - treat as no text at all
    return body

def last_monday(offset_weeks=0):
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    return monday - timedelta(weeks=offset_weeks)


def valid_date(s):
    """Accept YYYY-MM-DD, reject anything else with a useful message."""
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def ask_for_window():
    """Interactive date-window picker.

    The window used to be a hard-coded constant, then a rule based on which
    weekday you happened to run on. Both quietly dropped days. Asking is the
    only version where the person running it knows exactly what was covered.
    """
    this_mon = last_monday(0)
    prev_mon = last_monday(1)
    prev_sun = this_mon - timedelta(days=1)

    print("\nWhich period do you want to collect?\n")
    print(f"  1. Last full week      {prev_mon:%Y-%m-%d} to {prev_sun:%Y-%m-%d}  (Mon-Sun)")
    print(f"  2. This week so far    {this_mon:%Y-%m-%d} to today")
    print(f"  3. Last 14 days        {datetime.now() - timedelta(days=14):%Y-%m-%d} to today")
    print( "  4. Enter dates myself")
    print( "  5. Everything the sites still list  (no date filter)\n")

    while True:
        c = input("Choice [1-5]: ").strip()

        if c == "1":
            return f"{prev_mon:%Y-%m-%d}", f"{prev_sun:%Y-%m-%d}"
        if c == "2":
            return f"{this_mon:%Y-%m-%d}", None
        if c == "3":
            return f"{datetime.now() - timedelta(days=14):%Y-%m-%d}", None
        if c == "5":
            print("  (no date filter — this can be thousands of articles)")
            return None, None
        if c == "4":
            while True:
                start = valid_date(input("  Start date (YYYY-MM-DD): "))
                if start:
                    break
                print("  Not a valid date. Use YYYY-MM-DD, e.g. 2026-08-10.")
            while True:
                raw_end = input("  End date (YYYY-MM-DD, or blank for today): ").strip()
                if not raw_end:
                    return start, None
                end = valid_date(raw_end)
                if not end:
                    print("  Not a valid date. Use YYYY-MM-DD, e.g. 2026-08-16.")
                elif end < start:
                    print(f"  End date is before {start}.")
                else:
                    return start, end

        print("  Enter a number from 1 to 5.")


# =====================================================================
# Synchronous scrapers — one function per source.
# Each returns a list of {source, date(ISO), title, url, extra...}, newest first.
# Each is wrapped in try/except at run time, so one broken site won't stop the rest.
# =====================================================================

# ---- BCSC (British Columbia) — DataTables JSON API -------------------
def fetch_bcsc(limit=100):
    API  = "https://gateway.bcsc.bc.ca/api/SearchTable"
    BASE = "https://www.bcsc.bc.ca"
    hdr = {"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
           "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
           "Origin": BASE, "Referer": BASE + "/about/media-room/news-releases",
           "Accept": "application/json, text/javascript, */*; q=0.01"}
    payload = {
        "draw": 1, "start": 0, "length": limit,
        "search[value]": "", "search[regex]": "false",
        "order[0][column]": 0, "order[0][dir]": "desc",
        "IndexName": "sitecore_pub_index_MainAlias",
        "FilterTags[]": "{307C9292-1099-4C8C-A64F-3DD573719652}",
        "TemplateIds[]": "{A137729F-EF76-4867-A8DB-988BD008387F}",
        "Facets[0][FieldName]": "computeditemdateyearfield_s",
        "Facets[0][SortOrder]": "desc", "Facets[0][DisplayCount]": 100,
        "Facets[0][SelectedValues]": "", "Facets[0][FieldType]": "String",
        "Facets[0][UsesAndOperator]": "false",
    }
    for i, col in enumerate(["newsReleaseNumber", "date", "title"]):
        payload[f"columns[{i}][data]"] = col
        payload[f"columns[{i}][name]"] = ""
        payload[f"columns[{i}][searchable]"] = "true"
        payload[f"columns[{i}][orderable]"] = "true"
        payload[f"columns[{i}][search][value]"] = ""
        payload[f"columns[{i}][search][regex]"] = "false"
    r = post(API, data=payload, headers=hdr)
    r.raise_for_status()
    out = []
    for row in r.json().get("data", []):
        url = (row.get("url") or "").strip()
        if url and not url.startswith("http"):
            url = BASE + ("" if url.startswith("/") else "/") + url
        out.append({"source": "BCSC", "date": to_iso(row.get("date", "")),
                    "title": (row.get("title") or "").strip(), "url": url})
    return out


# ---- CIRO — server-rendered listing --------------------------------
def fetch_ciro(max_pages=1):
    BASE, LIST = "https://www.ciro.ca", "https://www.ciro.ca/newsroom/news-releases"
    date_re = re.compile(r'(\d{2}/\d{2}/\d{2})\s*$')
    out, seen = [], set()
    for page in range(max_pages):
        r = get(f"{LIST}?page={page}", timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        added = 0
        for a in soup.find_all("a", href=True):
            if "/newsroom/publications/" not in a["href"]:
                continue
            text = a.get_text(" ", strip=True)
            m = date_re.search(text)
            if not m:
                continue
            url = urljoin(BASE, a["href"])
            if url in seen:
                continue
            seen.add(url)
            try:
                d = datetime.strptime(m.group(1), "%m/%d/%y").strftime("%Y-%m-%d")
            except ValueError:
                d = ""
            out.append({"source": "CIRO", "date": d,
                        "title": text[:m.start()].strip(), "url": url})
            added += 1
        if added == 0:
            break
    return out


# ---- ASIC (Australia) — per-year table -----------------------------
def fetch_asic(year=2026):
    BASE = "https://www.asic.gov.au"
    URL = (f"{BASE}/regulatory-resources/find-a-document/regulatory-document-updates/"
           f"regulatory-tracker/regulatory-tracker-{year}/")
    date_re = re.compile(r'(\d{1,2})/(\d{1,2})/(\d{4})')

    def links_in(cell):
        res = []
        if cell:
            for a in cell.find_all("a", href=True):
                t = a.get_text(" ", strip=True)
                if t:
                    res.append({"title": t, "url": urljoin(BASE, a["href"])})
        return res

    r = get(URL, timeout=30)
    r.raise_for_status()
    table = BeautifulSoup(r.text, "html.parser").find("table")
    out = []
    if not table:
        return out
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 4:
            continue
        m = date_re.search(cells[0].get_text(" ", strip=True))
        if not m:
            continue
        d, mth, y = map(int, m.groups())
        try:
            iso = datetime(y, mth, d).strftime("%Y-%m-%d")
        except ValueError:
            continue
        docs = links_in(cells[2])
        primary = docs[0] if docs else {"title": cells[2].get_text(" ", strip=True), "url": ""}
        out.append({"source": "ASIC", "date": iso,
                    "title": primary["title"], "url": primary["url"],
                    "type": cells[5].get_text(" ", strip=True) if len(cells) > 5 else "",
                    "description": cells[3].get_text(" ", strip=True)})
    return out


# ---- CRA (Canada Revenue Agency) — canada.ca news API ---------------
def fetch_cra(pick=50, start_date="2026-01-01"):
    ATOM = "{http://www.w3.org/2005/Atom}"
    import xml.etree.ElementTree as ET
    url = ("https://api.io.canada.ca/io-server/gc/news/en/v2"
           "?dept=revenueagency&type=newsreleases&sort=publishedDate&orderBy=desc"
           f"&pick={pick}&format=atom&atomtitle=CRA&publishedDate%3E={start_date}")
    r = get(url, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for e in root.findall(f"{ATOM}entry"):
        title = (e.findtext(f"{ATOM}title") or "").strip()
        link = e.find(f"{ATOM}link")
        url_ = link.get("href") if link is not None else ""
        pub = (e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated") or "").strip()
        if title and url_:
            out.append({"source": "CRA", "date": to_iso(pub),
                        "title": title, "url": url_,
                        "summary": (e.findtext(f"{ATOM}summary") or "").strip()})
    return out


# ---- Newfoundland & Labrador — securities notices ------------------
def fetch_nl():
    URL, BASE = "https://www.gov.nl.ca/gs/securities/notices/", "https://www.gov.nl.ca"
    keys = ("news releases", "notice of publications", "staff notices")
    date_re = re.compile(r'^(\d{4}\.\d{2}\.\d{2})')
    r = get(URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out, section, in_target = [], None, False
    for el in soup.find_all(["h2", "li"]):
        if el.name == "h2":
            section = el.get_text(" ", strip=True)
            in_target = any(k in section.lower() for k in keys)
            continue
        if not in_target:
            continue
        a = el.find("a", href=True)
        if not a:
            continue
        text = el.get_text(" ", strip=True)
        dm = date_re.match(text)
        out.append({"source": "NL", "date": to_iso(dm.group(1)) if dm else "",
                    "title": a.get_text(" ", strip=True),
                    "url": urljoin(BASE, a["href"]), "section": section})
    return out


# ---- Saskatchewan FCAA — news releases -----------------------------
def fetch_fcaa():
    URL = "https://fcaa.gov.sk.ca/whats-new/fcaa-news-releases"
    date_re = re.compile(r'(January|February|March|April|May|June|July|August|'
                         r'September|October|November|December)\s+\d{1,2},\s+\d{4}')
    r = get(URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/news-and-media/" not in href:
            continue
        title = a.get_text(" ", strip=True)
        if not title or href in seen:
            continue
        seen.add(href)
        row = a.find_parent("tr") or a.parent
        m = date_re.search(row.get_text(" ", strip=True) if row else "")
        out.append({"source": "FCAA", "date": to_iso(m.group(0)) if m else "",
                    "title": title, "url": href})
    return out


# ---- ESMA (EU) — server-rendered news listing ----------------------
def fetch_esma(max_pages=2):
    BASE, URL = "https://www.esma.europa.eu", "https://www.esma.europa.eu/press-news/esma-news"
    date_re = re.compile(r'(\d{2})/(\d{2})/(\d{4})')
    out, seen = [], set()
    for page in range(max_pages):
        page_url = URL if page == 0 else f"{URL}?page={page}"
        r = get(page_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        added = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/press-news/esma-news/" not in href:
                continue
            slug = href.split("/press-news/esma-news/")[-1].strip("/")
            if not slug:
                continue
            url = urljoin(BASE, href)
            if url in seen:
                continue
            text = a.get_text(" ", strip=True)
            m = date_re.search(text)
            if not m:
                continue
            d, mth, y = m.groups()
            try:
                iso = datetime(int(y), int(mth), int(d)).strftime("%Y-%m-%d")
            except ValueError:
                iso = ""
            seen.add(url)
            out.append({"source": "ESMA", "date": iso,
                        "title": text[:m.start()].strip(), "url": url,
                        "summary": text[m.end():].strip()})
            added += 1
        if added == 0:
            break
    return out

# =====================================================================
# The other 9 sources, using YOUR actual notebook code (normalized to the
# same {source, date(ISO), title, url, ...} shape, newest-first):
#   MB, FCNB, NSSC, NU, OSC, FCA (UK), SEC (US), SFC (Hong Kong)
# (ASC is in the next cell — it's the only async one.)
# =====================================================================

# ---- Manitoba Securities Commission --------------------------------
def fetch_mb():
    URL, BASE = "https://mbsecurities.ca/news/", "https://mbsecurities.ca"
    date_re = re.compile(r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)'
                         r'[a-z]*\.?\s*\d{1,2},\s*\d{4}')
    r = get(URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/news/current/" not in href or href.rstrip("/").endswith("index.html"):
            continue
        title = a.get_text(strip=True)
        url = urljoin(BASE, href)
        if not title or url in seen:
            continue
        seen.add(url)
        container = a.find_parent(["li", "tr"])
        ctext = container.get_text(" ", strip=True) if container else title
        m = date_re.search(ctext)
        out.append({"source": "MB", "date": to_iso(m.group(0)) if m else "",
                    "title": title, "url": url,
                    "type": "CSA" if (container and container.name == "tr") else "Manitoba"})
    return out


# ---- FCNB (New Brunswick) ------------------------------------------
def fetch_fcnb(max_pages=10):
    BASE, URL = "https://fcnb.ca", "https://fcnb.ca/en/news-alerts"
    date_re = re.compile(r'^\d{1,2}\s+[A-Za-z]+\.?\s+\d{4}$')   # "20 May 2026"
    items = {}
    for page in range(max_pages):
        page_url = URL if page == 0 else f"{URL}?page={page}"
        r = get(page_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        added = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/en/news-alerts/" not in href:
                continue
            if not href.split("/en/news-alerts/")[-1].strip("/"):
                continue
            url = urljoin(BASE, href)
            if url not in items:
                items[url] = {"source": "FCNB", "date": "", "title": "", "url": url, "type": ""}
                added += 1
            rec = items[url]
            img = a.find("img")
            if img and img.get("alt") and not rec["type"]:
                rec["type"] = img["alt"].strip()
            text = a.get_text(strip=True)
            if text:
                if date_re.match(text):
                    rec["date"] = to_iso(text)
                elif len(text) > len(rec["title"]):
                    rec["title"] = text
        if added == 0:
            break
    return list(items.values())


# ---- Nova Scotia Securities Commission (PDF releases) --------------
def fetch_nssc(max_pages=2):        # page 0-1 covers the current week; raise for history
    URL, BASE = "https://nssc.novascotia.ca/media-releases", "https://nssc.novascotia.ca"
    date_re = re.compile(r'(January|February|March|April|May|June|July|August|'
                         r'September|October|November|December)\s+\d{1,2},\s+\d{4}')
    out, seen = [], set()
    for page in range(max_pages):
        page_url = URL if page == 0 else f"{URL}?page={page}"
        r = get(page_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        added = 0
        for a in soup.find_all("a", href=True):
            if not a["href"].lower().endswith(".pdf"):
                continue
            url = urljoin(BASE, a["href"])
            title = a.get_text(" ", strip=True)
            if not title or url in seen:
                continue
            row = a.find_parent("tr") or a.parent
            m = date_re.search(row.get_text(" ", strip=True) if row else "")
            seen.add(url)
            out.append({"source": "NSSC", "date": to_iso(m.group(0)) if m else "",
                        "title": title, "url": url})
            added += 1
        if added == 0:
            break
    return out


# ---- Nunavut Securities Office (static .shtml, PDFs; stale ~2017) ---
def fetch_nu():
    URL, BASE = "https://nunavutlegalregistries.ca/sr_news_en.shtml", "https://nunavutlegalregistries.ca"
    date_re = re.compile(r'\b(\d{2}-[A-Za-z]{3}-\d{2})\b')   # 03-Dec-15
    ref_re = re.compile(r'^(\d{2,3}-\d{3})')
    r = get(URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        if not a["href"].lower().endswith(".pdf"):
            continue
        url = urljoin(BASE, a["href"])
        title = a.get_text(" ", strip=True)
        if not title or url in seen:
            continue
        li = a.find_parent("li") or a.parent
        m = date_re.search(li.get_text(" ", strip=True) if li else "")
        ref = ""
        prev = li.find_previous_sibling("li") if li else None
        if prev:
            rm = ref_re.match(prev.get_text(" ", strip=True))
            if rm:
                ref = rm.group(1)
        seen.add(url)
        out.append({"source": "NU", "date": to_iso(m.group(1)) if m else "",
                    "title": title, "url": url, "ref": ref})
    out.sort(key=lambda a: a["date"], reverse=True)
    return out


# ---- OSC (Ontario) — official "OSC Headlines" RSS feed -------------
def fetch_osc():
    FEED = "https://feeds.feedburner.com/rss_osc_headlines_en"
    r = get(FEED, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        summary = (item.findtext("description") or "").strip()
        if title and link:
            out.append({"source": "OSC", "date": rss_to_iso(pub),
                        "title": title, "url": link, "summary": summary})
    return out


# ---- FCA (UK) — news RSS feed --------------------------------------
def fetch_fca():
    FEED = "https://www.fca.org.uk/news/rss.xml"
    r = get(FEED, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        summary = (item.findtext("description") or "").strip()
        category = (item.findtext("category") or "").strip()
        if title and link:                       # FCA date isn't RFC-822; to_iso handles it
            out.append({"source": "FCA", "date": rss_to_iso(pub),
                        "title": title, "url": link,
                        "summary": summary, "category": category})
    return out


# ---- SEC (US) — press releases (server-rendered list) --------------
def fetch_sec(max_pages=2):
    BASE, URL = "https://www.sec.gov", "https://www.sec.gov/newsroom/press-releases"
    date_re = re.compile(r'(January|February|March|April|May|June|July|August|'
                         r'September|October|November|December)\s+\d{1,2},\s+\d{4}')
    ref_re = re.compile(r'\b(\d{4}-\d{1,3})\b')
    out, seen = [], set()
    for page in range(max_pages):
        page_url = URL if page == 0 else f"{URL}?page={page}"
        r = get(page_url, extra_headers={"User-Agent": SEC_UA}, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        added = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/newsroom/press-releases/" not in href or "?" in href:
                continue
            if not href.split("/newsroom/press-releases/")[-1].strip("/"):
                continue
            url = urljoin(BASE, href)
            title = a.get_text(" ", strip=True)
            if not title or url in seen:
                continue
            seen.add(url)
            row = a.find_parent("tr") or a.parent
            rowtext = row.get_text(" ", strip=True) if row else ""
            dm, rm = date_re.search(rowtext), ref_re.search(url)
            out.append({"source": "SEC", "date": to_iso(dm.group(0)) if dm else "",
                        "title": title, "url": url, "ref": rm.group(1) if rm else ""})
            added += 1
        if added == 0:
            break
    return out


# ---- SFC (Hong Kong) — Press-releases RSS feed ---------------------
def fetch_sfc():
    FEED = "https://www.sfc.hk/en/RSS-Feeds/Press-releases"
    r = get(FEED, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        summary = (item.findtext("description") or "").strip()
        if title and link:
            out.append({"source": "SFC", "date": rss_to_iso(pub),
                        "title": title, "url": link, "summary": summary})
    return out

# =====================================================================
# ASC (Alberta) — Coveo listing is JS-rendered, so this one needs a
# headless browser (Playwright). It's async; in Jupyter call it with `await`.
#   Setup once:  pip install playwright  &&  playwright install chromium
# =====================================================================
async def fetch_asc(max_pages=2):
    from playwright.async_api import async_playwright
    BASE = "https://www.asc.ca"
    PAGE_URL = (BASE + "/en/news-and-publications/news-releases"
                "#sort=%40z95xnewspublishdate%20descending")
    SEL = "div.CoveoResult"

    def parse_cards(html):
        soup = BeautifulSoup(html, "html.parser")
        rows = []
        for card in soup.select(SEL):
            link = card.select_one("a.CoveoResultLink")
            if not link:
                continue
            title = link.get_text(strip=True)
            url = (link.get("href") or "").strip()
            if not title or not url:
                continue
            if not url.startswith("http"):
                url = urljoin(BASE, url)
            dc = card.select_one(".coveoforsitecore-time-cell")
            tc = card.select_one(".newsreleasetype")
            rows.append({"source": "ASC",
                         "date": to_iso(dc.get_text(strip=True) if dc else ""),
                         "title": title, "url": url,
                         "type": tc.get_text(strip=True) if tc else ""})
        return rows

    out, seen = [], set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_selector(SEL, timeout=30_000)
        for _ in range(max_pages):
            await page.wait_for_selector(SEL, timeout=30_000)
            for row in parse_cards(await page.content()):
                if row["url"] in seen:
                    continue
                seen.add(row["url"])
                out.append(row)
            nxt = (await page.query_selector(".coveo-pager-next .coveo-accessible-button")
                   or await page.query_selector(".coveo-pager-next"))
            if not nxt:
                break
            first = await page.query_selector("a.CoveoResultLink")
            href_before = await first.get_attribute("href") if first else None
            await nxt.click()
            try:
                await page.wait_for_function(
                    """(prev) => { const a = document.querySelector('a.CoveoResultLink');
                                   return a && a.getAttribute('href') !== prev; }""",
                    arg=href_before, timeout=15_000)
            except Exception:
                break
        await browser.close()
    return out

def norm_title(t):
    """Lowercase, de-accent, strip punctuation — so 'Privée (UBP SA)' and
    'Privee, UBP SA' compare equal."""
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def days_apart(d1, d2):
    try:
        a = datetime.strptime(d1, "%Y-%m-%d")
        b = datetime.strptime(d2, "%Y-%m-%d")
        return abs((a - b).days)
    except (ValueError, TypeError):
        return 999


def dedupe(items, similarity=SIMILARITY):
    """Collapse the same document arriving from several regulators.

    Two passes: exact URL, then near-identical title within DATE_SLACK days.
    The surviving record keeps a list of the other publishers, so nothing is
    lost — you can still see that four provinces carried the notice.
    """
    def url_key(u):
        return (u or "").split("#")[0].rstrip("/").lower()

    unique, by_url, dropped = [], {}, []

    for a in items:
        k = url_key(a.get("url"))
        if not k:
            continue
        if k in by_url:
            keeper = by_url[k]
            keeper.setdefault("also_published_by", []).append(a.get("source"))
            dropped.append((a.get("source"), keeper.get("source"),
                            a.get("title", "")[:46], "same URL"))
            continue
        by_url[k] = a
        unique.append(a)

    kept, index = [], []          # index holds (normalised title, record)
    for a in unique:
        nt = norm_title(a.get("title"))
        match = None
        if nt:
            for nt2, b in index:
                if days_apart(a.get("date"), b.get("date")) > DATE_SLACK:
                    continue
                if nt == nt2 or SequenceMatcher(None, nt, nt2).ratio() >= similarity:
                    match = b
                    break
        if match is not None:
            match.setdefault("also_published_by", []).append(a.get("source"))
            dropped.append((a.get("source"), match.get("source"),
                            a.get("title", "")[:46], "same title"))
            continue
        index.append((nt, a))
        kept.append(a)

    return kept, dropped


# =====================================================================
# RUNNER — replaces the notebook's top-level `await` cell.
# =====================================================================
SYNC_SOURCES = [
    ("BCSC", fetch_bcsc), ("CIRO", fetch_ciro), ("ASIC", fetch_asic),
    ("CRA",  fetch_cra),  ("NL",   fetch_nl),   ("FCAA", fetch_fcaa),
    ("ESMA", fetch_esma), ("MB",   fetch_mb),   ("FCNB", fetch_fcnb),
    ("NSSC", fetch_nssc), ("NU",   fetch_nu),   ("OSC",  fetch_osc),
    ("FCA",  fetch_fca),  ("SEC",  fetch_sec),  ("SFC",  fetch_sfc),
]


async def scrape_all(start, end, with_text=True):
    raw, failed = {}, []
    window = ("no date filter" if not start
              else f"{start} to {end}" if end
              else f"on/after {start}")
    print(f"\nScraping 16 sources, window = {window}\n")

    for name, fn in SYNC_SOURCES:
        t0 = time.time()
        try:
            raw[name] = fn()
            print(f"  {name:5} {len(raw[name]):>4} listed   {time.time()-t0:4.1f}s")
        except Exception as e:
            raw[name] = []
            failed.append(name)
            print(f"  {name:5}  FAILED: {e}")

    try:
        raw["ASC"] = await fetch_asc(max_pages=2)
        print(f"  {'ASC':5} {len(raw['ASC']):>4} listed")
    except Exception as e:
        raw["ASC"] = []
        failed.append("ASC")
        print(f"  {'ASC':5}  FAILED: {e}")
        print("         (ASC needs Playwright: playwright install chromium)")

    all_items = [i for items in raw.values() for i in items]

    unique, dupes = dedupe(all_items, SIMILARITY)

    kept = [a for a in unique if a.get("date")]
    if start:
        kept = [a for a in kept if a["date"] >= start]
    if end:
        kept = [a for a in kept if a["date"] <= end]
    kept.sort(key=lambda a: a["date"], reverse=True)

    undated = sum(1 for a in all_items if not a.get("date"))

    print(f"\n{'='*70}")
    print(f"{len(kept)} articles in window  "
          f"(of {len(all_items)} listed, {16-len(failed)}/16 sources up)")
    if dupes:
        print(f"{len(dupes)} cross-posted duplicate(s) merged before any API "
              f"call — each one is a classification you don't pay for:")
        for src_name, kept_by, title, why in dupes[:8]:
            print(f"   {src_name:5} -> kept {kept_by:5} ({why})  {title}")
        if len(dupes) > 8:
            print(f"   ... and {len(dupes)-8} more")
    if undated:
        print(f"{undated} listed items had no parseable date and were dropped.")
    print(f"{'='*70}\n")

    by_src = {}
    for a in kept:
        by_src.setdefault(a["source"], []).append(a)
    for s in sorted(by_src):
        print(f"{s}  ({len(by_src[s])})")
        for a in by_src[s]:
            print(f"   {a['date']}  {a['title'][:66]}")
        print()

    if len(failed) > 8:
        print(f"ABORT: {len(failed)} of 16 sources failed. Check your connection.")
        sys.exit(1)

    if not kept:
        print("Nothing in that window. weekly_articles.json not written.")
        sys.exit(0)

    if with_text:
        print("Fetching full text ...")
        for i, a in enumerate(kept, 1):
            print(f"  [{i}/{len(kept)}] {a['source']}: {a['title'][:55]}")
            try:
                a["text"] = fetch_text(a["url"], referer=a["url"])
            except Exception as e:
                a["text"] = ""
                print(f"        (no text: {e})")
            else:
                if not a["text"]:
                    print("        (no text: page renders its body in JavaScript)")

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(kept, f, indent=2, ensure_ascii=False)
    print(f"\nSaved {len(kept)} articles -> {OUTPUT_JSON}")
    return kept


def main():
    ap = argparse.ArgumentParser(
        description="Scrape 16 regulators. With no date flags, it asks you.")
    ap.add_argument("--from", dest="start", metavar="YYYY-MM-DD",
                    help="collect articles dated on/after this")
    ap.add_argument("--to", dest="end", metavar="YYYY-MM-DD",
                    help="collect articles dated on/before this")
    ap.add_argument("--all", action="store_true", help="no date filter")
    ap.add_argument("--similarity", type=float, default=SIMILARITY,
                    metavar="0.90",
                    help="title-match threshold for merging duplicates; "
                         "1.0 means exact titles only")
    ap.add_argument("--no-text", action="store_true",
                    help="skip full-text fetch (enrich.py needs the text)")
    args = ap.parse_args()

    if args.all:
        start, end = None, None
    elif args.start or args.end:
        start, end = args.start, args.end
        for label, v in (("--from", start), ("--to", end)):
            if v and not valid_date(v):
                print(f"{label} must be YYYY-MM-DD, e.g. 2026-08-10.")
                sys.exit(1)
    else:
        start, end = ask_for_window()      # nothing specified -> ask

    globals()["SIMILARITY"] = args.similarity
    asyncio.run(scrape_all(start, end, with_text=not args.no_text))


if __name__ == "__main__":
    main()

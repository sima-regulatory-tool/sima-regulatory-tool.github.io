# SIMA Regulatory Horizon Scanner — how it works

## The shape of it

Four stages. Each writes a file the next one reads, so you can stop and
inspect between any two, and re-run a stage without redoing the earlier ones.

```
  16 regulator websites
          |
          |   scrape.py          asks which date window
          v
  weekly_articles.json           raw articles + full text
          |
          |   enrich.py          one Claude call per article
          v
  data.json                      43-field codebook records (accumulates)
          |
          |   publish.py         embeds the data into the page
          v
  index.html                     self-contained dashboard
          |
          |   git push
          v
  live website
```

`run_weekly.py` runs the first two. `update.sh` runs everything including the
push.

---

## Stage 1 — `scrape.py`

Visits 16 regulators: BCSC, CIRO, ASC, MB, FCAA, FCNB, NSSC, NU, NL, OSC, CRA
(Canada), SEC (US), FCA (UK), ESMA (EU), ASIC (Australia), SFC (Hong Kong).

Each site is scraped differently — some have JSON APIs, some RSS feeds, some
need HTML parsing, and ASC needs a headless browser because its listing is
JavaScript-rendered. Every source is wrapped in its own `try/except`, so one
site being down or redesigned never stops the others.

It asks which period you want. Option 1 is the last full Mon–Sun week.

**Why the per-source counts look huge.** `MB 224 listed` means Manitoba's news
page holds 224 releases in total, going back years. The date filter runs after
all 16 have reported. The number that matters is the one in the summary box:
`10 articles in window`.

Then it de-duplicates by URL. CSA notices get republished by several
provincial regulators, so the same document arrives from MB, FCNB and NSSC —
without this you would classify it three times and pay three times.

Finally it downloads each surviving article's full text. Some pages render
their body in JavaScript and return only a placeholder; those are recorded as
having no text rather than as content.

Output: `weekly_articles.json`

---

## Stage 2 — `enrich.py`

Sends each article to Claude with your codebook prompt, and gets back one
43-field record: jurisdiction, regulator, topic area, impact level, dates,
action required, and so on.

Three things protect you here:

- **Articles with too little text are skipped before any API call is made.**
  A JavaScript placeholder is 46 characters — enough to look like content to
  a naive check, never enough to classify.
- **Already-classified URLs are skipped.** `data.json` accumulates, and the
  merge is keyed on `source_link`, so re-running costs nothing.
- **Records that come back with no title are rejected** rather than written as
  blank rows.

`--limit 3` classifies only three articles. `--dry-run` makes no calls at all.

Output: `data.json` (grows every week; this is your archive)

---

## Stage 3 — `publish.py`

Reads `template.html`, swaps in the data, writes `index.html`. The template is
never modified — `index.html` is disposable and regenerable.

It translates between two schemas, because your codebook and the dashboard
were built separately:

| `data.json` | `index.html` |
|---|---|
| `regulation_title` | `Regulation Title` |
| `jurisdiction_country` | `Jurisdiction_Country` |
| `["Legal","Compliance"]` | `"Legal; Compliance"` |
| `14-AUG-26` | `2026-08-14` |

That last row matters most: `new Date("14-AUG-26")` is `Invalid Date` in
JavaScript, so mismatched dates would silently empty the dashboard.

**Why embed instead of fetching a JSON file?** Browsers block `fetch()` under
`file://` CORS rules. A fetching page works on GitHub Pages but breaks when
someone double-clicks their local copy. Embedding works in both.

`--check` runs the conversion and reports without writing anything.

Output: `index.html`

---

## Stage 4 — publish

`git add index.html data.json && git commit && git push`. GitHub Pages
redeploys within about a minute. `update.sh` does this and stops before
committing if nothing changed.

---

## What to push

Commit:

```
index.html          the site
data.json           the archive — this is the thing you cannot regenerate
template.html       the dashboard design
scrape.py enrich.py publish.py run_weekly.py prune.py
requirements.txt update.sh
```

Do **not** commit:

```
weekly_articles.json    regenerable, and large
*.bak                   local safety copies
.env                    should not exist — the key belongs in the environment
```

A `.gitignore` with those three lines is in the repo.

`data.json` is the one irreplaceable file. Every week you push is a commit, so
the history is your backup — if the updater PC dies, a fresh clone has
everything.

---

## The weekly routine

```
./update.sh
```

Pulls, asks which week, scrapes, classifies, embeds, pushes. It stops at the
first failure and leaves the live site untouched, so a bad run costs you
freshness, never correctness.

To do it by hand:

```
python run_weekly.py          # stages 1 and 2
python publish.py             # stage 3
open index.html               # check it before pushing
git add -A && git commit -m "Weekly update" && git push
```

---

## Known gaps

- **SFC (Hong Kong)** renders article bodies in JavaScript. It is scraped and
  listed but produces no classifiable text. It would need Playwright, like
  ASC. Given the Canada-plus-major-markets remit, dropping it may be the
  better trade.
- **CIRO** returned 403 to the old scraper. Full browser headers have been
  added, which usually resolves it — unverified, so check the first run.
- **NU (Nunavut)** is genuinely stale; its newest release is from 2017.
- **ASC** needs `playwright install chromium` or it fails with a clear message.

Fifteen of sixteen sources working is a normal, publishable week.

---

## Security notes

For the technical officer:

- No API key ever enters the repo. It lives in an environment variable on the
  updater PC. Nothing runs in GitHub Actions, so there is no third-party
  action supply chain to pin.
- All record text is HTML-escaped at render time via `esc()`. Scraped
  regulator text and model output are both treated as untrusted.
- `Source Link` is scheme-checked, so `javascript:` and `data:` URLs are
  dropped rather than rendered as links.
- Excel export prefixes any cell starting `= + - @` with an apostrophe,
  neutralising spreadsheet formula injection.
- A Content-Security-Policy meta tag restricts scripts to self and the XLSX
  CDN, blocks all network calls from the page, and forbids framing.
- `noindex, nofollow` keeps it out of search results while the repo is public.

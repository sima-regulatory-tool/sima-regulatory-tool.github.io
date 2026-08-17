#!/usr/bin/env bash
# ============================================================
#  SIMA Regulatory Scanner - WEEKLY UPDATER
#  Run on the updater Mac only:   ./update.sh
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

fail() { echo; echo "  *** $1"; echo "  *** Nothing published. Site still shows last week's data."; exit 1; }

echo; echo "[1/5] Getting the latest repo state..."
git pull --rebase || fail "git pull failed - resolve that first."

echo; echo "[2/5] Scraping and classifying (it will ask which week)..."
python3 run_weekly.py || fail "the pipeline stopped - see the message above."

echo; echo "[3/5] Embedding the data into every dashboard version..."
python3 publish.py || fail "publish.py failed."

echo; echo "[4/5] Checking whether anything changed..."
if git diff --quiet -- index.html index_v2.html data.json 2>/dev/null; then
  echo "    No new records this week. Nothing to publish."
  exit 0
fi

echo; echo "[5/5] Publishing to the website..."
git add data.json index.html index_v2.html 2>/dev/null || git add data.json index.html
git commit -m "Weekly update $(date +%Y-%m-%d)"
git push || fail "git push failed - check your GitHub credentials."

echo; echo "    Published. The site will refresh in about a minute."

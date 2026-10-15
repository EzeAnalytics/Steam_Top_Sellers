"""
Steam Top Sellers scraper (Group 7)

Collects the top 500 ranked items from
    https://store.steampowered.com/search/?filter=topsellers
by calling the same background endpoint the website uses
(/search/results/), which returns JSON containing the rows as HTML.

Each run saves two files in the output folder (default: raw/):
    steam_YYYY-MM-DD.json   the server responses, unchanged (evidence)
    steam_YYYY-MM-DD.csv    one row per item, fields extracted as displayed text
                            (cleaning into numbers happens in a separate script)

Responsible-scraping rules built in (assignment Section 4):
    - identifies itself with a course User-Agent and contact email
    - waits 2-5 seconds between requests
    - hard cap of 1,000 requests per run (a normal run uses about 10)
    - no login, no CAPTCHA or proxy tricks; stops if blocked (403/429)
    - public product data only

Usage (run from the repository root):
    python scraper/steam_scraper.py --pages 1 --out-dir data/test   # quick test
    python scraper/steam_scraper.py                                 # full daily run

Set your contact email either in CONTACT_EMAIL below or with the
environment variable SCRAPER_CONTACT_EMAIL.
"""

import argparse
import csv
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- settings
ENDPOINT = "https://store.steampowered.com/search/results/"
BASE_PARAMS = {
    "query": "",
    "count": 50,                 # the page itself uses 50; small values are ignored
    "filter": "topsellers",
    "supportedlang": "english",
    "infinite": 1,
    "cc": "us",                  # fixes the currency to USD for every run
}
TARGET_ITEMS = 500
CONTACT_EMAIL = os.environ.get("SCRAPER_CONTACT_EMAIL", "ezekielbulima@gmail.com")
USER_AGENT = f"DSC2107-Course-Project ({CONTACT_EMAIL})"
DELAY_RANGE = (2, 5)             # seconds between requests (assignment rule)
MAX_RETRIES = 4                  # attempts per page
TIMEOUT = 30                     # seconds
MAX_REQUESTS = 1000              # hard daily cap (assignment rule)

FIELDS = [
    "scraped_at", "rank", "item_key", "app_id", "title", "url",
    "release_date_text", "review_class", "review_text", "platforms",
    "price_text", "original_price_text", "discount_pct",
    "bundle_discount_text", "includes_text",
]

log = logging.getLogger("steam")


class BlockedError(Exception):
    """The site refused us (403/429). Rule: stop, document, reduce the rate."""


# ---------------------------------------------------------------- fetching
class Fetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.requests_made = 0

    def fetch_page(self, start):
        """Return (final_url, parsed_json) for one batch, or None on failure."""
        params = dict(BASE_PARAMS, start=start)
        for attempt in range(1, MAX_RETRIES + 1):
            if self.requests_made >= MAX_REQUESTS:
                raise RuntimeError(f"Request cap of {MAX_REQUESTS} reached, stopping.")
            self.requests_made += 1
            try:
                resp = self.session.get(ENDPOINT, params=params, timeout=TIMEOUT)
            except requests.RequestException as exc:
                self._backoff(attempt, f"network error ({exc})", start)
                continue

            if resp.status_code in (403, 429):
                raise BlockedError(f"HTTP {resp.status_code} at start={start}. "
                                   "Stop, document it, and reduce the rate.")
            if resp.status_code >= 500:
                self._backoff(attempt, f"server error {resp.status_code}", start)
                continue
            if resp.status_code >= 400:
                log.error("HTTP %d at start=%d, not retrying", resp.status_code, start)
                return None

            try:
                data = resp.json()
            except ValueError:
                self._backoff(attempt, "response was not valid JSON", start)
                continue
            if data.get("success") != 1:
                self._backoff(attempt, f"success={data.get('success')!r}", start)
                continue

            time.sleep(random.uniform(*DELAY_RANGE))     # be polite
            return resp.url, data

        log.error("Giving up on start=%d after %d attempts", start, MAX_RETRIES)
        return None

    @staticmethod
    def _backoff(attempt, reason, start):
        wait = 2 ** attempt + random.uniform(0, 1)
        log.warning("start=%d: %s. Attempt %d/%d, waiting %.1fs",
                    start, reason, attempt, MAX_RETRIES, wait)
        time.sleep(wait)


# ----------------------------------------------------------------- parsing
def _text(el):
    return el.get_text(strip=True) if el else None


def parse_row(a, rank, scraped_at):
    """Extract one item. Anything missing becomes None, never an exception."""
    row = {f: None for f in FIELDS}
    row["scraped_at"] = scraped_at
    row["rank"] = rank
    row["item_key"] = a.get("data-ds-itemkey")
    row["app_id"] = a.get("data-ds-appid")
    row["url"] = a.get("href", "").split("?")[0] or None
    row["title"] = _text(a.select_one("span.title"))
    row["release_date_text"] = _text(a.select_one("div.search_released"))

    review = a.select_one("span.search_review_summary")
    if review:
        row["review_text"] = review.get("data-tooltip-html")
        classes = [c for c in review.get("class", []) if c != "search_review_summary"]
        row["review_class"] = classes[0] if classes else None

    platforms = []
    for span in a.select("span.platform_img"):
        platforms += [c for c in span.get("class", []) if c in ("win", "mac", "linux")]
    row["platforms"] = ",".join(platforms) if platforms else None

    block = a.select_one("div.discount_block")
    row["discount_pct"] = block.get("data-discount") if block else None
    row["price_text"] = _text(a.select_one("div.discount_final_price"))
    row["original_price_text"] = _text(a.select_one("div.discount_original_price"))
    row["bundle_discount_text"] = _text(a.select_one("div.bundle_base_discount"))
    row["includes_text"] = _text(a.select_one("span.includes_games_results"))
    return row


def parse_results_html(html, start, scraped_at):
    """Parse one batch of rows. rank = start + position (1-based)."""
    soup = BeautifulSoup(html or "", "html.parser")
    rows = []
    for idx, a in enumerate(soup.select("a.search_result_row")):
        rank = start + idx + 1
        try:
            rows.append(parse_row(a, rank, scraped_at))
        except Exception:                      # one odd row must not stop the run
            log.exception("Could not parse the row at rank %d, keeping a blank row", rank)
            blank = {f: None for f in FIELDS}
            blank.update(scraped_at=scraped_at, rank=rank)
            rows.append(blank)
    return rows


# ----------------------------------------------------------------- storage
def save_outputs(out_dir, date_str, pages, rows):
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"steam_{date_str}.json"
    csv_path = out_dir / f"steam_{date_str}.csv"

    with json_path.open("w", encoding="utf-8") as f:
        json.dump({"endpoint": ENDPOINT, "pages": pages}, f, ensure_ascii=False)

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return json_path, csv_path


# -------------------------------------------------------------------- main
def main(argv=None):
    parser = argparse.ArgumentParser(description="Steam top sellers scraper")
    parser.add_argument("--pages", type=int, default=None,
                        help="max batches of 50 to fetch (default: enough for 500 items)")
    parser.add_argument("--out-dir", default="raw", help="output folder (default: raw)")
    parser.add_argument("--log-file", default="logs/steam_scraper.log")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace today's files if they already exist")
    args = parser.parse_args(argv)

    handlers = [logging.StreamHandler()]
    if args.log_file:
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s %(message)s")

    if "CHANGE-ME" in CONTACT_EMAIL:
        log.warning("Contact email is still the placeholder. Set CONTACT_EMAIL "
                    "in the script or SCRAPER_CONTACT_EMAIL before the real runs.")

    out_dir = Path(args.out_dir)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if (out_dir / f"steam_{date_str}.json").exists() and not args.overwrite:
        log.error("steam_%s files already exist in %s. Use --overwrite to replace them.",
                  date_str, out_dir)
        return 2

    fetcher = Fetcher()
    pages, rows, start = [], [], 0
    log.info("Run started (UTC %s), target %d items", date_str, TARGET_ITEMS)

    try:
        while len(rows) < TARGET_ITEMS and (args.pages is None or len(pages) < args.pages):
            result = fetcher.fetch_page(start)
            if result is None:
                break
            url, data = result
            scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            pages.append({"start": start, "url": url, "scraped_at": scraped_at,
                          "response": data})

            page_rows = parse_results_html(data.get("results_html"), start, scraped_at)
            log.info("start=%d: %d items parsed (total so far %d)",
                     start, len(page_rows), len(rows) + len(page_rows))
            if not page_rows:
                log.warning("No items returned at start=%d, stopping.", start)
                break
            rows.extend(page_rows)
            start += len(page_rows)
            total = data.get("total_count")
            if isinstance(total, int) and start >= total:
                break
    except BlockedError as exc:
        log.error("BLOCKED: %s", exc)
    except RuntimeError as exc:
        log.error("%s", exc)
    except KeyboardInterrupt:
        log.info("Stopped by user.")

    # remove duplicates (keep first / best rank), then cap at the target
    seen, unique = set(), []
    for r in rows:
        key = r["item_key"] or f"rank-{r['rank']}"
        if key in seen:
            log.warning("Duplicate item %s at rank %s dropped", key, r["rank"])
            continue
        seen.add(key)
        unique.append(r)
    rows = unique[:TARGET_ITEMS]

    if not pages:
        log.error("Nothing was collected, no files written.")
        return 1

    json_path, csv_path = save_outputs(out_dir, date_str, pages, rows)
    log.info("Saved %d items -> %s and %s (%d requests made)",
             len(rows), csv_path, json_path, fetcher.requests_made)

    complete = len(rows) >= TARGET_ITEMS or (args.pages is not None and len(rows) > 0)
    if not complete:
        log.error("Run incomplete: only %d of %d items collected.", len(rows), TARGET_ITEMS)
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())

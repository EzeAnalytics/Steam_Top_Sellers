"""
Cleaning + database loading (DSC2107, Group 7: Steam Top Sellers)

Reads every raw daily file  raw/steam_YYYY-MM-DD.csv  and, in ONE step:
    1. cleans the text fields into proper types (numbers, dates, flags)
    2. removes duplicates
    3. rebuilds a SQLite database with three tables
           products      one row per product
           observations  one row per product per day (rank, price, discount, reviews)
           scrape_runs   one row per daily file (how many items, when)
    4. writes a flat export  data/steam_clean.csv  (handy for the notebook)
    5. writes a data-quality report  docs/cleaning_report.md

The script is fully reproducible: the database is rebuilt from the raw files
every time, so the raw files are the single source of truth and nothing is
edited by hand. Only the Python standard library is needed.

Usage (run from the repository root):
    python scraper/clean_and_load.py
Options:
    --raw-dir raw   --db data/steam.db   --clean-csv data/steam_clean.csv
    --report docs/cleaning_report.md
"""

import argparse
import csv
import logging
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

log = logging.getLogger("clean")

FILE_RE = re.compile(r"^steam_(\d{4}-\d{2}-\d{2})\.csv$")
REQUIRED_COLUMNS = [
    "scraped_at", "rank", "item_key", "title", "release_date_text", "review_text",
    "platforms", "price_text", "original_price_text", "discount_pct",
]
REVIEW_RE = re.compile(r"(\d{1,3})%\s+of\s+the\s+([\d,]+)\s+user\s+review")
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
DATE_FORMATS = ("%b %d, %Y", "%d %b, %Y", "%B %d, %Y", "%Y-%m-%d")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE scrape_runs (
    scrape_date      TEXT PRIMARY KEY,          -- UTC date taken from the file name
    source_file      TEXT NOT NULL,
    first_scraped_at TEXT,
    last_scraped_at  TEXT,
    n_items          INTEGER NOT NULL
);

CREATE TABLE products (
    product_key   TEXT PRIMARY KEY,             -- Steam item key, e.g. App_730
    app_id        INTEGER,
    title         TEXT NOT NULL,
    url           TEXT,
    release_date  TEXT,                         -- ISO date, NULL if unknown
    platforms     TEXT,                         -- e.g. win,mac,linux
    on_windows    INTEGER NOT NULL CHECK (on_windows IN (0, 1)),
    on_mac        INTEGER NOT NULL CHECK (on_mac     IN (0, 1)),
    on_linux      INTEGER NOT NULL CHECK (on_linux   IN (0, 1)),
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    days_in_top   INTEGER NOT NULL              -- number of daily files it appears in
);

CREATE TABLE observations (
    obs_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_key         TEXT    NOT NULL REFERENCES products (product_key),
    scrape_date         TEXT    NOT NULL REFERENCES scrape_runs (scrape_date),
    scraped_at          TEXT    NOT NULL,
    rank                INTEGER NOT NULL CHECK (rank >= 1),
    price_usd           REAL    CHECK (price_usd IS NULL OR price_usd >= 0),
    original_price_usd  REAL    CHECK (original_price_usd IS NULL OR original_price_usd >= 0),
    discount_pct        INTEGER CHECK (discount_pct IS NULL OR discount_pct BETWEEN 0 AND 100),
    is_free             INTEGER NOT NULL CHECK (is_free IN (0, 1)),
    review_label        TEXT,
    review_pct          INTEGER CHECK (review_pct IS NULL OR review_pct BETWEEN 0 AND 100),
    review_count        INTEGER CHECK (review_count IS NULL OR review_count >= 0),
    bundle_discount_pct INTEGER,
    includes_games      INTEGER,
    UNIQUE (product_key, scrape_date)
);

CREATE INDEX idx_obs_date    ON observations (scrape_date);
CREATE INDEX idx_obs_product ON observations (product_key);
"""

FLAT_QUERY = """
SELECT o.scrape_date, o.scraped_at, o.rank, p.product_key, p.app_id, p.title, p.url,
       p.release_date, p.platforms, p.on_windows, p.on_mac, p.on_linux,
       o.price_usd, o.original_price_usd, o.discount_pct, o.is_free,
       o.review_label, o.review_pct, o.review_count,
       o.bundle_discount_pct, o.includes_games
FROM observations o JOIN products p USING (product_key)
ORDER BY o.scrape_date, o.rank
"""


# ------------------------------------------------------------- field cleaners
def parse_price(text, issues):
    """'$1,059.00' -> 1059.0 ; 'Free' -> 0.0 ; blank -> None (no price shown)."""
    text = (text or "").strip()
    if not text:
        return None
    if text.lower().startswith("free"):
        return 0.0
    match = NUMBER_RE.search(text)
    if not match:
        issues["price_unparsed"] += 1
        return None
    if "$" not in text:
        # Every request asks for USD. A different currency would be silently
        # wrong (e.g. "9,99" read as 999), so store NULL and count it.
        issues["non_usd_price"] += 1
        return None
    return float(match.group().replace(",", ""))


def parse_int(text):
    """'-15%' / '15' -> 15 ; blank -> None."""
    match = re.search(r"\d+", text or "")
    return int(match.group()) if match else None


def parse_review(text, issues):
    """'Mostly Positive<br>73% of the 1,226 user reviews ...' -> (label, 73, 1226)."""
    text = (text or "").strip()
    if not text:
        return None, None, None
    match = REVIEW_RE.search(text)
    if not match:
        issues["review_unparsed"] += 1
        return None, None, None
    label = text.split("<br>")[0].strip() or None
    return label, int(match.group(1)), int(match.group(2).replace(",", ""))


def parse_date(text, issues):
    """'Oct 13, 2026' -> '2026-10-13' ; anything else -> None (counted)."""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    issues["release_date_unparsed"] += 1
    return None


# ---------------------------------------------------------------- file reading
def clean_file(path, scrape_date):
    """Return (cleaned_rows, issues Counter) for one raw daily file."""
    issues = Counter()
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name}: missing columns {missing}")
        raw_rows = list(reader)

    best = {}                                    # product_key -> row with the best rank
    for raw in raw_rows:
        issues["rows_read"] += 1
        try:
            rank = int(raw["rank"])
        except (TypeError, ValueError):
            issues["bad_rank_dropped"] += 1
            continue
        key = (raw.get("item_key") or "").strip()
        if not key:
            issues["no_item_key_dropped"] += 1
            continue
        title = (raw.get("title") or "").strip()
        if not title:
            issues["no_title_dropped"] += 1
            continue

        price = parse_price(raw["price_text"], issues)
        is_free = int(price == 0.0)
        original = parse_price(raw["original_price_text"], issues)
        discount = parse_int(raw["discount_pct"])
        if discount is None and price is not None:
            discount = 0                          # free / not discounted
        if original is None and price is not None:
            original = price                      # no discount: original = final
        if price is None:
            issues["price_missing"] += 1
        if price is not None and original is not None and original < price:
            issues["original_below_final"] += 1

        label, pct, count = parse_review(raw["review_text"], issues)
        if label is None:
            issues["no_review_summary"] += 1

        release = parse_date(raw["release_date_text"], issues)
        platforms = ",".join(p for p in (raw.get("platforms") or "").split(",") if p)
        app_id = parse_int(raw.get("app_id"))

        includes = parse_int(raw.get("includes_text"))
        bundle = parse_int(raw.get("bundle_discount_text"))
        if includes is not None:
            issues["bundles"] += 1

        row = {
            "product_key": key, "app_id": app_id, "title": title,
            "url": (raw.get("url") or "").strip() or None,
            "release_date": release, "platforms": platforms or None,
            "on_windows": int("win" in platforms.split(",")),
            "on_mac": int("mac" in platforms.split(",")),
            "on_linux": int("linux" in platforms.split(",")),
            "scrape_date": scrape_date,
            "scraped_at": (raw.get("scraped_at") or "").strip() or f"{scrape_date}T00:00:00+00:00",
            "rank": rank, "price_usd": price, "original_price_usd": original,
            "discount_pct": discount, "is_free": is_free,
            "review_label": label, "review_pct": pct, "review_count": count,
            "bundle_discount_pct": bundle, "includes_games": includes,
        }
        if key in best:
            issues["duplicates_removed"] += 1
            if rank >= best[key]["rank"]:
                continue                          # keep the best (lowest) rank
        best[key] = row

    rows = sorted(best.values(), key=lambda r: r["rank"])
    return rows, issues


# ---------------------------------------------------------------------- main
def build_database(db_path, runs, observations):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()                          # rebuild from scratch: reproducible
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)

    # product table: attributes from the most recent day each product appeared
    products = {}
    seen_days = {}
    for r in observations:                        # observations are in date order
        merged = dict(r)
        old = products.get(r["product_key"])
        if old:                                   # keep the last known value if a day lacks it
            for field in ("release_date", "platforms", "url", "app_id"):
                if merged[field] is None:
                    merged[field] = old[field]
            for flag, name in (("on_windows", "win"), ("on_mac", "mac"), ("on_linux", "linux")):
                merged[flag] = int(name in (merged["platforms"] or "").split(","))
        products[r["product_key"]] = merged
        seen_days.setdefault(r["product_key"], []).append(r["scrape_date"])

    con.executemany(
        "INSERT INTO scrape_runs VALUES (?,?,?,?,?)",
        [(d, v["file"], v["first"], v["last"], v["n"]) for d, v in sorted(runs.items())])
    con.executemany(
        "INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(k, r["app_id"], r["title"], r["url"], r["release_date"], r["platforms"],
          r["on_windows"], r["on_mac"], r["on_linux"],
          min(seen_days[k]), max(seen_days[k]), len(seen_days[k]))
         for k, r in products.items()])
    con.executemany(
        """INSERT INTO observations
           (product_key, scrape_date, scraped_at, rank, price_usd, original_price_usd,
            discount_pct, is_free, review_label, review_pct, review_count,
            bundle_discount_pct, includes_games)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(r["product_key"], r["scrape_date"], r["scraped_at"], r["rank"], r["price_usd"],
          r["original_price_usd"], r["discount_pct"], r["is_free"], r["review_label"],
          r["review_pct"], r["review_count"], r["bundle_discount_pct"], r["includes_games"])
         for r in observations])
    con.commit()
    return con


def export_flat_csv(con, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    cur = con.execute(FLAT_QUERY)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([c[0] for c in cur.description])
        writer.writerows(cur)


def write_report(path, con, per_day, all_issues, skipped):
    path.parent.mkdir(parents=True, exist_ok=True)
    dates = [r[0] for r in con.execute("SELECT scrape_date FROM scrape_runs ORDER BY 1")]
    n_prod = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    n_obs = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    always = con.execute("SELECT COUNT(*) FROM products WHERE days_in_top = ?",
                         (len(dates),)).fetchone()[0]

    gaps = []
    if dates:
        d0, d1 = datetime.fromisoformat(dates[0]), datetime.fromisoformat(dates[-1])
        have = set(dates)
        for i in range((d1 - d0).days + 1):
            day = (d0.toordinal() + i)
            iso = datetime.fromordinal(day).date().isoformat()
            if iso not in have:
                gaps.append(iso)

    lines = [
        "# Cleaning and loading report", "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} (local time). "
        "File dates are UTC dates taken from the raw file names.", "",
        "## Summary", "",
        f"- Daily files loaded: **{len(dates)}** ({dates[0]} to {dates[-1]})" if dates else "- No files loaded",
        f"- Observations (product-days): **{n_obs}**",
        f"- Distinct products: **{n_prod}**; present on every loaded day: **{always}**",
        f"- Missing days inside the range: {', '.join(gaps) if gaps else 'none'}",
    ]
    if skipped:
        lines.append(f"- Files skipped: {', '.join(skipped)}")
    lines += ["", "## Per-day checks", "",
              "| Date | Rows read | Rows loaded | Rank range | Free | Discounted | No review | No price | Duplicates removed |",
              "|---|---|---|---|---|---|---|---|---|"]
    for date in dates:
        s = con.execute(
            """SELECT COUNT(*), MIN(rank), MAX(rank), SUM(is_free),
                      SUM(CASE WHEN discount_pct > 0 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN review_label IS NULL THEN 1 ELSE 0 END),
                      SUM(CASE WHEN price_usd IS NULL THEN 1 ELSE 0 END)
               FROM observations WHERE scrape_date = ?""", (date,)).fetchone()
        iss = per_day[date]
        lines.append(f"| {date} | {iss['rows_read']} | {s[0]} | {s[1]}-{s[2]} | {s[3]} | {s[4]} "
                     f"| {s[5]} | {s[6]} | {iss['duplicates_removed']} |")

    lines += ["", "## Cleaning rules applied", "",
              "- Prices: `$1,059.00` becomes 1059.0; `Free` becomes 0 with `is_free = 1`; "
              "a blank price (item shown without a price) stays NULL and is **not** treated as free.",
              "- Original price: when there is no discount it equals the final price.",
              "- Discount %: blank becomes 0 when a price exists, otherwise NULL.",
              "- Reviews: the tooltip text is split into label, percent and count; items without reviews stay NULL.",
              "- Release dates: parsed to ISO format (YYYY-MM-DD); unparseable values become NULL.",
              "- Platforms: split into three 0/1 flags (Windows, Mac, Linux).",
              "- Bundles: `Includes 2 games` becomes `includes_games = 2`; the extra bundle discount is kept separately.",
              "- Duplicates: if a product appears twice in one file, the best (lowest) rank is kept.",
              "- Currency: all prices are USD (requests were made with `cc=us`).", "",
              "## Issues found across all files", "",
              "| Issue | Count |", "|---|---|"]
    for name, count in sorted(all_issues.items()):
        if name != "rows_read":
            lines.append(f"| {name} | {count} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Clean raw Steam files and load SQLite")
    parser.add_argument("--raw-dir", default="raw")
    parser.add_argument("--db", default="data/steam.db")
    parser.add_argument("--clean-csv", default="data/steam_clean.csv")
    parser.add_argument("--report", default="docs/cleaning_report.md")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    raw_dir = Path(args.raw_dir)
    files = sorted((m.group(1), p) for p in raw_dir.glob("steam_*.csv")
                   if (m := FILE_RE.match(p.name)))
    if not files:
        log.error("No files like steam_YYYY-MM-DD.csv found in %s", raw_dir)
        return 1

    runs, observations, per_day, skipped = {}, [], {}, []
    all_issues = Counter()
    for scrape_date, path in files:
        try:
            rows, issues = clean_file(path, scrape_date)
        except Exception as exc:                  # a bad file must not stop the others
            log.error("Skipping %s: %s", path.name, exc)
            skipped.append(path.name)
            continue
        if not rows:
            log.error("Skipping %s: no usable rows", path.name)
            skipped.append(path.name)
            continue
        stamps = sorted(r["scraped_at"] for r in rows)
        runs[scrape_date] = {"file": path.name, "first": stamps[0], "last": stamps[-1], "n": len(rows)}
        observations.extend(rows)
        per_day[scrape_date] = issues
        all_issues.update(issues)
        flag = "" if len(rows) >= 500 else "  <-- fewer than 500 items!"
        log.info("%s: %d rows loaded (rank %d-%d)%s", path.name, len(rows),
                 rows[0]["rank"], rows[-1]["rank"], flag)

    if not observations:
        log.error("Nothing could be loaded.")
        return 1

    con = build_database(Path(args.db), runs, observations)
    export_flat_csv(con, Path(args.clean_csv))
    write_report(Path(args.report), con, per_day, all_issues, skipped)

    n_prod = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    n_obs = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    check = con.execute("PRAGMA foreign_key_check").fetchall()
    con.close()
    log.info("Loaded %d days, %d products, %d observations -> %s", len(runs), n_prod, n_obs, args.db)
    log.info("Flat export: %s | Report: %s", args.clean_csv, args.report)
    if check:
        log.error("Foreign-key problems found: %s", check[:5])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

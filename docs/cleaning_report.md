# Cleaning and loading report

Generated: 2026-10-19 10:53 (local time). File dates are UTC dates taken from the raw file names.

## Summary

- Daily files loaded: **7** (2026-10-13 to 2026-10-19)
- Observations (product-days): **3499**
- Distinct products: **724**; present on every loaded day: **336**
- Missing days inside the range: none

## Per-day checks

| Date | Rows read | Rows loaded | Rank range | Free | Discounted | No review | No price | Duplicates removed |
|---|---|---|---|---|---|---|---|---|
| 2026-10-13 | 500 | 500 | 1-500 | 68 | 182 | 31 | 1 | 0 |
| 2026-10-14 | 500 | 500 | 1-500 | 70 | 180 | 30 | 1 | 0 |
| 2026-10-15 | 500 | 500 | 1-500 | 70 | 189 | 31 | 1 | 0 |
| 2026-10-16 | 499 | 499 | 1-500 | 72 | 167 | 25 | 2 | 0 |
| 2026-10-17 | 500 | 500 | 1-500 | 69 | 175 | 28 | 1 | 0 |
| 2026-10-18 | 500 | 500 | 1-500 | 68 | 162 | 26 | 1 | 0 |
| 2026-10-19 | 500 | 500 | 1-500 | 61 | 166 | 25 | 1 | 0 |

## Cleaning rules applied

- Prices: `$1,059.00` becomes 1059.0; `Free` becomes 0 with `is_free = 1`; a blank price (item shown without a price) stays NULL and is **not** treated as free.
- Original price: when there is no discount it equals the final price.
- Discount %: blank becomes 0 when a price exists, otherwise NULL.
- Reviews: the tooltip text is split into label, percent and count; items without reviews stay NULL.
- Release dates: parsed to ISO format (YYYY-MM-DD); unparseable values become NULL.
- Platforms: split into three 0/1 flags (Windows, Mac, Linux).
- Bundles: `Includes 2 games` becomes `includes_games = 2`; the extra bundle discount is kept separately.
- Duplicates: if a product appears twice in one file, the best (lowest) rank is kept.
- Currency: all prices are USD (requests were made with `cc=us`).

## Issues found across all files

| Issue | Count |
|---|---|
| bundles | 57 |
| no_review_summary | 196 |
| price_missing | 8 |

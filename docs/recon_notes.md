# Reconnaissance Notes: Steam Top Sellers (Group 7)

**Course:**  Data Mining and Wrangling [website scrapping]
**Target:** `https://store.steampowered.com/search/?filter=topsellers`
**Date of reconnaissance:** 13 October 2026
**Purpose:** Working notes that feed the 1-page reconnaissance note (PDF) and the Responsible Scraping / Ethics sections of the final report.

---

## 1. robots.txt findings

**File checked:** `https://store.steampowered.com/robots.txt`
**Checked on:** 13 Oct 2026
**Saved copy:** `docs/robots_steam_2026-10-13.txt`

Contents of the file:

```
Host: store.steampowered.com
User-Agent: *
Disallow: /share/
Disallow: /news/externalpost/
Disallow: /account/emailoptout/?*token=
Disallow: /account/notificationsettings/?*token=
Disallow: /login/?*guestpasskey=
Disallow: /join/?*redir=
Disallow: /account/ackgift/
Disallow: /email/
Disallow: /widget/
```

| Item | Finding |
|---|---|
| Applies to | `User-Agent: *` (all bots, including ours) |
| Target page `/search/?filter=topsellers` | **Not disallowed** |
| Results endpoint `/search/results/` | **Not disallowed** (no Disallow line matches it) |
| Blocked paths | Sharing links, external news posts, account/email token pages, login and join redirects, gift pages, `/email/`, `/widget/` |
| Crawl-delay | None specified |
| Sitemap | None listed |
| Our commitment | Stay out of every blocked path; apply our own 2-5 second delay; cap at 1,000 requests per daily run |

**Terms of service:** Steam's own terms (Steam Subscriber Agreement) should be skimmed by the group for any wording on automated access. *TODO: record the outcome here.*

---

## 2. Site inspection (browser Network tab)

**Tool used:** Chrome/Edge DevTools (F12), Network tab, Fetch/XHR filter, Keep log on.
**Action:** Scrolled the top-sellers list; each scroll loaded the next batch of games.

### What we observed (from our own screenshots)

- The normal page is `/search/?filter=topsellers`; game rows are **not** all in the first page load. More rows load as the user scrolls ("infinite scroll").
- Each batch is a background **GET** request to `/search/results/` that returns **200 OK**.
- Requests seen with `start=50`, `100`, `400`, `450`, `500`, `550`, `600`, so **start increases by 50 each time**.
- Thumbnail requests (`capsule_231x87.jpg`) and small pop-up requests (`<appid>?review_score_preference...`, triggered by hovering) are unrelated to the list and are ignored.
- A `saledata/?cc=UG` request shows Steam detected our region as Uganda. Prices were displayed in **USD** (e.g. $39.99, $7.99).

### Request URL captured

```
https://store.steampowered.com/search/results/?query&start=600&count=50&dynamic_data=&sort_by=_ASC&hwtype=0&snr=1_7_7_7000_7&filter=topsellers&supportedlang=english&infinite=1
```

| Parameter | Meaning |
|---|---|
| `start` | Offset in the list (0, 50, 100, ...) |
| `count` | Items per request (the page uses 50) |
| `filter=topsellers` | The top-sellers ranking (our assigned list) |
| `supportedlang=english` | Language filter added by the page |
| `infinite=1` | Marks the infinite-scroll request |
| `snr`, `dynamic_data`, `sort_by`, `hwtype` | Added by the page; not needed for our scraper |

**Request method:** GET  **Status:** 200 OK  **Login needed:** No

### Response structure

Verified with one test request on 13 Oct 2026:

```
https://store.steampowered.com/search/results/?query&start=0&count=5&filter=topsellers&supportedlang=english&infinite=1&cc=us
```

The response is **JSON wrapping HTML**:

| Key | Contents |
|---|---|
| `success` | 1 when the request worked |
| `results_html` | One long text block of HTML containing the game rows |
| `total_count` | 9066 on 13 Oct 2026 (full list size; we only need the first 500) |
| `start` | The offset requested |

**Observation:** a request with `count=5` returned 25 rows, so Steam appears to enforce its own minimum. We use `count=50`, the value the website itself uses.

*TODO (group): confirm the structure yourselves in the Network tab (Preview or Response tab) and add a screenshot.*

---

## 3. Where each field lives

Each game is one `<a class="search_result_row">` element inside `results_html`.

| Field | Source in the HTML | Notes |
|---|---|---|
| App ID (unique key) | `data-ds-appid` attribute | Use as the primary key in the database |
| Title | `span.title` | |
| Rank | Not in the data | Calculate: `start` + position in the batch + 1 |
| Price (final) | `div.discount_final_price` text | Free games show the text `Free` |
| Original price | `div.discount_original_price` text | Only present when discounted |
| Discount % | `data-discount` attribute on `div.discount_block` | 0 when not discounted |
| Release date | `div.search_released` text | Format like `Oct 13, 2026` |
| Review summary | `span.search_review_summary`, attribute `data-tooltip-html` | Text like `Mostly Positive<br>73% of the 226 user reviews for this game are positive.` Parse into label, percent, count |
| Platforms | `span.platform_img` with class `win`, `mac` or `linux` | Collect into a list |
| Scrape time | Added by our scraper | `scraped_at` timestamp on every record |

---

## 4. Edge cases and data-quality gotchas

These are the "missing fields and layout surprises" the scraper must handle without crashing.

1. **Free games** show `Free`, not a price. Store price as 0.
2. **No discount**: no original-price element. Set original price equal to final price and discount to 0.
3. **No reviews** (new games): the review span is absent. Store null.
4. **Thousands separators**: prices like `$1,059.00` (Steam Frame). Remove commas before converting to a number.
5. **Hardware and DLC are in the list**, not only games (Steam Frame, Steam Deck, season passes, expansion content). They are outliers for the analysis; keep them and consider flagging a product type.
6. **Bundles** ("Includes 2 games") show two discounts (e.g. `-10%` and `-15%`). Rule to decide and document: use the main `data-discount` value, and keep the displayed final and original prices.
7. **Outer price attribute can disagree with the displayed price** (Steam Deck showed two different numbers). Parse the displayed `discount_final_price` text, not the outer `data-price-final`.
8. **Unusual release dates**: some items are not yet released (dates in the future). Keep as given and parse into a proper date.
9. **Review text varies**: some tooltips have extra sentences after the percentage. Parse only the label, the percent and the review count.
10. **Rank churn**: games can enter and leave the top 500 between days. The analysis must handle products that appear on some days only.

---

## 5. Currency decision

- Our browser was identified as Uganda (`cc=UG`) and showed **USD**.
- A scraper running from another place (for example GitHub's servers) could be shown a different currency.
- **Decision:** add `cc=us` to every request so all 7 daily runs are in **USD**. Tested once on 13 Oct 2026 and it returned USD prices.

---

## 6. Chosen approach (Step 4 decision)

**Call the `/search/results/` JSON endpoint with `requests`, then parse the `results_html` field with BeautifulSoup.** Playwright and Selenium are not needed.

**Justification:**

1. No JavaScript rendering is needed.
2. Pagination is controlled by one parameter (`start`), so the top 500 needs **10 requests** per run, far below the 1,000-request cap.
3. Rows carry stable attributes (`data-ds-appid`, `data-discount`) that make parsing reliable.
4. It is the same request the website itself makes for every visitor.
5. The path is not disallowed in robots.txt.

**Risk:** this is an undocumented endpoint and could change. Mitigation: the scraper checks `success == 1`, logs every failure, and saves the raw response so problems can be traced.

**Alternative considered:** parsing the rendered page HTML. Rejected because the first page load does not contain all rows; the rest arrive through the background requests above.

---

## 7. Scraper design (planned)

| Setting | Value |
|---|---|
| Items per run | Top 500 (start = 0, 50, ..., 450) |
| Requests per run | About 10 |
| Delay between requests | Random 2-5 seconds |
| Daily request cap | 1,000 (we use roughly 1% of it) |
| User-Agent | `DSC2107-Course-Project (your-email@example.com)` *(replace with a real contact email)* |
| Retries | With back-off on network errors |
| Login | None |
| CAPTCHA / proxies / spoofing | None. If blocked: stop, document, slow down |
| Personal data | None collected (only public product data) |
| Raw output | One unchanged file per run, e.g. `raw/steam_2026-10-13.json` |
| Timestamp | `scraped_at` on every record |
| Currency | USD via `cc=us` |

---

## 8. Ethics and responsible scraping

- Only public, logged-out product data is collected.
- No reviewer names, profiles or other personal data.
- Low request rate with delays and a daily cap.
- The scraper identifies itself with a course User-Agent and contact email.
- Data is for coursework only and will not be published or resold.
- robots.txt was checked, saved with a date, and respected.
- *TODO: add one sentence about Steam's terms of service once read.*

---

## 9. Pending items

- [ ] Confirm the response structure yourselves in the Preview/Response tab and screenshot it
- [ ] "View page source" check: search for the first game's name; note whether it appears in the page source
- [ ] Take screenshots: Request URL (Headers tab), Preview tab, Network list showing `start=` values
- [ ] Skim Steam's terms of service and record the outcome
- [ ] Put a real contact email in the User-Agent
- [ ] Books.toscrape.com warm-up (about 1,000 books)
- [ ] Turn these notes into the 1-page recon note (PDF)

# RC-15: Better deal emails

**Status:** Approved
**Backlog item:** RC-15 in `docs/BACKLOG.md`

## Summary

The Yahoo job sends one digest email per run, covering every tracked item, and sends nothing at all when there are no new deals.
The digest is an HTML table per item (image, listing, price in yen and USD, market price, discount) with a plain-text alternative, every seller-supplied value is HTML-escaped, and a deal is recorded as seen only after the email was actually sent.

## Motivation

Today `report_item_deals` (`src/yahoo_scrape.py:142-196`) sends one email per item, every run, even when the item has no new deals.
With two tracked items that is two emails per run, and usually both are empty (`send_email(f"Daily Refresh on {item.name}", body_text)` at `src/yahoo_scrape.py:195` runs unconditionally).
RC-01, RC-07 and RC-03b all listed this as a known issue owned by RC-15.

The body is a list of `Title:`, `Price:`, `Product Link:` and `Image:` lines joined with `<br>` (`src/yahoo_scrape.py:179-194`).
Seller-written titles and image URLs are interpolated into that HTML without escaping, so a title such as `<b>` or `</a>` changes the layout of the email, and an image URL is shown as text instead of an image.

There is also a data-loss bug that a digest makes worse.
`insert_listing` marks a listing as seen (`src/yahoo_scrape.py:166`) before the email is sent, and `send_email` catches every exception and only prints it (`src/sendemail.py:52-61`).
So when Gmail rejects the login or the network drops, the deals of that run are stored as seen and are never emailed.
Today that loses one item's deals; with one digest it loses every item's deals for the run.
Also, every new deal is marked seen, including the ones cut by the top-5 limit (`src/yahoo_scrape.py:176-177`), so deal number 6 is never shown to the user at all.

## Scope

**In scope:**

- A new module `src/digest.py` with the deal record and a pure digest builder (no I/O): subject, HTML body, and plain-text body.
- `src/yahoo_scrape.py`: replace `report_item_deals` with two pure functions, `find_item_deals` (new deals for one item) and `build_sections` (all items, top-5 cut, one listing in at most one section), and make `main()` build one digest and send it once, or print one line and send nothing.
- `src/sendemail.py`: `send_email(subject, html_body, text_body)` sends `multipart/alternative` and returns `True` or `False` instead of `None`.
- `src/db.py`: a read-only `get_known_listing_urls(urls)`, so deals can be found without writing, and recorded only after a successful send (Open question 1).
- HTML safety: everything interpolated into HTML is escaped with `html.escape(..., quote=True)`, and only `http`/`https` URLs become `href` or `src` values.
- Dry run prints the whole digest (subject, text part, HTML part).
- A printed line and no email when there are no new deals; a one-line `Error:` and exit code 1 when the send fails (Open question 7).
- Updating the tests listed under "Superseded tests", the `sent_emails` fixture in `tests/conftest.py`, and the README.

**Out of scope:**

- Logging, retries, and turning `print` into `logging`, including `send_email`'s own error print (RC-10).
- The hardcoded yen rate `0.0062` (RC-08).
  It moves into a named constant `YEN_TO_USD` with the same value, nothing more.
- Auction-aware deal rules, buy-now prices, end times (RC-16).
- Word-aware or Unicode-normalized keyword matching (RC-17).
- Writing `listing_matches` rows (RC-04) and updating `last_seen_at` or re-alerting on price drops (RC-05).
- Any change to `src/schema.sql`, `src/catalog.json`, or `parse_listings` / `_parse_record`.
- Email attachments, embedded (CID) images, CSS `<style>` blocks, web fonts, dark-mode styling, tracking pixels, and unsubscribe links.
- More than one recipient, a configurable subject, or a configurable top-N.
- Splitting an oversized digest into several emails (see "Known issues").

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/digest.py` (new) | `Deal`, `ItemDeals`, `Digest` dataclasses; `is_safe_http_url`; `build_digest`. Pure: no database, network, file, or print. |
| `src/yahoo_scrape.py` | Remove `report_item_deals`; add `YEN_TO_USD`, `TOP_DEALS_PER_ITEM`, `NO_DEALS_LINE`, `find_item_deals`, `build_sections`; rewrite the loop in `main()`. |
| `src/sendemail.py` | New signature and return value; `multipart/alternative` with a UTF-8 text part and a UTF-8 HTML part; new dry-run output. |
| `src/db.py` (shared) | Add `get_known_listing_urls(urls)`. No other change. |
| `tests/conftest.py` | `sent_emails` fixture records `(subject, html, text)` and returns `True`. |
| `tests/test_digest.py` (new) | Pure tests for `build_digest` and `is_safe_http_url`. |
| `tests/test_yahoo_scrape.py` | Pure tests for `find_item_deals` and `build_sections`; rewritten pipeline and `main()` tests (see "Superseded tests"). |
| `tests/test_sendemail.py` | Three-argument calls, MIME structure, return values, new dry-run output. |
| `tests/test_db.py` | Tests for `get_known_listing_urls`. |
| `tests/test_live_fixtures.py` | One new test: a digest built from the live page round-trips its real image URLs. |
| `README.md` | "What it does", "Project structure" (`digest.py`), and the dry-run paragraph in "Configuration". |

`docs/BACKLOG.md` lists RC-15 as touching only `src/sendemail.py` and `src/yahoo_scrape.py`; the orchestrator should update that line to add `src/db.py` and `src/digest.py` if Open question 1 is answered as recommended.

### Schema changes and migration

None.
`listings` rows are written by the existing `insert_listing` with the same columns and values as today, only later in the run.
An existing `raincheck.db` needs no change, and rows written before this change keep deduplicating, because `get_known_listing_urls` matches on the same `listings.url` values.

### Interfaces

#### `src/digest.py` (new)

```python
from dataclasses import dataclass
from datetime import date
from typing import Sequence

@dataclass(frozen=True)
class Deal:
    item_id: int
    brand_id: int
    url: str
    title: str               # as parsed (stripped); display normalization happens in build_digest
    price_yen: int
    price_usd: float
    market_price_usd: float  # the item's effective price used for the comparison
    discount_pct: float      # (market - price_usd) / market * 100, always > 0
    image_url: str | None

@dataclass(frozen=True)
class ItemDeals:
    item_name: str
    market_price_usd: float
    deals: tuple[Deal, ...]  # shown deals, best discount first, at most TOP_DEALS_PER_ITEM
    not_shown: int = 0       # new deals for this item cut by the limit

@dataclass(frozen=True)
class Digest:
    subject: str
    html: str
    text: str
    deals: tuple[Deal, ...]  # every shown deal, in display order; main() records exactly these

def is_safe_http_url(url: str | None) -> bool
def build_digest(sections: Sequence[ItemDeals], run_date: date) -> Digest | None
```

`is_safe_http_url(url)` is `True` only when all of these hold:

- `url` is a non-empty `str`.
- It contains no whitespace and no control character (no character for which `ch.isspace()` is true, and none with code point below `0x20` or equal to `0x7f`).
  This rejects values like `" javascript:..."` and `"java\nscript:..."` outright, instead of relying on how `urllib.parse` strips them.
- `urllib.parse.urlsplit(url)` has a scheme equal to `http` or `https` (case-insensitive) and a non-empty `netloc`.
  This rejects `javascript:`, `data:`, `vbscript:`, `ftp:`, scheme-relative `//host/path`, and `https:path`.

`build_digest(sections, run_date)`:

- Returns `None` when there is no shown deal in any section (empty `sections`, or every section has empty `deals`).
  Sections with empty `deals` are ignored even if `not_shown > 0` (cannot happen from `build_sections`, but the rule is defined).
- Keeps the section order and the deal order it is given; it does not sort.
- Never prints, reads the clock, or touches the database or network; `run_date` is passed in so tests are deterministic.

Formatting, used identically in the HTML and the text part:

| Value | Format | Example |
| --- | --- | --- |
| Yen price | `f"¥{price_yen:,}"` (U+00A5) | `¥50,000` |
| USD price, market price | `f"${value:,.2f}"` | `$310.00`, `$1,234.50` |
| Discount | `f"{discount_pct:.1f}%"` | `61.3%` (61.25000000000001 rounds up) |
| Deal count `n` | `1 new deal`, otherwise `<n> new deals` | `2 new deals` |
| Not-shown `k` | `and 1 more new deal not shown`, otherwise `and <k> more new deals not shown` | |
| Date | `run_date.isoformat()` | `2026-10-26` |
| Display title | `" ".join(deal.title.split())` | `A\nB\t C` becomes `A B C` |

Subject: `RainCheck: <count> - <date>`, for example `RainCheck: 2 new deals - 2026-10-26`, where the count is the number of shown deals (not counting `not_shown`).
No seller-supplied text ever reaches the subject, so header injection through a title is impossible by construction.

HTML body.
Every interpolated value (item name, display title, URLs, formatted numbers) goes through `html.escape(value, quote=True)`.
Inline `style` attributes only; no `<style>`, `<script>`, `<link>`, `<iframe>`, `<form>`, `<object>`, `<embed>`, no `class`, and no event-handler attributes.
Reference shape (whitespace and exact style values are the implementer's choice; the criteria test structure, not bytes):

```html
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>RainCheck: 2 new deals - 2026-10-26</title></head>
<body style="margin:0;padding:16px;font-family:Arial,Helvetica,sans-serif;color:#222222;">
<div style="max-width:600px;margin:0 auto;">
<h1 style="font-size:20px;">RainCheck: 2 new deals - 2026-10-26</h1>
<h2 style="font-size:16px;">rick owens ramones</h2>
<table width="100%" cellpadding="6" style="border-collapse:collapse;width:100%;">
<tr><th>Image</th><th>Listing</th><th>Price</th><th>Market</th><th>Discount</th></tr>
<tr>
  <td><a href="URL"><img src="IMAGE" alt="TITLE" width="100" height="100" style="display:block;border:0;width:100px;height:100px;"></a></td>
  <td><a href="URL">TITLE</a></td>
  <td>¥50,000<br>$310.00</td>
  <td>$800.00</td>
  <td>61.3%</td>
</tr>
</table>
<p>and 2 more new deals not shown</p>   <!-- only when not_shown > 0 -->
</div></body></html>
```

- The image cell holds a linked `<img>` only when `is_safe_http_url(image_url)`; otherwise it holds the text `No image` and nothing else.
  The `<img>` has exactly the attributes `src`, `alt`, `width`, `height`, `style`; `alt` is the display title, so the row still makes sense when the mail client blocks remote images.
- The listing cell holds `<a href="URL">TITLE</a>` only when `is_safe_http_url(url)`; otherwise it holds the escaped display title as plain text, and the image (if any) is not linked either.
  The parser always builds `https://` listing URLs, so this branch is defense in depth.
- An unsafe URL never appears anywhere in the HTML or the text part.
- Images are remote `<img>` tags pointing at Yahoo's thumbnail CDN; nothing is attached or embedded, which keeps the message small.
- Width: the content sits in a `max-width:600px` container, the conventional email width that fits Gmail's reading pane and phones.

Text body (exact):

```
RainCheck: 2 new deals - 2026-10-26

rick owens ramones (market price $800.00)

1. Rick Owens Ramones High Top 42
   ¥50,000 ($310.00) - 61.3% below market
   https://auctions.yahoo.co.jp/jp/auction/t1000000001

2. Rick Owens Ramon Sneakers
   ¥100,000 ($620.00) - 22.5% below market
   https://auctions.yahoo.co.jp/jp/auction/t1000000004
```

Rules: the subject line, a blank line, then per section a header `<item name> (market price <market>)`, a blank line, and per deal three lines (`<n>. <display title>`, three spaces plus the price line, three spaces plus the URL) followed by a blank line.
Numbering restarts at 1 in each section.
The URL line is omitted when the URL is unsafe.
When `not_shown > 0`, the not-shown sentence follows as its own line, then a blank line.
The text ends with exactly one `\n` (no trailing blank line).
Nothing in the text part is HTML-escaped.

#### `src/yahoo_scrape.py`

```python
YEN_TO_USD = 0.0062          # RC-08 replaces this with a live rate
TOP_DEALS_PER_ITEM = 5
NO_DEALS_LINE = "No new deals; no email sent."

def find_item_deals(item: TrackedItem, listings: Sequence[YahooListing], skip_urls: Collection[str]) -> list[Deal]
def build_sections(items: Sequence[TrackedItem], listings: Sequence[YahooListing],
                   known_urls: Collection[str], per_item_limit: int = TOP_DEALS_PER_ITEM) -> list[ItemDeals]
def main() -> None
```

`report_item_deals` is removed.
Both new functions are pure: no database, network, or print.

`find_item_deals(item, listings, skip_urls)`:

1. `market = item.effective_price_usd`; if `None`, return `[]`.
2. For each listing in input order: skip it unless `title_matches(listing.title, item.keywords, item.exclude_keywords)`; skip it if `listing.url in skip_urls`.
3. `price_usd = listing.price_yen * YEN_TO_USD`; `discount = (market - price_usd) / market * 100`; keep it only if `discount > 0` (same rule as today).
4. Build a `Deal` with `item_id=item.id`, `brand_id=item.brand_id`, `market_price_usd=market`, and the listing's `url`, `title`, `price_yen`, `image_url`.
5. Return the deals sorted by `discount_pct` descending with a stable sort, so equal discounts keep page order (newest first).

`build_sections(items, listings, known_urls, per_item_limit)`:

1. `claimed = set()`.
2. For each item in the given order (`get_tracked_items` returns id order): `deals = find_item_deals(item, listings, set(known_urls) | claimed)`; add every returned URL to `claimed`, including the ones about to be cut.
3. If `deals` is non-empty, append `ItemDeals(item.name, item.effective_price_usd, tuple(deals[:per_item_limit]), max(0, len(deals) - per_item_limit))`.
4. Return the sections; items with no new deals, or no usable price, produce no section.

The claim in step 2 means a listing that is a deal for two items appears only under the first, as today (where the first item's `insert_listing` made it "seen" for the second).

`main()`:

```python
def main():
    try:
        items = get_tracked_items()                     # schema check before any network call (RC-03b)
        if not items:
            print(NO_ITEMS_LINE)                        # unchanged RC-03b text
            return
        listings = parse_listings(fetch_yahoo_page(RICK_OWENS_URL))   # unchanged, errors propagate
        for item in items:
            if item.effective_price_usd is None:
                print(f"Skipping {item.name}: no usable market price.")
        known = get_known_listing_urls(listing.url for listing in listings)
        digest = build_digest(build_sections(items, listings, known), datetime.now().astimezone().date())
        if digest is None:
            print(NO_DEALS_LINE)
            return
        if not send_email(digest.subject, digest.html, digest.text):
            n = len(digest.deals)
            print(f"Error: digest email not sent, so its deals were not recorded and will be offered again next run ({n} deal{'' if n == 1 else 's'}).", file=sys.stderr)
            raise SystemExit(1)
        for deal in digest.deals:
            insert_listing(deal.brand_id, deal.url, deal.title, deal.price_yen, deal.image_url)
    except SchemaVersionError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)
```

- Exactly one `send_email` call per run when there is at least one new deal, and none otherwise.
- The per-deal console prints (`Title:`, `Price:`, `Product Link:`) and the `--- Yahoo Auctions Update for: ... ---` line are removed; dry run shows the whole digest instead, and the real path prints `Email sent!` as today.
- The run date is the machine's local date (on Lambda that is UTC).
- Only shown deals are recorded; deals cut by the limit stay unrecorded and are eligible again next run (Open question 2).
- If `insert_listing` raises after a successful send (for example "database is locked"), the exception propagates; deals not yet recorded are emailed again next run.

#### `src/sendemail.py`

```python
def send_email(subject: str, html_body: str, text_body: str) -> bool
```

- Dry run (`is_dry_run()` unchanged): does not construct `smtplib.SMTP` or read `SENDER_PASSWORD`, prints exactly the block below, and returns `True`.

  ```
  [DRY RUN] Email not sent (RAINCHECK_DRY_RUN is set).
  To: <recipient>
  Subject: <subject>
  --- text/plain ---
  <text_body>
  --- text/html ---
  <html_body>
  ```

  `<recipient>` is chosen as today (`RECEIVER_EMAIL`, then `SENDER_EMAIL`, then `(not configured)`).
  Each line is one `print`, so the output ends with the HTML body followed by one newline (plus whatever newline the body itself ends with).
- Real path: builds `MIMEMultipart("alternative")` with `From`, `To`, `Subject`, then attaches `MIMEText(text_body, "plain", "utf-8")` first and `MIMEText(html_body, "html", "utf-8")` second.
  RFC 2046 says the last alternative is the preferred one, so clients that render HTML show the table and the rest show the text.
  UTF-8 is explicit because titles are Japanese; Python then encodes both parts as base64.
- The SMTP flow (`smtp.gmail.com`, 587, `timeout=30`, `starttls`, `login`, `sendmail`) is unchanged.
  Returns `True` after `sendmail` returns and prints `Email sent!` as today.
- Any `Exception` inside the SMTP block still prints `Error: unable to send email (<e>)` and is not raised, but now returns `False`.
- Reading `SENDER_EMAIL` / `SENDER_PASSWORD` stays outside the `try`, so a missing credential still raises `KeyError` (unchanged; nothing is recorded because `main()` never reaches the recording loop).

#### `src/db.py`

```python
def get_known_listing_urls(urls: Iterable[str]) -> set[str]
```

- Returns the subset of `urls` already stored in `listings.url`.
- Consumes `urls` once and de-duplicates it.
  If there is nothing to look up, returns `set()` without opening the database.
- Otherwise opens one connection with `closing(get_connection())`, runs `SELECT 1 FROM listings WHERE url = ?` with `(url,)` for each distinct URL (the `UNIQUE` constraint on `url` makes each lookup an index hit; at most 100 URLs per run), and never writes or commits.
- Schema errors from `get_connection()` propagate as `SchemaVersionError` as for every other function.

## Acceptance criteria

New tests are named `test_rc15_c<N>_...`.
Rewritten tests keep their old names where the behavior they guard is the same (see "Superseded tests").
"Minimal listings" means `parse_listings(brand_page_minimal.html)`; `URL_1`, `URL_4`, `X1`, `X3`, `Z2` are the constants already in `tests/test_yahoo_scrape.py`.
"The Ramones item" is the `rick owens ramones` `TrackedItem` from `seeded_db` (effective price 800.0).

### Finding deals (pure)

1. `find_item_deals(ramones_item, minimal_listings, set())` returns two `Deal`s, `URL_1` then `URL_4`.
   The first has `item_id == ramones_item.id`, `brand_id == ramones_item.brand_id`, `title == "Rick Owens Ramones High Top 42"`, `price_yen == 50000`, `price_usd == pytest.approx(310.0)`, `market_price_usd == 800.0`, `discount_pct == pytest.approx(61.25)`, `image_url == "https://auc-pctr.c.yimg.jp/i/test/1.jpg"`; the second has `discount_pct == pytest.approx(22.5)`.
2. With `skip_urls={URL_1}` the result is only the `URL_4` deal.
3. With the item replaced by `dataclasses.replace(ramones_item, exclude_keywords=("high",))` the result is only the `URL_4` deal (rewrites RC-03b criterion 9).
4. With `dataclasses.replace(ramones_item, market_price_usd=400.0)` the result is only the `URL_1` deal with `discount_pct == pytest.approx(22.5)` (rewrites RC-03b criterion 11).
5. With both prices `None` (`dataclasses.replace(..., market_price_usd=None, manual_price_usd=None)`) the result is `[]` and nothing is printed.
6. With `market_price_usd=310.0` (so `URL_1`'s discount is exactly 0) `URL_1` is not a deal.
7. Two matching listings with equal `price_yen`, built inline as `YahooListing`s, come back in input order.
8. With `dataclasses.replace(ramones_item, brand="Not In Database")` every deal's `brand_id` is still `ramones_item.brand_id` (rewrites RC-03b criterion 10).
9. `find_item_deals` and `build_sections` need no database: called with inline `TrackedItem`s while `RAINCHECK_DB_PATH` points at a file that does not exist, they return normally and the file still does not exist afterwards.

### Building sections (pure)

10. For one item matching seven inline listings that are all deals with distinct prices, `build_sections` returns one `ItemDeals` with the five largest discounts in descending order and `not_shown == 2`.
11. With two items whose keywords both match the same cheap listing, the listing appears only in the first item's section, and the second item gets no section if that was its only deal.
    This holds even when the listing was cut from the first section by `per_item_limit` (the second section still does not contain it).
12. Items with no new deals or no usable price produce no section, and the remaining sections follow the input item order.
13. A URL in `known_urls` is in no section.

### Digest builder (pure)

14. `build_digest([], d)` and `build_digest([ItemDeals("x", 800.0, ())], d)` both return `None`.
15. The subject is `RainCheck: 1 new deal - 2026-10-26` for one shown deal and `RainCheck: 3 new deals - 2026-10-26` for three shown deals spread over two sections, with `run_date=date(2026, 10, 26)`; `not_shown` does not change the count.
16. `Digest.deals` contains exactly the shown deals in display order (section order, then deal order).
17. Parsed with BeautifulSoup (`html.parser`): the document has one `<title>` equal to the subject, one `<h1>` equal to the subject, one `<h2>` per section whose text equals the item name, and one `<table>` per section.
    Each table's header row has `<th>` texts exactly `["Image", "Listing", "Price", "Market", "Discount"]`, followed by one `<tr>` per deal in order, each with exactly five `<td>`.
18. For the `URL_1` deal (market 800.0) the cells' `get_text(" ", strip=True)` are, from the second cell, `Rick Owens Ramones High Top 42`, `¥50,000 $310.00`, `$800.00`, `61.3%`; a deal with `price_yen=200000`, `price_usd=1240.0`, `market_price_usd=1500.0` renders `¥200,000 $1,240.00` and `$1,500.00`.
19. A deal with a safe image URL has, in its first cell, an `<a>` whose `href` is the listing URL containing one `<img>` whose attributes are exactly `{src, alt, width, height, style}`, with `src` equal to the image URL, `alt` equal to the display title, `width == "100"` and `height == "100"`.
    A deal with `image_url=None` has first-cell text exactly `No image` and no `<img>`.
20. The listing cell's `<a>` has `href` equal to the deal URL and text equal to the display title.
21. Escaping: with title `<script>alert("x")</script> & 'q'` and item name `<b>bold</b> & co`, the HTML contains no `<script>` and no `<b>` element, the raw HTML contains `&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &#x27;q&#x27;`, the parsed link text and `img` `alt` both equal the original title, and the `<h2>` text equals `<b>bold</b> & co`.
22. Attribute safety: an image URL `https://img.test/a.jpg?w=1&h=2"onerror="alert(1)` parses back from the `src` attribute as exactly that string, the `<img>` still has exactly the five attributes of criterion 19, and the raw HTML contains `w=1&amp;h=2&quot;onerror=&quot;alert(1)`.
23. `is_safe_http_url` is `True` for `https://auc-pctr.c.yimg.jp/i/test/1.jpg` and `HTTP://example.test/a.jpg`, and `False` for `None`, `""`, `javascript:alert(1)`, `JavaScript:alert(1)`, `data:image/png;base64,AAAA`, `vbscript:x`, `ftp://example.test/a.jpg`, `//example.test/a.jpg`, `https:a.jpg`, `https:///a.jpg`, ` https://example.test/a.jpg`, `https://example.test/a b.jpg`, `java\nscript:alert(1)`, and `https://example.test/\x00.jpg`.
24. A deal whose `image_url` is `javascript:alert(1)` renders `No image` with no `<img>`, and `javascript:` appears nowhere in the HTML or text.
    A deal whose `url` is `javascript:alert(1)` renders its title as plain text with no `<a>` anywhere in its row, its text-part block has no URL line, and `javascript:` appears nowhere in the HTML or text.
25. Across a digest built from every adversarial input in criteria 21-24, the parsed HTML has no `script`, `style`, `link`, `iframe`, `form`, `object`, or `embed` element, and no element has an attribute whose name starts with `on`.
26. A section with `not_shown=2` adds `and 2 more new deals not shown` after its table and after its last text block; `not_shown=1` adds `and 1 more new deal not shown`; `not_shown=0` adds neither.
27. For one section `rick owens ramones` (market 800.0) with the `URL_1` and `URL_4` deals and `run_date=date(2026, 10, 26)`, `Digest.text` equals the exact text body shown under "Interfaces".
28. A title `A\nB\t  C` is displayed as `A B C` in the HTML link text, the `alt`, and the text part.
29. Size and layout: 10 sections of 5 deals each, every deal with a 120-character title and a 330-character `https://` image URL containing `&`, produce `len(digest.html.encode("utf-8")) < 102_000` (Gmail clips larger messages), and the HTML contains `max-width:600px`.
30. `build_digest` prints nothing (`capsys` output is empty).

### `send_email`

31. Real path (`RAINCHECK_DRY_RUN` unset, recording fake `smtplib.SMTP`): `send_email("Subj", "<p>テスト</p>", "テスト")` returns `True`, and the message passed to `sendmail`, parsed with `email.message_from_string`, has content type `multipart/alternative`, `Subject: Subj`, and exactly two parts: `text/plain` then `text/html`, both with charset `utf-8`, whose decoded payloads (`get_payload(decode=True).decode("utf-8")`) equal `テスト` and `<p>テスト</p>`.
32. When the fake's `login` or `sendmail` raises `smtplib.SMTPException("boom")`, or its constructor raises `OSError("down")`, `send_email` returns `False`, does not raise, and prints a line starting with `Error: unable to send email (`.
33. Dry run with no email variables set: `send_email("Subj", "<b>H</b>", "T")` returns `True`, never constructs `smtplib.SMTP`, and prints exactly `[DRY RUN] Email not sent (RAINCHECK_DRY_RUN is set).\nTo: (not configured)\nSubject: Subj\n--- text/plain ---\nT\n--- text/html ---\n<b>H</b>\n` (rewrites RC-01 criterion 18).
34. RC-01 criteria 17, 19, 20, 21 and RC-03b criterion 47 still hold with three-argument calls (SMTP host, port, `timeout=30`, `starttls`, `login`, recipient choice).

### `get_known_listing_urls`

35. On `seeded_db` after `insert_listing` of `https://x/1` and `https://x/2`, `get_known_listing_urls(["https://x/1", "https://x/3", "https://x/1"])` returns `{"https://x/1"}`, and a generator argument gives the same result.
36. `get_known_listing_urls([])` returns `set()` without opening the database: it succeeds when `RAINCHECK_DB_PATH` points at a file that does not exist and creates no file.
37. It writes nothing: the database file's SHA-256 (`tests/dbhelpers.sha256`) is unchanged, and no connection is leaked under the suite's strict `ResourceWarning` filter.
38. On `legacy_db` it raises `SchemaVersionError`.

### `main()` end to end (`fake_http` serving the minimal fixture, `seeded_db`)

39. (Rewrites RC-01 criterion 14, name kept.)
    With the working directory set to `tmp_path`, `main()` calls `send_email` exactly once; the subject matches `^RainCheck: 2 new deals - \d{4}-\d{2}-\d{2}$`; the HTML has exactly one `<h2>`, `rick owens ramones`, with the `URL_1` row before the `URL_4` row; and `fake_http.calls` is one `GET` to `RICK_OWENS_URL` with `timeout` 30.
40. (Rewrites RC-01 criterion 45, name kept.)
    After that run `listings` holds exactly `URL_1` at 50000 yen and `URL_4` at 100000 yen, with `brand_id` equal to the Ramones item's.
41. (Rewrites RC-01 criterion 47, name kept.)
    A second `main()` sends nothing, prints `No new deals; no email sent.`, exits normally, and leaves `listings` unchanged.
42. Record after send: a fake `send_email` that, when called, reads the `listings` table and finds it empty, then returns `True`; after `main()` both rows exist.
43. Failed send: a fake `send_email` returning `False` makes `main()` raise `SystemExit` with code 1, write exactly one stderr line `Error: digest email not sent, so its deals were not recorded and will be offered again next run (2 deals).`, and insert no rows.
    A following `main()` with a succeeding fake sends the same two deals.
44. A listing already in `listings` before the run (insert `URL_1` with `insert_listing` first) is not shown: the subject says `1 new deal`.
45. Top-5 per item end to end: with `parse_listings` monkeypatched to return seven inline Ramones deals, the digest shows five, says `and 2 more new deals not shown`, and only those five URLs are recorded; the next run's digest shows exactly the remaining two.
46. With the Ramones item's prices set to `NULL`, `main()` prints `Skipping rick owens ramones: no usable market price.` and `No new deals; no email sent.`, sends nothing, and inserts nothing (rewrites RC-07 criterion 25 and RC-03b criterion 12).
47. With `fake_http` serving `brand_page_malformed.html`, the single digest lists `X3`, then `X1`, then `Z2`; the HTML has exactly two `<img>` elements and the `X3` row's first cell is `No image`; the rows inserted are `X1`, `Z2` (40000 yen) and `X3` (`image_url` `NULL`) (rewrites RC-07 criterion 24).
48. With `pod shorts` set inactive, `find_item_deals` (spied) is called only for `rick owens ramones` (rewrites RC-03b criterion 14).
49. With `db.CATALOG_PATH` pointing at a missing file, `main()` still sends one digest (rewrites RC-03b criterion 15).
50. Dry run end to end (real `send_email`, `RAINCHECK_DRY_RUN=1`, `smtplib.SMTP` patched to raise if constructed): stdout contains `[DRY RUN] Email not sent`, the subject line, `--- text/plain ---`, `--- text/html ---`, and `URL_1`; rows are recorded (Open question 8).
    When there are no new deals, stdout contains `No new deals; no email sent.` and no `[DRY RUN]`.
51. RC-07 criteria 28 and 29 (HTTP 503 and old layout: exception, no email, no rows) and RC-03b criteria 13, 16-19 and 21 still pass unchanged.

### Live fixture

52. For the live fixture, with a `TrackedItem` built inline from the real catalog's `rick owens ramones` keywords and exclude keywords and a `manual_price_usd` of 100000.0, `build_digest(build_sections(...), date(2026, 10, 26))` produces HTML in which every `<img>` `src` equals the corresponding deal's `image_url` exactly (each live URL contains `&`), and every `<a>` `href` starts with `https://`.

### Whole change

53. `python3 -m pytest` passes offline with the strict warning filters in `pyproject.toml`.
54. (Reviewer) `src/yahoo_scrape.py` no longer contains `report_item_deals`, `Daily Refresh`, or `"<br>".join`; `src/digest.py` imports nothing from `db`, `yahoo_scrape`, `sendemail`, `requests`, or `smtplib`; every interpolation into HTML in `src/digest.py` goes through `html.escape(..., quote=True)`.
55. (Reviewer) The README "What it does" says one digest per run with the top 5 new deals per item and no email when there are none; "Project structure" lists `digest.py`; the dry-run paragraph says it prints the whole digest.
56. (Verifier) Against a throwaway copy of the database with `RAINCHECK_DRY_RUN=1`: run `python3 src/yahoo_scrape.py` once (one live Yahoo request) and report the printed subject and the number of rows; save the printed HTML to a temporary file, open it in a browser, and report that the images load and the table reads correctly at 600 px and at phone width.
    Run it a second time and confirm `No new deals; no email sent.` and no row change.

## Edge cases

- **Empty results:** no active items prints the RC-03b line (unchanged).
  An empty Yahoo page, or no matching cheap listings, prints `No new deals; no email sent.` and sends nothing (criterion 41).
  `get_known_listing_urls` with no URLs does not open the database (criterion 36).
- **`None` or missing values:** an item without a usable price prints the skip line and contributes no section (criterion 46).
  `image_url` `None` renders `No image` (criterion 19).
- **Network failure or bad HTTP status:** Yahoo errors propagate before any database read of listings, any email, or any write (criterion 51).
  An SMTP failure returns `False`, nothing is recorded, and the run exits 1 so an unattended run shows as failed (criterion 43).
  A crash after the send but before recording finishes re-sends those deals once on the next run: at-least-once delivery, chosen over losing deals (Open question 1).
- **Malformed input:** titles with markup, quotes, and `&`; image URLs with quotes or non-http schemes; whitespace and control characters (criteria 21-25, 28).
- **Duplicates:** one listing matching two items appears once, under the first item (criterion 11).
  A listing stored by an earlier run is not shown again (criterion 44); a price drop on a stored listing is still not re-alerted (RC-05).
  Two runs at the same time could both email the same deals; `INSERT OR IGNORE` still keeps one row.
- **Large digests:** see "Known issues".

### Known issues (not fixed here)

| Issue | Owner |
| --- | --- |
| `send_email` still swallows exceptions and reports them with `print`; it now returns `False` so the caller can react. | RC-10 |
| A digest is not split if it ever exceeds Gmail's roughly 102 KB clipping size; at 5 deals per item that needs well over 10 items (criterion 29). | future item if the watchlist grows |
| Remote images are hotlinked from Yahoo's CDN; if Yahoo ever blocks hotlinking, the `alt` text and `No image`-style layout still show the deal. | revisit in RC-11 if seen |

## Test plan

### Fixtures

No new fixture files.

- `brand_page_minimal.html`, `brand_page_malformed.html`, `brand_page_live.html`, `tests/fixtures/catalog.json` and the conftest fixtures `seeded_db`, `temp_db`, `legacy_db`, `db_conn`, `fake_http`, `test_catalog`, `fixtures_dir`.
- `sent_emails` in `tests/conftest.py` changes: it monkeypatches `yahoo_scrape.send_email` with a function taking `(subject, html_body, text_body)`, appends that triple, and returns `True`.
  Its type becomes `list[tuple[str, str, str]]`; tests asserting `sent_emails == []` keep working.
- Tests needing a failing or inspecting sender (criteria 42, 43) monkeypatch `yahoo_scrape.send_email` locally.
- Pure tests build `TrackedItem`, `YahooListing`, `Deal`, and `ItemDeals` inline, so each test shows its whole input.
  A small helper in `tests/test_digest.py` makes a `Deal` with defaults.
- HTML assertions parse with BeautifulSoup (already a dependency); MIME assertions parse with `email.message_from_string` and decode payloads, because UTF-8 parts are base64-encoded.
- `main()` date checks use a regex, never `date.today()`, so a test running across midnight cannot flake.

### Superseded tests

| Test | Change |
| --- | --- |
| `tests/conftest.py` `sent_emails` | three arguments, returns `True` |
| `test_c45_inserts_only_the_two_matching_cheap_listings` | runs `main()`; criterion 40 |
| `test_c46_one_email_sorted_by_discount_with_percentages` | runs `main()`; asserts digest subject, row order, and the `61.3%` / `22.5%` discount cells instead of `cheaper than market` |
| `test_c47_second_run_inserts_no_new_rows` | runs `main()` twice; criterion 41 |
| `test_rc07_c24_malformed_fixture_pipeline_rows_and_email` | runs `main()`; criterion 47 (`<img>` count and `No image` instead of `Image: ` lines) |
| `test_rc07_c25_null_market_price_skips_item` | runs `main()`; criterion 46 |
| `test_rc03b_c9_exclude_keywords_from_the_item_apply` | `find_item_deals`; criterion 3 |
| `test_rc03b_c10_brand_comes_from_the_item_not_a_name_lookup` | `find_item_deals`; criterion 8 |
| `test_rc03b_c11_effective_price_of_the_item_drives_the_deal_math` | `find_item_deals`; criterion 4 |
| `test_rc03b_c12_item_without_usable_price_is_skipped_with_a_line` | `main()`; criterion 46 |
| `test_c14_main_works_from_unrelated_cwd_and_emails_once_per_item` | one digest; criterion 39 |
| `test_rc03b_c14_main_skips_inactive_items` | spy on `find_item_deals`; criterion 48 (pod shorts has no deals in the minimal fixture, so the subject list no longer discriminates) |
| `test_rc03b_c15_main_does_not_read_catalog_json` | one digest; criterion 49 |
| `tests/test_sendemail.py` `test_c17_...`, `test_c19_...`, `test_c21_...`, `test_rc03b_c47_...` | three-argument calls; assertions otherwise unchanged |
| `tests/test_sendemail.py` `test_c18_...` | new exact output; criterion 33 |

Unchanged: `test_rc03b_c13_...` (still records `[URL_1]`), `test_rc03b_c16_...` through `test_rc03b_c19_...`, `test_rc03b_c21_...`, `test_rc07_c26_...` through `test_rc07_c29_...`, `test_c20_...`, and every test in `tests/test_db.py`, `tests/test_yahoo_parse.py`, `tests/test_matching.py`, `tests/test_ebay_pricing.py`, and the existing live-fixture tests.

Before implementation, the new and rewritten tests fail with `ImportError` (`digest`), `AttributeError` (`find_item_deals`, `get_known_listing_urls`), or `TypeError` (three-argument `send_email`).

### Test files

| File | Covers |
| --- | --- |
| `tests/test_digest.py` (new) | 14-30 |
| `tests/test_yahoo_scrape.py` | 1-13, 39-51 |
| `tests/test_sendemail.py` | 31-34 |
| `tests/test_db.py` | 35-38 |
| `tests/test_live_fixtures.py` | 52 |

## Shared-file conflicts

Yes: `src/db.py`, one new read-only function, if Open question 1 is answered as recommended.
`src/schema.sql` and `src/catalog.json` are not touched.
So RC-15 must not run in parallel with RC-04, RC-05, RC-06, RC-13, RC-14, or RC-16, which touch `src/db.py`.
It also touches `src/yahoo_scrape.py`, shared with RC-08, RC-10, RC-13, and RC-16 (not a formally shared file, but a guaranteed merge conflict), and `tests/conftest.py`.
If Option B of Open question 1 is chosen, `src/db.py` is not touched.

## Open questions

**Resolved 2026-10-26:** the user approved this spec and chose the recommended option for every question below: deals are recorded as seen only after a successful send and only the deals shown, top 5 per item grouped by item in watchlist order, the subject `RainCheck: N new deal(s) - YYYY-MM-DD`, a plain-text part, exit 1 with an `Error:` line on a failed send, and a dry run still records deals as seen.

1. **When is a deal recorded as seen?**
   - Option A (recommended): find deals with a read-only lookup, send the digest, and record the shown deals only after `send_email` returns `True`.
     A failed send loses nothing, and the next run offers the same deals again.
     A crash between the send and the end of recording re-sends those deals once (at-least-once delivery); a duplicate email is a minor annoyance, while a lost deal on an item you are hunting is the failure that matters.
     Tradeoff: adds `get_known_listing_urls` to the shared `src/db.py`, so RC-15 runs alone, and `send_email` must report success.
   - Option B: keep today's insert-before-send, and leave it to RC-10.
     Tradeoff: no `src/db.py` change and a smaller diff, but `find_item_deals` cannot be pure, and until RC-10 any SMTP hiccup silently drops every deal of that run, which one digest makes worse than today.
2. **Which deals are recorded: only the shown ones, or every new deal found?**
   Recommendation: only the shown ones, so "seen" means "emailed" and a deal cut by the top-5 limit is offered again next run (if still listed and still cheap), with the digest saying how many were cut.
   Tradeoff: on a day with many deals the leftover ones arrive a day later, and a long-lived listing could wait several runs behind better ones.
   Alternative: record every new deal as today, which drops number 6 and below forever.
3. **Where is the top-5 cut?**
   Recommendation: per item, as today, so one item with many cheap listings cannot crowd every other item out of the digest; the size stays bounded (5 times the number of items, criterion 29).
   Alternatives: top 5 across the whole digest (shortest email, but a hot item hides the rest), or no cut (complete, but unbounded size).
4. **How is the digest ordered?**
   Recommendation: grouped by item in item id order (watchlist order), best discount first within each item, with the market price on every row.
   Tradeoff: the single best deal of the day might be in the second section.
   Alternative: one ranked list across items with an extra "Item" column; better for scanning by discount, but it repeats the item name on every row and makes the per-item cut and the "not shown" line awkward.
5. **Subject line?**
   Recommendation: `RainCheck: <n> new deal(s) - <YYYY-MM-DD>`, using the machine's local date.
   The count tells you from the inbox whether it is worth opening, and the date gives each day a distinct subject, so Gmail does not thread every digest into one ever-growing conversation (which also counts toward clipping).
   Tradeoff: on Lambda the local date is UTC, so an evening run in US time shows tomorrow's date.
   Alternative: a fixed subject like `RainCheck deals`, which threads all digests together.
6. **Plain-text alternative or HTML only?**
   Recommendation: `multipart/alternative` with a text part first and HTML second.
   It is the standard structure, reads correctly in text-only clients and notification previews, and an HTML-only message is a mild spam signal.
   Tradeoff: two renderings to keep consistent, covered by criteria 27 and 28.
7. **Exit code when the send fails?**
   Recommendation: print one `Error:` line to stderr and exit 1, so a failed run is visibly failed (and, after RC-11/RC-12, raises the Lambda error alarm).
   Tradeoff: starts a sliver of RC-10's error policy; RC-10 may later replace the print with logging.
   Alternative: exit 0 and rely on `send_email`'s printed error, which hides the failure from any scheduler.
8. **Does a dry run record deals as seen?**
   Recommendation: yes (dry-run `send_email` returns `True`), matching what the README already says: dry run only affects email, so pair it with a throwaway `RAINCHECK_DB_PATH`.
   This also lets the verifier check that a second run sends nothing.
   Tradeoff: a dry run against the real database still "uses up" those deals.
   Alternative: dry run writes nothing, which is safer against the real database but makes the second-run behavior impossible to verify end to end in dry run.

## Rollback

- **Before merge:** close the PR; nothing else changed.
- **After merge:** revert the merge commit with a new PR.
  There is no schema change, and rows written by RC-15 have the same columns and URL format as before, so the old code reads them as "seen" with no data fix.
  The only visible effect of rolling back is the return of one email per item per run.

## Amendments

### Amendment 1 (2026-10-26): review hardening

- `src/yahoo_scrape.py` `_parse_record`: a record whose `title` or `imageUrl` cannot be encoded as UTF-8 (for example a lone surrogate such as `\ud800`, which JSON can produce) is invalid: the record is skipped and counted in the existing skip warning, and an `imageUrl` that cannot be encoded is treated as invalid for the whole record.
  Without this, one such listing makes `send_email` (or `insert_listing`) raise `UnicodeEncodeError` on every run while it stays on the page, so no digest is ever sent.
- New criterion 57: `parse_listings` on a page with one valid record and one record whose title contains a lone surrogate returns only the valid listing and logs `Skipped 1 of 2 Yahoo listing records: 1 invalid, 0 duplicate`; `main()` on such a page sends one digest with the valid deal.
- Test hardening: the criterion 30 purity test parses `src/digest.py` with `ast` and asserts no import of `db`, `requests`, `smtplib`, `sqlite3`, `sendemail`, or `yahoo_scrape`; the criterion 54 test also asserts `yahoo_scrape`'s source contains neither `Daily Refresh` nor `"<br>".join`; a new test makes `insert_listing` raise on the second call after a successful send and asserts the exception propagates, the first row stays, and the next run sends only the second deal.

### Amendment 2 (2026-10-26, approved by the user): phone-friendly layout

Rendering the verified digest in a browser showed two layout problems: the document had no viewport tag, so phones laid it out 980px wide and shrank it, and five columns left the title about 140px on a phone, wrapping a typical title over nine lines with the headers centered over left-aligned data.
The user approved a three-column layout, prototyped and checked at 375px and 1024px wide (no horizontal overflow at 375px).
The plain-text part, the subject, the escaping rules, and `is_safe_http_url` are unchanged.

- The `<head>` gains `<meta name="viewport" content="width=device-width, initial-scale=1">` after the charset tag.
- Each item heading is `<h2>` with the escaped item name followed by `<span>` containing `(market $X.XX)` in a muted color, so the market price is shown once per item instead of in every row.
- Each item table has `cellspacing="0"`, `cellpadding="8"`, and three columns: `Image` (100px wide, left-aligned header), `Listing` (left-aligned header), and `Price` (right-aligned header).
  The header row and each deal row have a light bottom border.
- Each deal row: the image cell (unchanged content, `vertical-align:top`, 100px wide); the listing cell (unchanged link-or-text rule, `vertical-align:top`, `line-height:1.4`); and the price cell, right-aligned, `vertical-align:top`, `white-space:nowrap`, containing the yen price, `<br>`, the USD price, `<br>`, and `<strong>` with the discount formatted as `N.N% off` in green.
- Criteria 17-20 change accordingly: the header row is `Image | Listing | Price`; each deal row has exactly three cells; criterion 18's cell texts become the listing title and `¥50,000 $310.00 61.3% off` (the price cell's `get_text(" ", strip=True)`); the market price appears in the `<h2>`'s `<span>` as `(market $800.00)`.
  Criterion 25 additionally allows `meta`, `span`, and `strong` elements and `name`/`content` attributes on the viewport `meta`.
- The orchestrator's mockup that was checked in the browser is the reference for the rendered result.


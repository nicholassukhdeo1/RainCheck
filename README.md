# RainCheck

A personal web-scraping project that hunts for underpriced fashion listings and emails me the best daily deals.

I built this to get hands-on with web scraping, HTTP requests, and automating a task I actually cared about - finding grail pieces (like Rick Owens Ramones) below their typical resale price.

## What it does

- Lets me build a watchlist of items to track, each with a market price and keywords
- Sets each item's market price to the median price of matching active eBay listings, fetched through the eBay Browse API
- Scrapes the Rick Owens brand page on Yahoo Auctions Japan (sorted newest-first)
- Filters listings by keyword, converts prices from JPY to USD, and keeps only those below market
- Ranks the remaining listings by discount and remembers which ones it has already seen
- Emails the top 5 best deals as an HTML summary

## Tech

- Python
- SQLite (the watchlist, seen listings, and market prices)
- eBay Browse API (market prices)
- BeautifulSoup (HTML parsing)
- Requests (HTTP)
- smtplib (email delivery)

## Project structure

```
src/
├── db.py            # every database function; run it to create and seed the database
├── schema.sql       # the table definitions
├── yahoo_scrape.py  # scrape Yahoo Auctions Japan and email the best deals
├── ebay_pricing.py  # compute each item's market price from eBay and store it
├── matching.py      # the keyword matcher both jobs share (include and exclude keywords)
├── sendemail.py     # sends the HTML email summary
├── setup.py         # interactive: build your watchlist, saved to catalog.json
├── catalog.json     # the watchlist of items being tracked
└── raincheck.db     # the SQLite database (local to each machine, not committed)
tests/               # offline test suite
scripts/
└── capture_fixtures.py  # saves real pages for the test fixtures
docs/                # backlog and feature specs
```

Each watchlist item is a dictionary (name, brand, size, search query, market price, keywords).
On each run the scraper pulls current listings, compares them against the item's market price, and stores the listings it has already seen in the SQLite database so repeat runs only surface new finds.

## Setup

1. Create and activate a virtual environment, then install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in your Gmail and eBay values (this file is gitignored and must never be committed):
   ```
   SENDER_EMAIL=you@gmail.com
   SENDER_PASSWORD=your_gmail_app_password
   RECEIVER_EMAIL=you@gmail.com
   EBAY_CLIENT_ID=your_ebay_client_id
   EBAY_CLIENT_SECRET=your_ebay_client_secret
   ```
   `SENDER_PASSWORD` is a Gmail [app password](https://myaccount.google.com/apppasswords), not your normal account password.
   `load_dotenv()` searches upward from the script's own folder (`src/`), so the `.env` file can live in `src/` or in the repository root.
   (Under a debugger it searches from the current directory instead.)
   See "Configuration" below for the optional variables.
3. Create and seed the database:
   ```bash
   python3 src/db.py --seed
   ```

## Usage

1. Edit `src/catalog.json` to change what is tracked.
2. Run `python3 src/db.py --seed` to copy the catalog into the database.
3. Update the market prices (this needs the eBay credentials), then scrape and email the best deals:

```bash
python3 src/ebay_pricing.py   # update market prices from eBay
python3 src/yahoo_scrape.py   # scrape and email the best deals
```

Both can be run from any directory.

- Both jobs read the items only from the database, never from `catalog.json`, so a `catalog.json` edit takes effect only after you seed.
- Both jobs ignore listings whose title contains an exclude keyword.
- Matching is case-insensitive substring matching, so `low` also matches `yellow` (until RC-17 makes it word-aware).
- eBay is searched with the item's `search_query`, or with the item name when it has none.
- If a job finds an outdated or missing database, it prints one `Error:` line naming the command that fixes it and stops before any network request.

`src/setup.py` is the exception.
It reads and writes `catalog.json` relative to the current directory, so run it from inside `src/`:

```bash
cd src
python3 setup.py         # build your watchlist
```

After adding items with it, run `python3 src/db.py --seed` so the new items reach the database.
But first fix the entries `setup.py` wrote in `catalog.json`.
Its `keywords` are placeholders that need fixing, and its `search_query` uses `+` between words, which seeding rejects, so rewrite it with spaces (or remove it).
(RC-14 replaces `setup.py`.)

## Database

- The database lives at `src/raincheck.db` by default, or at the path in `RAINCHECK_DB_PATH`.
- It is local to each machine and is not committed.
  `.gitignore` excludes `*.db` and the SQLite side files (`-journal`, `-wal`, `-shm`).
- Create or rebuild it with `python3 src/db.py --seed`.
- Rebuilding from scratch loses two things.
  The record of listings already seen is gone, so current deals are emailed again on the next run.
  The eBay-derived market prices are gone too, so items fall back to `market_price` from `catalog.json` until `ebay_pricing.py` runs again.
- Seeding (`python3 src/db.py --seed`) syncs `catalog.json` into the database.
  Edits to an item (price, search query, keywords, exclude keywords) propagate, and an item you remove from the catalog is deactivated, not deleted, so its history is kept.
  Adding it back reactivates the same row.
- `market_price` in `catalog.json` is only a fallback.
  It is stored as `manual_price_usd`, and it never overwrites the eBay-derived `market_price_usd`.
  Deal math uses the eBay price when there is one and the catalog price otherwise.
- Seeding checks the whole catalog first and reports every problem per entry (for example `entry 2 ("pod shorts"): market_price must be a number greater than 0`).
  If anything is invalid, nothing is written.
- The database has a schema version.
  After you update RainCheck, back up the database and run `python3 src/db.py` to upgrade it in place without losing listings or eBay prices.
  The jobs refuse to run on an outdated database and tell you to do this, so nothing is changed behind your back.
- To back it up safely, run `sqlite3 src/raincheck.db ".backup '<destination>'"`.
  This makes a consistent copy even if another process is writing, so store it outside the repository.
- Warning: checking out a commit from before this change (before the RC-02 merge) replaces your local database with the old committed copy without asking.
  Back it up first.

## Configuration

Two optional environment variables change how RainCheck behaves.
Set them in your shell or in `.env`.

- `RAINCHECK_DB_PATH` - path to the SQLite database file.
  When unset or empty, RainCheck uses `src/raincheck.db`.
  A relative path is resolved against the directory you run the command from.
  The parent directory must already exist.
- `RAINCHECK_DRY_RUN` - when set to anything except `0`, `false`, `no`, `off`, or empty, emails are printed instead of sent.
  Unrecognized values (such as a typo like `ture`) also count as on, so a typo never sends real email.
  Dry run only affects email.
  Database writes still happen, so a dry run against your real database marks listings as seen and they will not be emailed later.
  Pair `RAINCHECK_DRY_RUN=1` with a throwaway `RAINCHECK_DB_PATH`.

eBay pricing also needs `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` (see `.env.example`).

## Running tests

The test suite runs fully offline against a throwaway database.
From the repository root:

```bash
pip install -r requirements-dev.txt
python3 -m pytest
```

## Notes

This is a personal learning project, built to sharpen my Python and get comfortable with scraping and automation end-to-end.

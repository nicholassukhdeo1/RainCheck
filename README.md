# RainCheck

A personal web-scraping project that hunts for underpriced fashion listings and emails me the best daily deals.

I built this to get hands-on with web scraping, HTTP requests, and automating a task I actually cared about - finding grail pieces (like Rick Owens Ramones) below their typical resale price.

## What it does

- Lets me build a watchlist of items to track, each with a market price and keywords
- Scrapes the Rick Owens brand page on Yahoo Auctions Japan (sorted newest-first)
- Filters listings by keyword, converts prices from JPY to USD, and keeps only those below market
- Ranks the remaining listings by discount and remembers which ones it has already seen
- Emails the top 5 best deals as an HTML summary
- Includes an experimental eBay scraper as a second data source

## Tech

- Python
- BeautifulSoup (HTML parsing)
- Requests (HTTP)
- smtplib (email delivery)

## Project structure

```
src/
├── setup.py         # interactive: build your watchlist, saved to catalog.json
├── yahoo_scrape.py  # scrape Yahoo Auctions Japan and email the best deals
├── ebay_scrape.py   # experimental eBay scraper
├── sendemail.py     # sends the HTML email summary
└── catalog.json     # the watchlist of items being tracked
```

Each watchlist item is a dictionary (name, brand, size, search query, market price, keywords).
On each run the scraper pulls current listings, compares them against the item's market price, and stores seen listings in a per-item JSON file so repeat runs only surface new finds.

## Setup

1. Install dependencies:
   ```bash
   pip install requests beautifulsoup4 python-dotenv
   ```
2. Copy `.env.example` to `.env` and fill in your Gmail credentials (this file is gitignored and must never be committed):
   ```
   SENDER_EMAIL=you@gmail.com
   SENDER_PASSWORD=your_gmail_app_password
   RECEIVER_EMAIL=you@gmail.com
   ```
   `SENDER_PASSWORD` is a Gmail [app password](https://myaccount.google.com/apppasswords), not your normal account password.

## Usage

Run from inside the `src/` folder:

```bash
cd src
python setup.py          # build your watchlist
python yahoo_scrape.py   # scrape and email the best deals
```

## Notes

This is a personal learning project, built to sharpen my Python and get comfortable with scraping and automation end-to-end.

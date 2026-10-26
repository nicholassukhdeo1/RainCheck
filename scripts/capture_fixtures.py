"""
One-off script that records a live Yahoo page and a live eBay search
response into tests/fixtures/, so the hand-written fixtures can be
checked against reality (tests/test_live_fixtures.py).

Run by the user only, from the repository root:
    python3 scripts/capture_fixtures.py

It needs EBAY_CLIENT_ID and EBAY_CLIENT_SECRET (from .env or the shell)
and live network access. It never touches a database or sends email.
"""
import json
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# the src/ modules use flat imports, so put src/ on the path like pytest does
sys.path.insert(0, str(ROOT / "src"))

# override=False (the default) keeps variables already set in the shell
load_dotenv(ROOT / "src" / ".env")
load_dotenv(ROOT / ".env")

from ebay_pricing import get_ebay_token  # noqa: E402
from yahoo_scrape import RICK_OWENS_URL  # noqa: E402

YAHOO_OUT = ROOT / "tests" / "fixtures" / "yahoo" / "brand_page_live.html"
EBAY_OUT = ROOT / "tests" / "fixtures" / "ebay" / "search_live.json"

# every request gets a timeout so a hung server cannot freeze the script
TIMEOUT = 30


def capture_yahoo():
    response = requests.get(RICK_OWENS_URL, timeout=TIMEOUT)
    response.raise_for_status()
    YAHOO_OUT.parent.mkdir(parents=True, exist_ok=True)
    # raw bytes, so the saved page is exactly what Yahoo sent
    YAHOO_OUT.write_bytes(response.content)


def capture_ebay():
    # the token stays in memory only: never printed, never written to disk
    # note: the token POST inside get_ebay_token has no timeout yet (RC-10 adds it)
    token = get_ebay_token()
    # same headers and params as ebay_market_price
    response = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        params={"q": "rick owens ramones", "limit": 50},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    # drop seller usernames/feedback and itemLocation (partial seller postal
    # codes) so no personal data is committed
    for entry in data.get("itemSummaries", []):
        entry.pop("seller", None)
        entry.pop("itemLocation", None)

    EBAY_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(EBAY_OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return len(data.get("itemSummaries", []))


def main():
    capture_yahoo()
    count = capture_ebay()
    print(f"Wrote {YAHOO_OUT}")
    print(f"Wrote {EBAY_OUT} ({count} item summaries)")


if __name__ == "__main__":
    main()

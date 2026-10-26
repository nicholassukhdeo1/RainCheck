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
import re
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


# Yahoo is a Next.js page: the listing data lives in one JSON blob inside this script tag.
# A regex (not BeautifulSoup) so everything outside the blob is written back byte-for-byte.
NEXT_DATA_RE = re.compile(
    r'(<script[^>]*\bid="__NEXT_DATA__"[^>]*>)(.*?)(</script>)', re.DOTALL
)


def _sanitize(node, removed):
    """
    Walks the parsed JSON at any depth and:
    - removes `seller` and `prefectureCode` from every dict that looks like a listing (has auctionId)
    - empties every `setCookies` value (Yahoo browser-ID cookies issued to our capture request)
    - appends the userId/displayName of each removed seller to `removed`, for the leak check
    """
    if isinstance(node, dict):
        if "auctionId" in node:
            seller = node.pop("seller", None)
            if isinstance(seller, dict):
                for key in ("userId", "displayName"):
                    value = seller.get(key)
                    if isinstance(value, str) and value:
                        removed.append(value)
            node.pop("prefectureCode", None)
        if "setCookies" in node:
            node["setCookies"] = []
        for value in node.values():
            _sanitize(value, removed)
    elif isinstance(node, list):
        for value in node:
            _sanitize(value, removed)


def strip_yahoo_seller_data(html):
    """
    Returns the page with seller data (userId, displayName, city) removed from
    the __NEXT_DATA__ JSON, so no personal data is committed.
    Fails closed: if the block is missing or is not valid JSON we raise instead
    of returning the page, so an unsanitized page can never be written.
    It also raises if a removed seller's userId or displayName still appears
    anywhere in the output, in case Yahoo moves seller data somewhere the
    key-based stripping does not reach.
    """
    match = NEXT_DATA_RE.search(html)
    if match is None:
        raise RuntimeError("No __NEXT_DATA__ script block found; refusing to write unsanitized page")
    try:
        data = json.loads(match.group(2))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"__NEXT_DATA__ is not valid JSON ({e}); refusing to write unsanitized page")

    removed = []
    _sanitize(data, removed)
    clean = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # seller-written titles could contain "</script>", which would close the tag early and
    # corrupt the page. Next.js escapes "<" the same way; it is still valid JSON with the same data.
    clean = clean.replace("<", "\\u003c")
    # splice by position (not re.sub) so backslashes in the JSON are never read as regex escapes
    output = html[: match.start(2)] + clean + html[match.end(2):]

    # Safety net: look for the removed values as plain text. Also check the "<"-escaped form,
    # because that is how a value would look inside the JSON block.
    # The message only gives a count so the leaked value is never printed.
    leaked = [v for v in set(removed) if v in output or v.replace("<", "\\u003c") in output]
    if leaked:
        raise RuntimeError(
            f"{len(leaked)} removed seller value(s) still appear in the page; refusing to write it"
        )
    return output


def capture_yahoo():
    response = requests.get(RICK_OWENS_URL, timeout=TIMEOUT)
    response.raise_for_status()
    # sanitize before anything touches disk
    clean_html = strip_yahoo_seller_data(response.content.decode("utf-8"))
    YAHOO_OUT.parent.mkdir(parents=True, exist_ok=True)
    YAHOO_OUT.write_text(clean_html, encoding="utf-8")


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

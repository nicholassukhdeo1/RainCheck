import os, base64, requests, sys, math, re
from dataclasses import dataclass
from db import get_tracked_items, record_ebay_listing_prices, SchemaVersionError
from matching import title_matches

from dotenv import load_dotenv
load_dotenv()

# timeout so a hung connection can't stall the run forever
EBAY_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class ListingPrice:
    """A matching listing's immutable first observed asking price."""
    ebay_listing_id: str
    price_usd: float


def get_ebay_token():

    # read credentials here, not at import, so importing this module never
    # fails when the variables are missing (fresh worktrees, tests).
    client_id = os.environ["EBAY_CLIENT_ID"]
    client_secret = os.environ["EBAY_CLIENT_SECRET"]

    # ebay takes ur credentials in base64 encryption
    creds = f"{client_id}:{client_secret}"
    b64 = base64.b64encode(creds.encode()).decode()

    URL_ = "https://api.ebay.com/identity/v1/oauth2/token"

    response = requests.post(URL_,headers={"Authorization": f"Basic {b64}", "Content-Type": "application/x-www-form-urlencoded"},
                            data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
                            timeout=EBAY_TIMEOUT_SECONDS)

    # error checking, lets us know if we failed to get the oauth2 token.
    response.raise_for_status()


    #json formats response into a dictionary including access_token as one of the keys.
    return response.json()["access_token"]

    # i need to open the json

# verifying if my ebay token func worked.
# print(get_ebay_token()[:40])

def _canonical_listing_id(item):
    """Collapse REST variation IDs to their parent listing when possible."""
    legacy = item.get("legacyItemId")
    legacy_id = (
        legacy if isinstance(legacy, str) and legacy.isascii() and legacy.isdecimal()
        else None
    )
    rest = item.get("itemId")
    rest_parts = re.fullmatch(r"v1\|(\d+)\|(\d+)", rest) if isinstance(rest, str) else None
    if legacy_id and rest_parts and legacy_id != rest_parts.group(1):
        return None
    if legacy_id:
        return f"legacy:{legacy_id}"
    if rest_parts:
        return f"legacy:{rest_parts.group(1)}"
    if isinstance(rest, str) and rest.strip():
        return f"rest:{rest}"
    return None


def ebay_listing_prices(query, keywords, exclude_keywords, token):
    """Fetch the first page and return validated fixed-price listing observations."""
    response = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        params={"q": query, "limit": 50, "sort": "newlyListed"},
        timeout=EBAY_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("eBay search response must be an object")
    items = data.get("itemSummaries", [])
    if not isinstance(items, list):
        raise ValueError("eBay itemSummaries must be a list")

    listings = []
    seen_ids = set()
    fallback_count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        options = item.get("buyingOptions")
        if not isinstance(options, list) or "FIXED_PRICE" not in options:
            continue
        title = item.get("title")
        price = item.get("price")
        listing_id = _canonical_listing_id(item)
        if (
            listing_id is None or not isinstance(title, str)
            or not isinstance(price, dict) or price.get("currency") != "USD"
        ):
            continue
        if not title_matches(title, keywords, exclude_keywords):
            continue
        value = price.get("value")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        try:
            amount = float(value)
        except (ValueError, OverflowError):
            continue
        if not math.isfinite(amount) or amount <= 0 or listing_id in seen_ids:
            continue
        # Only a valid first row claims the canonical ID; malformed copies do not.
        seen_ids.add(listing_id)
        if listing_id.startswith("rest:"):
            fallback_count += 1
        listings.append(ListingPrice(listing_id, amount))
    if fallback_count:
        print(f"eBay search used {fallback_count} opaque listing ID fallback(s).")
    return tuple(listings)

def refresh_market_prices():

    # read the items before asking eBay for a token: opening the database checks its
    # schema version, so an outdated database fails before any network call
    items = get_tracked_items()
    if not items:
        print("No active items in the database. Add items to catalog.json, then run python3 src/db.py --seed.")
        return

    my_token = get_ebay_token()

    for item in items:
        # search_query is the wording that finds this item on eBay; fall back to the name if it is missing
        query = item.search_query or item.name
        print(f'Pricing {item.name} with eBay query "{query}"')

        listings = ebay_listing_prices(query, item.keywords, item.exclude_keywords, my_token)
        result = record_ebay_listing_prices(item.id, listings)
        print(
            f"{item.name}: {result.new_count} new, {result.duplicate_count} duplicate; "
            f"{result.candidate_count} cumulative candidates, {result.outlier_count} outliers, "
            f"{result.retained_count} retained."
        )
        if result.price_updated:
            print(f"Stored all-time eBay arithmetic mean for {item.name}: {result.mean_usd} USD.")
        elif result.new_count == 0 and result.candidate_count == 0:
            print(f"No valid matching eBay listings for {item.name}; insufficient sample, market price preserved.")
        elif result.new_count == 0:
            print(f"No new eBay listing IDs for {item.name}; current market price preserved.")
        elif result.mean_usd is None:
            print(f"Insufficient retained eBay listings for {item.name} (need 5); market price preserved.")
        elif not result.item_found:
            print(f"Item {item.name} no longer exists; market price not stored.")
        else:
            print(f"No retained-price update for {item.name}; current market price preserved.")


def main():
    try:
        # refresh_market_prices fetches its own token, so we do not fetch one here too
        refresh_market_prices()
    except SchemaVersionError as e:
        # the one error with a message written for you (it names the fix), so no traceback
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

import os, base64, requests, statistics, sys, math
from dataclasses import dataclass
from db import get_tracked_items, update_market_price, SchemaVersionError
from matching import title_matches

from dotenv import load_dotenv
load_dotenv()

# timeout so a hung connection can't stall the run forever
EBAY_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class PriceEstimate:
    price_usd: float | None
    candidate_count: int
    outlier_count: int
    retained_count: int


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

def ebay_market_price(query, keywords, exclude_keywords, token):

    # bearer means that we have already gotten the token
    currListing = requests.get("https://api.ebay.com/buy/browse/v1/item_summary/search",headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
                               ,params={"q": query,"limit": 50}, timeout=EBAY_TIMEOUT_SECONDS)
    currListing.raise_for_status()

    data = currListing.json()
    if not isinstance(data, dict):
        raise ValueError("eBay search response must be an object")
    items = data.get("itemSummaries", [])
    if not isinstance(items, list):
        raise ValueError("eBay itemSummaries must be a list")

    prices = []
    seen_ids = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("itemId")
        title = item.get("title")
        price = item.get("price")
        if (
            not isinstance(item_id, str) or not item_id.strip()
            or not isinstance(title, str)
            or not isinstance(price, dict)
            or price.get("currency") != "USD"
            or item_id in seen_ids
        ):
            continue
        # the shared matcher (also used by the Yahoo job) decides what counts as this item
        if not title_matches(title, keywords, exclude_keywords):
            continue
        value = price.get("value")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        try:
            amount = float(value)
        except (ValueError, OverflowError):
            continue
        if not math.isfinite(amount) or amount <= 0:
            continue
        # Mark an ID seen only after validation, so a later valid copy can count.
        seen_ids.add(item_id)
        prices.append(amount)

    if not prices:
        return PriceEstimate(None, 0, 0, 0)

    median = statistics.median(prices)
    mad = statistics.median(abs(price - median) for price in prices)
    if mad == 0:
        retained = [price for price in prices if price == median]
    else:
        # Use the original median and MAD for one filtering pass.
        cutoff = 3.5 * 1.4826 * mad
        retained = [price for price in prices if abs(price - median) <= cutoff]

    retained_count = len(retained)
    return PriceEstimate(
        statistics.median(retained) if retained_count >= 5 else None,
        len(prices),
        len(prices) - retained_count,
        retained_count,
    )

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

        estimate = ebay_market_price(query, item.keywords, item.exclude_keywords, my_token)
        print(
            f"{item.name}: {estimate.candidate_count} candidates, "
            f"{estimate.outlier_count} outliers, {estimate.retained_count} retained."
        )
        if estimate.price_usd is None:
            if estimate.candidate_count == 0:
                print(f"No valid matching eBay listings for {item.name}; insufficient sample, market price not updated.")
            else:
                print(f"Insufficient retained eBay listings for {item.name} (need 5); market price not updated.")
            continue

        # The database still owns the final usable-price check before writing.
        try:
            stored = update_market_price(item.id, estimate.price_usd)
        except ValueError:
            print(f"eBay estimate for {item.name} is not a usable price; market price not updated.")
            continue
        if stored:
            print(f"Stored eBay estimate for {item.name}: {estimate.price_usd} USD.")
        else:
            print(f"Item {item.name} no longer exists; market price not stored.")


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

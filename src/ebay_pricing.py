import os, base64, requests, statistics, sys
from db import get_tracked_items, update_market_price, SchemaVersionError
from matching import title_matches

from dotenv import load_dotenv
load_dotenv()

# timeout so a hung connection can't stall the run forever
EBAY_TIMEOUT_SECONDS = 30


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

    price = []

    items = currListing.json().get("itemSummaries",[])

    for item in items:
        # the shared matcher (also used by the Yahoo job) decides what counts as this item
        if title_matches(item["title"], keywords, exclude_keywords):
            price.append(float(item["price"]["value"]))

    # always look at the format of the json or api before trying to access it because how else would u know
    # how to work with it.

    # print(json.dumps(currListing.json().get("itemSummaries", [])[0], indent=2))

    for item in sorted(items, key=lambda i: float(i["price"]["value"])):
        print(item["price"]["value"], "-", item["title"][:70])

    if not price:
        return None
    return statistics.median(price)

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

        price = ebay_market_price(query, item.keywords, item.exclude_keywords, my_token)
        if price is None:
            print(f"No matching eBay listings for {item.name}; market price not updated.")
            continue

        # update_market_price owns the "usable price" rule, so we just catch its ValueError
        # instead of copying the rule here; one bad median must not stop the other items
        try:
            stored = update_market_price(item.id, price)
        except ValueError:
            print(f"eBay median for {item.name} is not a usable price ({price}); market price not updated.")
            continue
        if not stored:
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

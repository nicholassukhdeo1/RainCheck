import os, base64, requests, statistics
from db import get_item_id, update_market_price, load_catalog

from dotenv import load_dotenv
load_dotenv()


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
                            data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"})

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
                               ,params={"q": query,"limit": 50})
    currListing.raise_for_status()

    price = []

    items = currListing.json().get("itemSummaries",[])

    for item in items:
        # if any keyword in item["title"].lower()

        title = item["title"].lower()

        if any(bad in title for bad in exclude_keywords):
            continue

        if any(keyword in title for keyword in keywords):
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

    my_token = get_ebay_token()

    for item in load_catalog():
        price = ebay_market_price(item["item name"],item["keywords"],item["exclude_keywords"],my_token)
        if price is None:
            continue

        item_id = get_item_id(item["brand"],item["item name"])

        update_market_price(item_id,price)


def main():
    # refresh_market_prices fetches its own token, so we do not fetch one here too
    refresh_market_prices()


if __name__ == "__main__":
    main()

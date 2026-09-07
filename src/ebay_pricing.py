import os, base64, requests, statistics
import json

from dotenv import load_dotenv
load_dotenv()

client_id = os.environ["EBAY_CLIENT_ID"] 
client_secret = os.environ["EBAY_CLIENT_SECRET"]

creds = f"{client_id}:{client_secret}"

# ebay takes ur credentials in base64 encryption
b64 = base64.b64encode(creds.encode()).decode()



def get_ebay_token():

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

def ebay_market_price(query, keywords, token):

    # bearer means that we have already gotten the token
    currListing = requests.get("https://api.ebay.com/buy/browse/v1/item_summary/search",headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
                               ,params={"q": query,"limit": 50})
    currListing.raise_for_status()

    price = []

    items = currListing.json().get("itemSummaries",[])

    for item in items:
        # if any keyword in item["title"].lower()

        title = item["title"].lower()

        if any(bad in title for bad in ["drkshdw", "canvas"]):
            continue

        if any(keyword in title for keyword in keywords):
            price.append(float(item["price"]["value"]))

    # always look at the format of the json or api before trying to access it because how else would u know
    # how to work with it.

    # print(json.dumps(currListing.json().get("itemSummaries", [])[0], indent=2))

    # for item in sorted(items, key=lambda i: float(i["price"]["value"])):
    #     print(item["price"]["value"], "-", item["title"][:70])


    return statistics.median(price)

    


if __name__ == "__main__":
    my_token = get_ebay_token()

    print(ebay_market_price("rick owens ramones",["ramones","ramon","high top"],my_token))
    




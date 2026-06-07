from bs4 import BeautifulSoup
from sendemail import send_email
import requests
import json
import re


# --- Item Catalog ---
# each dictionary represents one item to monitor.
# add new items here as you expand the catalog.
ramones = {
    "item name": "Ramones",
    "brand": "Rick Owens",
    "size": 42,
    "search_query": "rick+owens+ramones",
    "market_price": 800  # average resale price in USD
}

pod_shorts = {
    "item name": "Pod Shorts",
    "brand": "Rick Owens",
    "size": 42,
    "search_query": "pods",
    "market_price": 200
}


def yahoo_jp_scrape(item_dict):
    """
    Scrapes Yahoo Auctions Japan for new listings of a given item.
    Filters out overpriced listings, ranks by discount from market price,
    and emails the top 5 best deals.
    """

    desired_item = {
        "item name": f"{desired_item}"
    }

    # each item gets its own JSON file to track previously seen listings.
    # this way we only alert on genuinely new listings each run.
    filename = f"{item_dict['search_query']}_seen_links.json"

    try:
        with open(filename, "r") as f:
            seen_links = json.load(f)
    except FileNotFoundError:
        # first run — no file exists yet, so start fresh
        seen_links = []

    # build the search URL dynamically using the item's search query
    url = (
        f"https://auctions.yahoo.co.jp/search/search?"
        f"auccat=23172&tab_ex=commerce&ei=utf-8&aq=-1&oq=&sc_i=&p={item_dict['search_query']}"
    )

    response = requests.get(url)
    soup = BeautifulSoup(response.text, "html.parser")

    # pull all relevant elements from the page
    alltitles = soup.find_all('h3')
    allprices = soup.find_all('span', class_='Product__priceValue u-textRed')
    alllinks = soup.find_all('a', class_='Product__titleLink js-browseHistory-add js-rapid-override')
    allimages = soup.find_all('img', class_='Product__imageData')

    # filter down to only links we haven't seen before
    # link['href'] extracts the URL string from the html element
    new_links = []
    for link in alllinks:
        if link['href'] not in seen_links:
            new_links.append(link['href'])

    # save updated seen links so next run knows what's already been processed
    seen_links += new_links
    with open(filename, "w") as f:
        json.dump(seen_links, f)

    # build a list of new listings with pricing data
    listings = []
    for title, price, link, image in zip(alltitles, allprices, new_links, allimages):

        # strip everything non-numeric from the price string (e.g. "71,500円" -> "71500")
        num_price = re.sub(r'[^0-9]', '', price.text)

        # convert yen to USD using current exchange rate
        price_usd = int(num_price) * 0.0062

        # calculate how much cheaper this listing is vs market price (as a percentage)
        # formula: ((market - listing) / market) * 100
        pct_cheaper = ((item_dict['market_price'] - price_usd) / item_dict['market_price']) * 100

        # only include listings that are actually below market price
        if pct_cheaper > 0:
            listings.append({
                "title": title.text.strip(),
                "raw_price": price.text,
                "price_usd": price_usd,
                "pct_cheaper": pct_cheaper,
                "link": link,
                "image": image['src']
            })

    # sort by biggest discount first, then take the top 5
    listings.sort(key=lambda x: x['pct_cheaper'], reverse=True)
    top_five = listings[:5]

    # build the email body in HTML format
    # <br> is used instead of \n because email clients render HTML
    body = []
    for listing in top_five:
        print("Title:", listing['title'])
        print("Price:", listing['price_usd'], f"{listing['pct_cheaper']}% cheaper than mkt")
        print("Product Link:", listing['link'])
        print("Image Link:", listing['image'])

        body.append(f"Title: {listing['title']}")

        # this outputs the price to TWO decimal points
        body.append(f"Price: ${listing['price_usd']:.2f}")

        # same concept, but ONE decimal point.
        body.append(f"{listing['pct_cheaper']:.1f}% cheaper than market")
        body.append(f"Product Link: {listing['link']}")
        body.append(f"Image: {listing['image']}")
        body.append("---")  # separator between listings

    body_text = "<br>".join(body)
    send_email(f"Daily Refresh on {item_dict['item name']}", body_text)

    print(f"--- Done: {item_dict['item name']} ---")


# --- Run ---

desired_item = input("Tell me something you'd like to get daily updates on!")
item_price = input("What's the market price on it?")
yahoo_jp_scrape(desired_item)
# yahoo_jp_scrape(pod_shorts)
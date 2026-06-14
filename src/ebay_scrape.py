from bs4 import BeautifulSoup
from sendemail import send_email
import requests
import json
import re

def ebay_scrape(item_dict):
    """
    Scrapes eBay for new listings of a given item.
    Filters out overpriced listings, ranks by discount from market price,
    and emails the top 5 best deals.
    """

    # build the search URL dynamically using the item's search query
    url = (
        f"https://www.ebay.com/sch/i.html?_nkw={item_dict['search_query']}"
        f"&_sacat=0&_from=R40&_trksid=p4624852.m570.l1313"
    )

    headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Referer": "https://www.google.com"
    }



    # beautifulSoup lets us scrape the info in the link
    # on top of it too, the headers component of what we're working with allows us to camoflage our way into the script
    # as otherwise, as a "python script," i was getting blocked
    response = requests.get(url,headers=headers)
    soup = BeautifulSoup(response.text, "html.parser")

    # pull all relevant elements from the page
    alltitles = soup.find_all('span', class_='su-styled-text primary default')
    allprices = soup.find_all('span', class_='su-styled-text primary bold large-1 s-card__price')

    # build a list of new listings with pricing data
    for title, price in zip(alltitles, allprices):

        # strip everything non-numeric from the price string (e.g. "71,500円" -> "71500")
        num_price = re.sub(r'[^0-9]', '', price.text)

        # its title.text otherwise you're printing the output as well
        print("Title:", title.text.strip())
        print("Price:", f"${num_price}")

    print(f"--- eBay Update for: {item_dict['item name']} ---")


# --- Run ---

desired_item = input("Tell me something you'd like to get daily updates on!")

# you need to convert this into a float.. cuz whatever you take via
# input will be a string. but you need to work w/ a float for this.
item_price = float(input("What's the market price on it?"))

user_item = {
    "item name": desired_item,
    "brand": "Rick Owens",
    "size": 42,

    # so, it modifies what we put via desired_item by taking
    # any space you inputted in, and turning it into a +
    # because thats how these search links are built in first place
    "search_query": desired_item.replace(" ", "+"),
    "market_price": item_price
}

ebay_scrape(user_item)
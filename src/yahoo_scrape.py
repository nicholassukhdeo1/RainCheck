from bs4 import BeautifulSoup
from sendemail import send_email
from db import get_connection, insert_listing, get_market_price, load_catalog
import requests
import re


def yahoo_jp_scrape(item_dict, brand_url):
    """
    Scrapes a Yahoo Auctions Japan brand page for new listings.
    Filters by item keywords, removes overpriced listings,
    ranks by discount from market price, and emails the top 5 deals.
    """

    conn = get_connection()
    brand_id = conn.execute(
        "SELECT id FROM brands WHERE name = ?",
        (item_dict["brand"],),
    ).fetchone()["id"]
    conn.close()

    market_price = get_market_price(item_dict["brand"],item_dict["item name"])
    if market_price is None:
        return

    

    response = requests.get(brand_url)
    soup = BeautifulSoup(response.text, "html.parser")

    alltitles = soup.find_all('h3')
    allprices = soup.find_all('span', class_='Product__priceValue u-textRed')
    alllinks = soup.find_all('a', class_='Product__titleLink js-browseHistory-add js-rapid-override')
    allimages = soup.find_all('img', class_='Product__imageData')



    listings = []
    for title, price, link, image in zip(alltitles, allprices, alllinks, allimages):
        title_text = title.text.strip().lower()

        # only process listings whose title contains at least one item keyword

        # any() is a method that returns True as long as ONE condition yields true

        if not any(keyword in title_text for keyword in item_dict['keywords']):
            continue
        
        # if the title you're currently looking at doesnt have any of the keywords, FADE it
        # and just keep it pushing, continue to next title.

        num_price = re.sub(r'[^0-9]', '', price.text)
        price_usd = int(num_price) * 0.0062
        pct_cheaper = ((market_price - price_usd) / market_price) * 100

        if pct_cheaper > 0:
            is_new = insert_listing(brand_id,link['href'],title.text.strip(),int(num_price),image['src'])
            if is_new:
                listings.append({
                    "title": title.text.strip(),
                    "raw_price": price.text,
                    "price_usd": price_usd,
                    "pct_cheaper": pct_cheaper,
                    "link": link['href'],
                    "image": image['src']
                })

    listings.sort(key=lambda x: x['pct_cheaper'], reverse=True)
    top_five = listings[:5]

    body = []
    for listing in top_five:
        print("Title:", listing['title'])
        print("Price:", listing['price_usd'], f"{listing['pct_cheaper']}% cheaper than mkt")
        print("Product Link:", listing['link'])

        body.append(f"Title: {listing['title']}")
        body.append(f"Price: ${listing['price_usd']:.2f}")
        body.append(f"{listing['pct_cheaper']:.1f}% cheaper than market")
        body.append(f"Product Link: {listing['link']}")
        body.append(f"Image: {listing['image']}")
        body.append("---")

    body_text = "<br>".join(body)
    send_email(f"Daily Refresh on {item_dict['item name']}", body_text)
    print(f"--- Yahoo Auctions Update for: {item_dict['item name']} ---")


# Rick Owens brand page - newest first, 100 items per page
RICK_OWENS_URL = "https://auctions.yahoo.co.jp/category/list/23000/?auccat=23000&s1=new&o1=d&brand_id=103538&n=100"

def main():
    for item in load_catalog():
        yahoo_jp_scrape(item, RICK_OWENS_URL)


if __name__ == "__main__":
    main()

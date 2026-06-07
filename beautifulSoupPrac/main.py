from bs4 import BeautifulSoup
from sendemail import send_email
import requests
import json
import re



ramones = {
    "item name": "Ramones",
    "brand": "Rick Owens",
    "size": 42,
    "search_query": "rick+owens+ramones",
    "market_price": 800
}

pod_shorts = {
    "item name": "Pod Shorts",
    "brand": "Rick Owens",
    "size": 42,
    "search_query": "pods",
    "market_price": 200
}

def yahoo_jp_scrape(item_dict):

    filename = f"{item_dict['search_query']}_seen_links.json"

    try:
        with open(filename, "r") as f:
            seen_links = json.load(f)
    except FileNotFoundError:
        seen_links = []

    url = f"https://auctions.yahoo.co.jp/search/search?auccat=23172&tab_ex=commerce&ei=utf-8&aq=-1&oq=&sc_i=&p={item_dict['search_query']}"

    response = requests.get(url)

    soup = BeautifulSoup(response.text, "html.parser")



    alltitles = soup.find_all('h3')

    allprices = soup.find_all('span', class_='Product__priceValue u-textRed')

    alllinks = soup.find_all('a', class_='Product__titleLink js-browseHistory-add js-rapid-override')

    allimages = soup.find_all('img', class_='Product__imageData')

    new_links = []
    for link in alllinks:
        if link['href'] not in seen_links: #its link['href'] because that extracts the LINK from the html
            new_links.append(link['href'])

    seen_links += new_links
    with open(filename, "w") as f:
        json.dump(seen_links, f)

    #if... not in
    # then use that link

    # i get it... so when you want to extract TEXT... you use .text
    # but when extracting smth else like links.. you must do "object['href']"
    # for link in alllinks:
    #     print(link['href'])

    listing_dict = {}
    
    body = []
    for title, price, link, image in zip(alltitles,allprices,new_links,allimages):
        num_price = re.sub(r'[^0-9]','', price.text)
        price_usd = int(num_price) * 0.0062
        pct_cheaper = ((item_dict['market_price'] - price_usd) / item_dict['market_price']) * 100
        if pct_cheaper > 0:
            listings.append({
                "title": title.text.strip(),
                "raw_price": price.text,
                "price_usd": price_usd,
                "pct_cheaper": pct_cheaper,
                "link": link,
                "image": image['src']
            })

            

            print("Title:",title.text.strip())
            print("Price:",int(num_price), f"{pct_cheaper}% cheaper than mkt")
            print("Product Link:",link)
            print("Image Link: ", image['src'])

            body.append(f"Title: {title.text.strip()}")
            body.append(f"Price: {price.text}")
            body.append(f"Product Link: {link}")
            body.append(f"Image Link: {image['src']}")

    
    body_text = "\n".join(body)

    # send_email(f"Daily Refresh on {item_dict['item name']}", body_text)

    print(f"--- All links above for {item_dict['item name']} ---")

yahoo_jp_scrape(ramones)
# yahoo_jp_scrape(pod_shorts)


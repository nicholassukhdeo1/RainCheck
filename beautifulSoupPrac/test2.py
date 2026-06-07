from bs4 import BeautifulSoup
import requests
import json



ramones = {
    "item name": "Ramones",
    "brand": "Rick Owens",
    "size": 42,
    "search_query": "rick+owens+ramones"
}

pod_shorts = {
    "item name": "Pod Shorts",
    "brand": "Rick Owens",
    "size": 42,
    "search_query": "pods"
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

    for title, price, link in zip(alltitles,allprices,new_links):
        print("Title:",title.text.strip())
        print("Price:",price.text)
        print("Product Link:",link)

    print(f"--- All links above for {item_dict['item name']} ---")

yahoo_jp_scrape(ramones)
# yahoo_jp_scrape(pod_shorts)


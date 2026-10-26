from bs4 import BeautifulSoup
from sendemail import send_email
from db import get_connection, insert_listing, get_market_price, load_catalog
from dataclasses import dataclass
import json
import logging
import requests

# Rick Owens brand page - newest first, 100 items per page
# (no trailing slash after 23000: with it Yahoo answers 308, an extra round trip every run)
RICK_OWENS_URL = "https://auctions.yahoo.co.jp/category/list/23000?auccat=23000&s1=new&o1=d&brand_id=103538&n=100"
YAHOO_TIMEOUT_SECONDS = 30
# Yahoo's page is client-rendered, so the listings only exist inside the __NEXT_DATA__ JSON
LISTING_PATH = ("props", "pageProps", "initialState", "search", "items", "listing", "items")
AUCTION_URL_PREFIX = "https://auctions.yahoo.co.jp/jp/auction/"
FLEAMARKET_URL_PREFIX = "https://paypayfleamarket.yahoo.co.jp/item/"

logger = logging.getLogger(__name__)


class YahooPageError(Exception):
    """The page is not a results page we understand (layout drift), as opposed to 'no results'."""


# frozen=True makes each record read-only, so a listing can't be changed after it is validated
@dataclass(frozen=True)
class YahooListing:
    auction_id: str
    url: str
    title: str
    price_yen: int
    image_url: str | None


def fetch_yahoo_page(url):
    """Downloads a Yahoo results page and returns it as text."""
    # timeout so a hung connection can't stall the run forever
    response = requests.get(url, timeout=YAHOO_TIMEOUT_SECONDS)
    # raise on 4xx/5xx so an error page is never parsed as if it were results
    response.raise_for_status()
    # decode explicitly: requests guesses ISO-8859-1 if the charset is missing, which garbles Japanese
    return response.content.decode("utf-8", errors="replace")


def _parse_record(record):
    """Returns a YahooListing for a valid record, or None if the record is invalid."""
    if not isinstance(record, dict):
        return None

    auction_id = record.get("auctionId")
    # ASCII letters and digits only, so an ID can never put "/", "?" or ".." into the URL
    if not (isinstance(auction_id, str) and auction_id.isascii() and auction_id.isalnum()):
        return None

    title = record.get("title")
    if not isinstance(title, str) or not title.strip():
        return None

    price = record.get("price")
    # bool is a subclass of int in Python, so True would otherwise pass as a price of 1
    if not isinstance(price, int) or isinstance(price, bool) or price < 1:
        return None

    image_url = record.get("imageUrl")
    if not isinstance(image_url, str) or not image_url:
        image_url = None

    # only a literal True counts as a flea market item
    prefix = FLEAMARKET_URL_PREFIX if record.get("isFleamarketItem") is True else AUCTION_URL_PREFIX

    return YahooListing(auction_id, prefix + auction_id, title.strip(), price, image_url)


def parse_listings(html):
    """
    Turns a Yahoo results page into a list of validated YahooListing records.
    Bad records are skipped and counted; a page whose layout has changed raises YahooPageError.
    """
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None or script.string is None or not script.string.strip():
        raise YahooPageError("Yahoo page has no __NEXT_DATA__ script; the layout may have changed")

    try:
        data = json.loads(script.string)
    except json.JSONDecodeError as e:
        raise YahooPageError("Yahoo __NEXT_DATA__ is not valid JSON") from e

    # walk the path one key at a time so the error can say exactly which step is missing
    dotted_path = ".".join(LISTING_PATH)
    node = data
    for key in LISTING_PATH:
        if not isinstance(node, dict) or key not in node:
            raise YahooPageError(f"Yahoo __NEXT_DATA__ is missing the listing path {dotted_path} (stopped at '{key}')")
        node = node[key]
    if not isinstance(node, list):
        raise YahooPageError(f"Yahoo __NEXT_DATA__ listing path {dotted_path} is not a list")

    # an empty array is a legitimate "no results" page, not drift
    if not node:
        return []

    listings = []
    seen_ids = set()
    invalid = 0
    duplicate = 0
    for record in node:
        listing = _parse_record(record)
        if listing is None:
            invalid += 1
        elif listing.auction_id in seen_ids:
            # first occurrence wins
            duplicate += 1
        else:
            seen_ids.add(listing.auction_id)
            listings.append(listing)

    # every record failing means the record shape changed, which is as serious as a missing path
    if not listings:
        raise YahooPageError(f"Yahoo listing records changed shape: 0 of {len(node)} records were valid")

    skipped = invalid + duplicate
    if skipped:
        logger.warning(
            "Skipped %d of %d Yahoo listing records: %d invalid, %d duplicate",
            skipped, len(node), invalid, duplicate,
        )
    return listings


def report_item_deals(item_dict, listings):
    """
    Filters already-parsed Yahoo listings by item keywords, removes overpriced listings,
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

    deals = []
    for listing in listings:
        title_text = listing.title.lower()

        # only process listings whose title contains at least one item keyword

        # any() is a method that returns True as long as ONE condition yields true

        if not any(keyword in title_text for keyword in item_dict['keywords']):
            continue

        # if the title you're currently looking at doesnt have any of the keywords, FADE it
        # and just keep it pushing, continue to next title.

        price_usd = listing.price_yen * 0.0062
        pct_cheaper = ((market_price - price_usd) / market_price) * 100

        if pct_cheaper > 0:
            is_new = insert_listing(brand_id, listing.url, listing.title, listing.price_yen, listing.image_url)
            if is_new:
                deals.append({
                    "title": listing.title,
                    "raw_price": listing.price_yen,
                    "price_usd": price_usd,
                    "pct_cheaper": pct_cheaper,
                    "link": listing.url,
                    "image": listing.image_url
                })

    deals.sort(key=lambda x: x['pct_cheaper'], reverse=True)
    top_five = deals[:5]

    body = []
    for deal in top_five:
        print("Title:", deal['title'])
        print("Price:", deal['price_usd'], f"{deal['pct_cheaper']}% cheaper than mkt")
        print("Product Link:", deal['link'])

        body.append(f"Title: {deal['title']}")
        body.append(f"Price: ${deal['price_usd']:.2f}")
        body.append(f"{deal['pct_cheaper']:.1f}% cheaper than market")
        body.append(f"Product Link: {deal['link']}")
        # a listing without a thumbnail has no image to show, so skip the line instead of printing "Image: None"
        if deal['image'] is not None:
            body.append(f"Image: {deal['image']}")
        body.append("---")

    body_text = "<br>".join(body)
    send_email(f"Daily Refresh on {item_dict['item name']}", body_text)
    print(f"--- Yahoo Auctions Update for: {item_dict['item name']} ---")


def main():
    # catalog first, so a bad catalog.json fails before any network call
    catalog = load_catalog()
    # fetch and parse once per run; errors propagate on purpose so a broken page
    # means no emails and a non-zero exit instead of looking like "no deals today"
    listings = parse_listings(fetch_yahoo_page(RICK_OWENS_URL))
    for item in catalog:
        report_item_deals(item, listings)


if __name__ == "__main__":
    main()

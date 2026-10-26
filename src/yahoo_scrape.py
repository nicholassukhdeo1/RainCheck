from bs4 import BeautifulSoup
from sendemail import send_email
from db import get_tracked_items, get_known_listing_urls, insert_listing, SchemaVersionError, TrackedItem
from digest import Deal, ItemDeals, build_digest
from matching import title_matches
from dataclasses import dataclass
from datetime import datetime
from typing import Collection, Sequence
import json
import logging
import requests
import sys

# Rick Owens brand page - newest first, 100 items per page
# (no trailing slash after 23000: with it Yahoo answers 308, an extra round trip every run)
RICK_OWENS_URL = "https://auctions.yahoo.co.jp/category/list/23000?auccat=23000&s1=new&o1=d&brand_id=103538&n=100"
YAHOO_TIMEOUT_SECONDS = 30
# Yahoo's page is client-rendered, so the listings only exist inside the __NEXT_DATA__ JSON
LISTING_PATH = ("props", "pageProps", "initialState", "search", "items", "listing", "items")
AUCTION_URL_PREFIX = "https://auctions.yahoo.co.jp/jp/auction/"
FLEAMARKET_URL_PREFIX = "https://paypayfleamarket.yahoo.co.jp/item/"

YEN_TO_USD = 0.0062  # RC-08 replaces this with a live rate
TOP_DEALS_PER_ITEM = 5
NO_ITEMS_LINE = "No active items in the database. Add items to catalog.json, then run python3 src/db.py --seed."
NO_DEALS_LINE = "No new deals; no email sent."

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

    # JSON can carry a lone surrogate like "\ud800" that UTF-8 cannot encode; one such listing
    # would otherwise crash the send/insert step on every run while it stays on the page
    for text in (title, image_url):
        if text is not None:
            try:
                text.encode("utf-8")
            except UnicodeEncodeError:
                return None

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
    parent = None
    for key in LISTING_PATH:
        if not isinstance(node, dict) or key not in node:
            raise YahooPageError(f"Yahoo __NEXT_DATA__ is missing the listing path {dotted_path} (stopped at '{key}')")
        parent = node
        node = node[key]
    if not isinstance(node, list):
        raise YahooPageError(f"Yahoo __NEXT_DATA__ listing path {dotted_path} is not a list")

    if not node:
        # an empty array is only "no results" if Yahoo agrees; a placeholder array on a page
        # that reports results (or is still loading) would otherwise bring back silent empty emails
        total = parent.get("totalResultsAvailable")
        reports_results = isinstance(total, int) and not isinstance(total, bool) and total > 0
        if reports_results or parent.get("isFetching") is True:
            raise YahooPageError(
                f"Yahoo page reports results (totalResultsAvailable={total!r}) but {dotted_path} is empty"
            )
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


def find_item_deals(item: TrackedItem, listings: Sequence[YahooListing], skip_urls: Collection[str]) -> list[Deal]:
    """
    Pure: returns the item's new deals (keyword match, below market, not in skip_urls),
    best discount first. Nothing is written, so the caller decides when a deal counts as seen.
    """
    # the item already carries its price (eBay first, catalog fallback), so no database read is needed
    market_price = item.effective_price_usd
    if market_price is None:
        return []

    deals = []
    for listing in listings:
        # only process listings that match the item's keywords and none of its exclude keywords
        # (the same rule the eBay job uses, so both halves of the comparison mean the same item)
        if not title_matches(listing.title, item.keywords, item.exclude_keywords):
            continue
        if listing.url in skip_urls:
            continue

        price_usd = listing.price_yen * YEN_TO_USD
        pct_cheaper = ((market_price - price_usd) / market_price) * 100
        if pct_cheaper > 0:
            deals.append(Deal(
                item_id=item.id,
                brand_id=item.brand_id,
                url=listing.url,
                title=listing.title,
                price_yen=listing.price_yen,
                price_usd=price_usd,
                market_price_usd=market_price,
                discount_pct=pct_cheaper,
                image_url=listing.image_url,
            ))

    # sort is stable, so equal discounts keep page order (newest first)
    deals.sort(key=lambda d: d.discount_pct, reverse=True)
    return deals


def build_sections(
    items: Sequence[TrackedItem],
    listings: Sequence[YahooListing],
    known_urls: Collection[str],
    per_item_limit: int = TOP_DEALS_PER_ITEM,
) -> list[ItemDeals]:
    """Pure: one section per item that has new deals, top per_item_limit each, in item order."""
    known = set(known_urls)
    # a listing that is a deal for two items is shown under the first only,
    # even if the first item's top-N cut hid it
    claimed = set()
    sections = []
    for item in items:
        deals = find_item_deals(item, listings, known | claimed)
        claimed.update(deal.url for deal in deals)
        if deals:
            sections.append(ItemDeals(
                item.name,
                item.effective_price_usd,
                tuple(deals[:per_item_limit]),
                max(0, len(deals) - per_item_limit),
            ))
    return sections


def main():
    try:
        # read the items first: opening the database checks its schema version,
        # so an outdated database fails before any network call
        items = get_tracked_items()
        if not items:
            print(NO_ITEMS_LINE)
            return
        # fetch and parse once per run; errors propagate on purpose so a broken page
        # means no emails and a non-zero exit instead of looking like "no deals today"
        listings = parse_listings(fetch_yahoo_page(RICK_OWENS_URL))
        for item in items:
            if item.effective_price_usd is None:
                print(f"Skipping {item.name}: no usable market price.")
        known = get_known_listing_urls(listing.url for listing in listings)
        digest = build_digest(build_sections(items, listings, known), datetime.now().astimezone().date())
        if digest is None:
            print(NO_DEALS_LINE)
            return
        if not send_email(digest.subject, digest.html, digest.text):
            # nothing was recorded, so the same deals are offered again next run
            n = len(digest.deals)
            print(
                "Error: digest email not sent, so its deals were not recorded and will be "
                f"offered again next run ({n} deal{'' if n == 1 else 's'}).",
                file=sys.stderr,
            )
            raise SystemExit(1)
        # record only after a successful send, and only the deals that were shown
        for deal in digest.deals:
            insert_listing(deal.brand_id, deal.url, deal.title, deal.price_yen, deal.image_url)
    except SchemaVersionError as e:
        # the one error with a message written for you (it names the fix), so no traceback
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

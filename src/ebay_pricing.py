import os, base64, requests, math, re, sys
from dataclasses import dataclass
from db import get_tracked_items, record_ebay_listing_prices, SchemaVersionError
import http_retry
from matching import title_matches
import logging

from dotenv import load_dotenv
load_dotenv()

# timeout so a hung connection can't stall the run forever
EBAY_TIMEOUT_SECONDS = 30
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunResult:
    succeeded_item_ids: tuple[int, ...]
    failed_item_ids: tuple[int, ...]

    @property
    def had_failure(self):
        return bool(self.failed_item_ids)


def _configure_logging():
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)


@dataclass(frozen=True)
class ListingPrice:
    """A matching listing's immutable first observed asking price."""
    ebay_listing_id: str
    price_usd: float


def get_ebay_token():

    # read credentials here, not at import, so importing this module never
    # fails when the variables are missing (fresh worktrees, tests).
    client_id = os.environ["EBAY_CLIENT_ID"]
    client_secret = os.environ["EBAY_CLIENT_SECRET"]

    # ebay takes ur credentials in base64 encryption
    creds = f"{client_id}:{client_secret}"
    b64 = base64.b64encode(creds.encode()).decode()

    URL_ = "https://api.ebay.com/identity/v1/oauth2/token"

    response = http_retry.request_with_retry("POST", URL_, headers={"Authorization": f"Basic {b64}", "Content-Type": "application/x-www-form-urlencoded"},
                            data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
                            timeout=EBAY_TIMEOUT_SECONDS)

    # error checking, lets us know if we failed to get the oauth2 token.
    response.raise_for_status()


    #json formats response into a dictionary including access_token as one of the keys.
    return response.json()["access_token"]

    # i need to open the json

# verifying if my ebay token func worked.
# print(get_ebay_token()[:40])

def _canonical_listing_id(item):
    """Collapse REST variation IDs to their parent listing when possible."""
    legacy = item.get("legacyItemId")
    legacy_id = (
        legacy if isinstance(legacy, str) and legacy.isascii() and legacy.isdecimal()
        else None
    )
    rest = item.get("itemId")
    rest_parts = re.fullmatch(r"v1\|(\d+)\|(\d+)", rest) if isinstance(rest, str) else None
    if legacy_id and rest_parts and legacy_id != rest_parts.group(1):
        return None
    if legacy_id:
        return f"legacy:{legacy_id}"
    if rest_parts:
        return f"legacy:{rest_parts.group(1)}"
    if isinstance(rest, str) and rest.strip():
        return f"rest:{rest}"
    return None


def ebay_listing_prices(query, keywords, exclude_keywords, token):
    """Fetch the first page and return validated fixed-price listing observations."""
    response = http_retry.request_with_retry(
        "GET",
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        params={"q": query, "limit": 50, "sort": "newlyListed"},
        timeout=EBAY_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("eBay search response must be an object")
    items = data.get("itemSummaries", [])
    if not isinstance(items, list):
        raise ValueError("eBay itemSummaries must be a list")

    listings = []
    seen_ids = set()
    fallback_count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        options = item.get("buyingOptions")
        if not isinstance(options, list) or "FIXED_PRICE" not in options:
            continue
        title = item.get("title")
        price = item.get("price")
        listing_id = _canonical_listing_id(item)
        if (
            listing_id is None or not isinstance(title, str)
            or not isinstance(price, dict) or price.get("currency") != "USD"
        ):
            continue
        if not title_matches(title, keywords, exclude_keywords):
            continue
        value = price.get("value")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        try:
            amount = float(value)
        except (ValueError, OverflowError):
            continue
        if not math.isfinite(amount) or amount <= 0 or listing_id in seen_ids:
            continue
        # Only a valid first row claims the canonical ID; malformed copies do not.
        seen_ids.add(listing_id)
        if listing_id.startswith("rest:"):
            fallback_count += 1
        listings.append(ListingPrice(listing_id, amount))
    if fallback_count:
        logger.warning("eBay search used %d opaque listing ID fallback(s)", fallback_count)
    return tuple(listings)

def refresh_market_prices():

    # read the items before asking eBay for a token: opening the database checks its
    # schema version, so an outdated database fails before any network call
    logger.info("eBay pricing job started")
    items = get_tracked_items()
    if not items:
        logger.info("No active items in the database")
        logger.info("eBay pricing finished: 0 succeeded, 0 failed")
        return RunResult((), ())

    my_token = get_ebay_token()
    succeeded = []
    failed = []
    for item in items:
        # search_query is the wording that finds this item on eBay; fall back to the name if it is missing
        query = item.search_query or item.name
        logger.info("Pricing item %d (%s)", item.id, item.name)
        try:
            listings = ebay_listing_prices(query, item.keywords, item.exclude_keywords, my_token)
            result = record_ebay_listing_prices(item.id, listings)
        except SchemaVersionError:
            raise
        except Exception as exc:
            failed.append(item.id)
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            suffix = f" HTTP {status}" if status is not None else f" ({type(exc).__name__})"
            logger.warning("eBay item %d failed%s", item.id, suffix)
            if status == 429:
                break
            continue
        succeeded.append(item.id)
        logger.info(
            "Item %d: %d new, %d duplicate; %d candidates, %d outliers, %d retained",
            item.id, result.new_count, result.duplicate_count, result.candidate_count,
            result.outlier_count, result.retained_count,
        )
        if result.price_updated:
            logger.info("Stored all-time eBay arithmetic mean for item %d: %.2f USD", item.id, result.mean_usd)
        elif result.new_count == 0 and result.candidate_count == 0:
            logger.info("No valid matching eBay listings for item %d; insufficient sample, market price preserved", item.id)
        elif result.new_count == 0:
            logger.info("No new eBay listing IDs for item %d; market price preserved", item.id)
        elif result.mean_usd is None:
            logger.info("Insufficient retained eBay listings for item %d (need 5); market price preserved", item.id)
        elif not result.item_found:
            logger.info("Item %d no longer exists; market price not stored", item.id)
        else:
            logger.info("No retained-price update for item %d; market price preserved", item.id)

    outcome = RunResult(tuple(succeeded), tuple(failed))
    logger.info("eBay pricing finished: %d succeeded, %d failed", len(succeeded), len(failed))
    return outcome


def main():
    _configure_logging()
    try:
        # refresh_market_prices fetches its own token, so we do not fetch one here too
        result = refresh_market_prices()
        if result.had_failure:
            raise SystemExit(1)
        return 0
    except SchemaVersionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        logger.error("eBay pricing failed (%s)", type(exc).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())

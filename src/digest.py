# Builds the digest email from already-computed deals.
# Pure on purpose: no database, network, clock, or print, so every line is easy to test.
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Literal, Sequence
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Deal:
    item_id: int
    brand_id: int
    url: str
    title: str               # as parsed (stripped); display normalization happens in build_digest
    price_yen: int
    price_usd: float
    market_price_usd: float  # the item's effective price used for the comparison
    discount_pct: float      # (market - price_usd) / market * 100, always > 0
    image_url: str | None
    kind: Literal["fixed_price", "auction_buy_now", "auction_watch"] = "fixed_price"
    current_price_yen: int | None = None
    potential_discount_pct: float | None = None
    scheduled_end_at: datetime | None = None


@dataclass(frozen=True)
class ItemDeals:
    item_name: str
    market_price_usd: float
    deals: tuple[Deal, ...]  # shown deals, best discount first, at most TOP_DEALS_PER_ITEM
    not_shown: int = 0       # new deals for this item cut by the limit


@dataclass(frozen=True)
class Digest:
    subject: str
    html: str
    text: str
    deals: tuple[Deal, ...]  # every shown deal, in display order; main() records exactly these


def is_safe_http_url(url):
    """True only for a non-empty http(s) URL with a host and no whitespace or control characters."""
    if not isinstance(url, str) or not url:
        return False
    # reject whitespace/control characters outright instead of trusting how urlsplit strips them
    for ch in url:
        if ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F:
            return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def _e(value):
    # one escape helper, so no interpolation into HTML can skip quote=True
    return escape(value, quote=True)


def _yen(value):
    return f"¥{value:,}"


def _usd(value):
    return f"${value:,.2f}"


def _pct(value):
    return f"{value:.1f}%"


def _deal_count(n):
    return f"{n} new deal" if n == 1 else f"{n} new deals"


def _not_shown_line(k):
    return f"and {k} more new deal not shown" if k == 1 else f"and {k} more new deals not shown"


def _neutral_not_shown_line(k):
    return f"and {k} more listing not shown" if k == 1 else f"and {k} more listings not shown"


def _display_title(title):
    # collapse newlines and runs of spaces so a title can't break the layout
    return " ".join(title.split())


def _scheduled_end_line(end):
    if end is None:
        return "Scheduled end unavailable"
    jst = end.astimezone(timezone(timedelta(hours=9)))
    return f"Scheduled end {jst:%Y-%m-%d %H:%M} JST (may change)"


def _potential_line(pct):
    return f"Potential discount at current price: {_pct(pct)} (not guaranteed; bids may rise)"


def _html_row(deal, show_buy_now_label=False):
    title = _display_title(deal.title)
    safe_url = is_safe_http_url(deal.url)

    if is_safe_http_url(deal.image_url):
        img = (
            f'<img src="{_e(deal.image_url)}" alt="{_e(title)}" width="100" height="100" '
            f'style="display:block;border:0;width:100px;height:100px;">'
        )
        image_cell = f'<a href="{_e(deal.url)}">{img}</a>' if safe_url else img
    else:
        image_cell = "No image"

    listing_cell = f'<a href="{_e(deal.url)}">{_e(title)}</a>' if safe_url else _e(title)

    if deal.kind == "auction_watch":
        listing_cell += '<br><strong>Auction watch - current price (provisional)</strong>'
        listing_cell += f'<br>{_e(_potential_line(deal.discount_pct))}'
        listing_cell += f'<br>{_e(_scheduled_end_line(deal.scheduled_end_at))}'
    elif deal.kind == "auction_buy_now":
        listing_cell += '<br><strong>Buy now</strong>'
        if deal.current_price_yen is not None and deal.potential_discount_pct is not None:
            listing_cell += f'<br>Current price (provisional): {_e(_yen(deal.current_price_yen))}'
            listing_cell += f'<br>{_e(_potential_line(deal.potential_discount_pct))}'
        listing_cell += f'<br>{_e(_scheduled_end_line(deal.scheduled_end_at))}'
    elif show_buy_now_label:
        listing_cell += '<br><strong>Buy now</strong>'

    # three columns (image, listing, price) instead of five: five were too narrow on a phone.
    # market price now shows once in the section heading, and the discount sits under the price.
    # the border goes on each td because many email clients ignore borders on a tr.
    # line-height is set once on the container div (see build_digest) and inherited, and the
    # image column width comes from the header cell, to keep each row small: Gmail clips
    # messages over about 102 KB (criterion 29)
    cell_border = "border-bottom:1px solid #eee"
    discount_html = ""
    if deal.kind != "auction_watch":
        discount_html = (
            f'<br><strong style="color:#1a7f37;">{_e(_pct(deal.discount_pct) + " off")}</strong>'
        )
    return (
        "<tr>"
        f'<td style="vertical-align:top;{cell_border}">{image_cell}</td>'
        f'<td style="vertical-align:top;{cell_border}">{listing_cell}</td>'
        # align="right" plus text-align:right: both forms are kept because right alignment matters
        # nowrap keeps the yen amount from splitting across lines in the narrow price column
        f'<td align="right" style="vertical-align:top;text-align:right;white-space:nowrap;{cell_border}">'
        f"{_e(_yen(deal.price_yen))}<br>{_e(_usd(deal.price_usd))}"
        f"{discount_html}</td>"
        "</tr>"
    )


def build_digest(sections: Sequence[ItemDeals], run_date: date) -> Digest | None:
    """Returns the subject, HTML, and text for the shown deals, or None when there are none."""
    # sections with no shown deal are ignored, even if they have a not_shown count
    sections = [s for s in sections if s.deals]
    if not sections:
        return None

    all_deals = tuple(deal for s in sections for deal in s.deals)
    has_watch = any(deal.kind == "auction_watch" for deal in all_deals)
    has_auction = any(deal.kind != "fixed_price" for deal in all_deals)
    # the subject holds only a count and a date, never seller text, so header injection can't happen
    if has_watch:
        count = f"{len(all_deals)} listing" if len(all_deals) == 1 else f"{len(all_deals)} listings"
        subject = f"RainCheck: {count} to review - {run_date.isoformat()}"
    else:
        subject = f"RainCheck: {_deal_count(len(all_deals))} - {run_date.isoformat()}"
    safe_subject = _e(subject)

    html_parts = [
        "<!DOCTYPE html>",
        # the viewport tag stops phones from shrinking the page to a desktop-width view
        f'<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{safe_subject}</title></head>',
        '<body style="margin:0;padding:16px;font-family:Arial,Helvetica,sans-serif;color:#222222;">',
        # 600px is the conventional email width: fits Gmail's reading pane and phones
        (
            '<div style="max-width:600px;margin:0 auto;line-height:1.4;overflow-wrap:anywhere;word-break:break-word;">'
            if has_auction else '<div style="max-width:600px;margin:0 auto;line-height:1.4;">'
        ),
        # 18px so the longest realistic subject fits a 375px phone without breaking the date at a hyphen
        f'<h1 style="font-size:18px;">{safe_subject}</h1>',
    ]
    text_lines = [subject, ""]

    for section in sections:
        market = _e("market " + _usd(section.market_price_usd))
        html_parts.append(
            f'<h2 style="font-size:16px;margin:0 0 8px;">{_e(section.item_name)} '
            f'<span style="font-weight:normal;color:#666;">({market})</span></h2>'
        )
        # margin-bottom keeps the not-shown line and the next heading from touching the table
        table_style = "border-collapse:collapse;width:100%;margin-bottom:16px;"
        price_width = ""
        if has_auction:
            # The fixed columns keep a long auction warning from pushing Price off a phone.
            table_style = "border-collapse:collapse;table-layout:fixed;width:100%;margin-bottom:16px;"
            price_width = "width:80px;"
        html_parts.append(
            '<table width="100%" cellpadding="8" cellspacing="0" '
            f'style="{table_style}">'
        )
        # borders go on the th cells because many clients ignore them on tr; a second border on
        # the tr would only add bytes (it repeats per section) for clients that already draw this one
        th_border = "border-bottom:1px solid #ddd"
        html_parts.append(
            "<tr>"
            # left cells rely on align="left" alone (left is also the fallback if a client drops the
            # attribute); right alignment is the one that matters, so Price keeps both forms to be safe
            f'<th align="left" style="width:100px;{th_border}">Image</th>'
            f'<th align="left" style="{th_border}">Listing</th>'
            f'<th align="right" style="{price_width}text-align:right;{th_border}">Price</th>'
            "</tr>"
        )
        text_lines.append(f"{section.item_name} (market price {_usd(section.market_price_usd)})")
        text_lines.append("")

        for number, deal in enumerate(section.deals, start=1):
            html_parts.append(_html_row(deal, show_buy_now_label=has_watch))
            text_lines.append(f"{number}. {_display_title(deal.title)}")
            if deal.kind == "auction_watch":
                text_lines.append("   Auction watch - current price (provisional)")
                text_lines.append(f"   {_yen(deal.price_yen)} ({_usd(deal.price_usd)})")
                text_lines.append(f"   {_potential_line(deal.discount_pct)}")
                text_lines.append(f"   {_scheduled_end_line(deal.scheduled_end_at)}")
            else:
                if has_watch or deal.kind == "auction_buy_now":
                    text_lines.append("   Buy now")
                text_lines.append(
                    f"   {_yen(deal.price_yen)} ({_usd(deal.price_usd)}) - {_pct(deal.discount_pct)} below market"
                )
                if deal.kind == "auction_buy_now":
                    if deal.current_price_yen is not None and deal.potential_discount_pct is not None:
                        text_lines.append(f"   Current price (provisional): {_yen(deal.current_price_yen)}")
                        text_lines.append(f"   {_potential_line(deal.potential_discount_pct)}")
                    text_lines.append(f"   {_scheduled_end_line(deal.scheduled_end_at)}")
            # an unsafe URL never reaches the text part either
            if is_safe_http_url(deal.url):
                text_lines.append(f"   {deal.url}")
            text_lines.append("")

        html_parts.append("</table>")
        if section.not_shown > 0:
            has_section_watch = any(deal.kind == "auction_watch" for deal in section.deals)
            overflow = (
                _neutral_not_shown_line(section.not_shown) if has_section_watch
                else _not_shown_line(section.not_shown)
            )
            html_parts.append(f"<p>{_e(overflow)}</p>")
            text_lines.append(overflow)
            text_lines.append("")

    html_parts.append("</div></body></html>")

    # drop the trailing blank line so the text ends with exactly one newline
    text = "\n".join(text_lines).rstrip("\n") + "\n"
    return Digest(subject, "\n".join(html_parts) + "\n", text, all_deals)

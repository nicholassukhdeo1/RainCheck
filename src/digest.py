# Builds the digest email from already-computed deals.
# Pure on purpose: no database, network, clock, or print, so every line is easy to test.
from dataclasses import dataclass
from datetime import date
from html import escape
from typing import Sequence
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


def _display_title(title):
    # collapse newlines and runs of spaces so a title can't break the layout
    return " ".join(title.split())


def _html_row(deal):
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

    return (
        "<tr>"
        f"<td>{image_cell}</td>"
        f"<td>{listing_cell}</td>"
        f"<td>{_e(_yen(deal.price_yen))}<br>{_e(_usd(deal.price_usd))}</td>"
        f"<td>{_e(_usd(deal.market_price_usd))}</td>"
        f"<td>{_e(_pct(deal.discount_pct))}</td>"
        "</tr>"
    )


def build_digest(sections: Sequence[ItemDeals], run_date: date) -> Digest | None:
    """Returns the subject, HTML, and text for the shown deals, or None when there are none."""
    # sections with no shown deal are ignored, even if they have a not_shown count
    sections = [s for s in sections if s.deals]
    if not sections:
        return None

    all_deals = tuple(deal for s in sections for deal in s.deals)
    # the subject holds only a count and a date, never seller text, so header injection can't happen
    subject = f"RainCheck: {_deal_count(len(all_deals))} - {run_date.isoformat()}"
    safe_subject = _e(subject)

    html_parts = [
        "<!DOCTYPE html>",
        f'<html><head><meta charset="utf-8"><title>{safe_subject}</title></head>',
        '<body style="margin:0;padding:16px;font-family:Arial,Helvetica,sans-serif;color:#222222;">',
        # 600px is the conventional email width: fits Gmail's reading pane and phones
        '<div style="max-width:600px;margin:0 auto;">',
        f'<h1 style="font-size:20px;">{safe_subject}</h1>',
    ]
    text_lines = [subject, ""]

    for section in sections:
        html_parts.append(f'<h2 style="font-size:16px;">{_e(section.item_name)}</h2>')
        html_parts.append('<table width="100%" cellpadding="6" style="border-collapse:collapse;width:100%;">')
        html_parts.append(
            "<tr><th>Image</th><th>Listing</th><th>Price</th><th>Market</th><th>Discount</th></tr>"
        )
        text_lines.append(f"{section.item_name} (market price {_usd(section.market_price_usd)})")
        text_lines.append("")

        for number, deal in enumerate(section.deals, start=1):
            html_parts.append(_html_row(deal))
            text_lines.append(f"{number}. {_display_title(deal.title)}")
            text_lines.append(
                f"   {_yen(deal.price_yen)} ({_usd(deal.price_usd)}) - {_pct(deal.discount_pct)} below market"
            )
            # an unsafe URL never reaches the text part either
            if is_safe_http_url(deal.url):
                text_lines.append(f"   {deal.url}")
            text_lines.append("")

        html_parts.append("</table>")
        if section.not_shown > 0:
            html_parts.append(f"<p>{_e(_not_shown_line(section.not_shown))}</p>")
            text_lines.append(_not_shown_line(section.not_shown))
            text_lines.append("")

    html_parts.append("</div></body></html>")

    # drop the trailing blank line so the text ends with exactly one newline
    text = "\n".join(text_lines).rstrip("\n") + "\n"
    return Digest(subject, "\n".join(html_parts) + "\n", text, all_deals)

"""Yahoo pipeline tests.

RC-07 rewrote the RC-01 criteria 14, 45, 46, 47 against parse_listings (they keep
their RC-01 names) and added criteria 20-29.
RC-15 replaced report_item_deals with find_item_deals / build_sections and made main()
send one digest; the superseded tests keep their names and now run main().
"""
import dataclasses
import re
from datetime import date

from bs4 import BeautifulSoup

import pytest
import requests

import db
import yahoo_scrape
from dbhelpers import rows as sql_rows, run as sql_run, set_user_version, sha256
from yahoo_scrape import RICK_OWENS_URL

URL_1 = "https://auctions.yahoo.co.jp/jp/auction/t1000000001"
URL_3 = "https://auctions.yahoo.co.jp/jp/auction/t1000000003"
URL_4 = "https://auctions.yahoo.co.jp/jp/auction/t1000000004"
X1 = "https://auctions.yahoo.co.jp/jp/auction/x1000000001"
X3 = "https://auctions.yahoo.co.jp/jp/auction/x1000000003"
Z2 = "https://paypayfleamarket.yahoo.co.jp/item/z1000000002"


def _html(fixtures_dir, name):
    return (fixtures_dir / "yahoo" / name).read_text(encoding="utf-8")


@pytest.fixture
def minimal_listings(fixtures_dir):
    return yahoo_scrape.parse_listings(_html(fixtures_dir, "brand_page_minimal.html"))


@pytest.fixture
def malformed_listings(fixtures_dir):
    return yahoo_scrape.parse_listings(_html(fixtures_dir, "brand_page_malformed.html"))


@pytest.fixture
def yahoo_http(fake_http, fixtures_dir):
    fake_http.add("GET", RICK_OWENS_URL, text=_html(fixtures_dir, "brand_page_minimal.html"))
    return fake_http


def _item(name):
    return [i for i in db.get_tracked_items() if i.name == name][0]


@pytest.fixture
def ramones_item(seeded_db):
    return _item("rick owens ramones")


def _rows(conn):
    return conn.execute("SELECT url, price_yen, image_url FROM listings ORDER BY url").fetchall()


# ---- helpers for the pure tests ----------------------------------------------------

AUCTION = "https://auctions.yahoo.co.jp/jp/auction/"


def inline_item(item_id=1, name="rick owens ramones", keywords=("ramones", "ramon"),
                exclude=(), price=800.0, brand_id=1):
    return db.TrackedItem(
        id=item_id, brand_id=brand_id, brand="Rick Owens", name=name, search_query=None,
        keywords=tuple(keywords), exclude_keywords=tuple(exclude),
        manual_price_usd=price, market_price_usd=None,
    )


def inline_listing(n, title=None, yen=10000, image=None):
    return yahoo_scrape.YahooListing(
        auction_id=f"t{n}", url=f"{AUCTION}t{n}", title=title or f"Rick Owens Ramones {n}",
        price_yen=yen, image_url=image,
    )


def seven_ramones_listings():
    # 10000..70000 yen against an 800 USD market: all are deals, cheapest has the best discount
    return [inline_listing(n, yen=10000 * n) for n in range(1, 8)]


def _soup(html):
    return BeautifulSoup(html, "html.parser")


def _row_urls(html):
    """The listing URL of each data row, in order, read from the Listing cell's link."""
    urls = []
    for tr in _soup(html).find_all("tr"):
        tds = tr.find_all("td")
        if tds:
            urls.append(tds[1].find("a")["href"])
    return urls


# ---- Finding deals (criteria 1-9) --------------------------------------------------


def test_rc15_c1_find_item_deals_returns_both_deals_best_first_with_full_fields(
    ramones_item, minimal_listings
):
    from digest import Deal

    deals = yahoo_scrape.find_item_deals(ramones_item, minimal_listings, set())
    assert [d.url for d in deals] == [URL_1, URL_4]
    assert all(isinstance(d, Deal) for d in deals)
    first, second = deals
    assert first.item_id == ramones_item.id
    assert first.brand_id == ramones_item.brand_id
    assert first.title == "Rick Owens Ramones High Top 42"
    assert first.price_yen == 50000
    assert first.price_usd == pytest.approx(310.0)
    assert first.market_price_usd == 800.0
    assert first.discount_pct == pytest.approx(61.25)
    assert first.image_url == "https://auc-pctr.c.yimg.jp/i/test/1.jpg"
    assert second.discount_pct == pytest.approx(22.5)


def test_rc15_c2_skip_urls_are_excluded(ramones_item, minimal_listings):
    deals = yahoo_scrape.find_item_deals(ramones_item, minimal_listings, {URL_1})
    assert [d.url for d in deals] == [URL_4]


def test_rc15_c3_exclude_keywords_from_the_item_apply(ramones_item, minimal_listings):
    item = dataclasses.replace(ramones_item, exclude_keywords=("high",))
    assert [d.url for d in yahoo_scrape.find_item_deals(item, minimal_listings, set())] == [URL_4]


def test_rc15_c4_effective_price_drives_the_deal_math(ramones_item, minimal_listings):
    item = dataclasses.replace(ramones_item, market_price_usd=400.0)
    assert item.effective_price_usd == 400.0
    deals = yahoo_scrape.find_item_deals(item, minimal_listings, set())
    assert [d.url for d in deals] == [URL_1]
    assert deals[0].discount_pct == pytest.approx(22.5)
    assert deals[0].market_price_usd == 400.0


def test_rc15_c5_no_usable_price_returns_empty_and_prints_nothing(
    ramones_item, minimal_listings, capsys
):
    item = dataclasses.replace(ramones_item, market_price_usd=None, manual_price_usd=None)
    assert yahoo_scrape.find_item_deals(item, minimal_listings, set()) == []
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_rc15_c6_a_listing_exactly_at_market_is_not_a_deal(ramones_item, minimal_listings):
    item = dataclasses.replace(ramones_item, market_price_usd=310.0)
    assert [d.url for d in yahoo_scrape.find_item_deals(item, minimal_listings, set())] == []
    # strictly above market is not a deal either
    item = dataclasses.replace(ramones_item, market_price_usd=300.0)
    assert yahoo_scrape.find_item_deals(item, minimal_listings, set()) == []


def test_rc15_c7_equal_discounts_keep_input_order():
    listings = [inline_listing(1, "Ramones A", 20000), inline_listing(2, "Ramones B", 20000),
                inline_listing(3, "Ramones C", 20000)]
    deals = yahoo_scrape.find_item_deals(inline_item(), listings, set())
    assert [d.title for d in deals] == ["Ramones A", "Ramones B", "Ramones C"]


def test_rc15_c8_brand_comes_from_the_item_not_a_name_lookup(ramones_item, minimal_listings):
    item = dataclasses.replace(ramones_item, brand="Not In Database")
    deals = yahoo_scrape.find_item_deals(item, minimal_listings, set())
    assert [d.url for d in deals] == [URL_1, URL_4]
    assert {d.brand_id for d in deals} == {ramones_item.brand_id}


def test_rc15_c9_pure_functions_need_no_database(tmp_path):
    path = tmp_path / "test.db"
    assert not path.exists()
    items = [inline_item(1), inline_item(2, "pod shorts", keywords=("pod short",), price=300.0)]
    listings = [inline_listing(1, yen=20000), inline_listing(2, "Rick Owens Pod Shorts", 10000)]
    deals = yahoo_scrape.find_item_deals(items[0], listings, set())
    sections = yahoo_scrape.build_sections(items, listings, set())
    assert [d.url for d in deals] == [f"{AUCTION}t1"]
    assert [s.item_name for s in sections] == ["rick owens ramones", "pod shorts"]
    assert not path.exists()


# ---- Building sections (criteria 10-13) --------------------------------------------


def test_rc15_c10_top_five_per_item_best_discount_first_with_not_shown_count():
    from digest import ItemDeals

    sections = yahoo_scrape.build_sections([inline_item()], seven_ramones_listings(), set())
    assert len(sections) == 1
    sec = sections[0]
    assert isinstance(sec, ItemDeals)
    assert sec.item_name == "rick owens ramones"
    assert sec.market_price_usd == 800.0
    assert [d.price_yen for d in sec.deals] == [10000, 20000, 30000, 40000, 50000]
    assert sec.not_shown == 2


def test_rc15_c10_default_limit_is_five_and_limit_is_adjustable():
    assert yahoo_scrape.TOP_DEALS_PER_ITEM == 5
    sec = yahoo_scrape.build_sections([inline_item()], seven_ramones_listings(), set(), per_item_limit=2)[0]
    assert len(sec.deals) == 2 and sec.not_shown == 5


def test_rc15_c11_a_listing_that_is_a_deal_for_two_items_appears_only_under_the_first():
    first = inline_item(1, "first", keywords=("ramones",))
    second = inline_item(2, "second", keywords=("ramones",))
    listings = [inline_listing(1, yen=20000)]
    sections = yahoo_scrape.build_sections([first, second], listings, set())
    assert [s.item_name for s in sections] == ["first"]


def test_rc15_c11_claim_holds_even_when_the_first_section_cut_the_listing():
    first = inline_item(1, "first", keywords=("ramones",))
    second = inline_item(2, "second", keywords=("sneakers",))
    listings = [
        inline_listing(1, "Rick Owens Ramones High", 10000),
        inline_listing(2, "Rick Owens Ramones Sneakers", 20000),
    ]
    sections = yahoo_scrape.build_sections([first, second], listings, set(), per_item_limit=1)
    assert [s.item_name for s in sections] == ["first"]
    assert [d.url for d in sections[0].deals] == [f"{AUCTION}t1"]
    assert sections[0].not_shown == 1


def test_rc15_c11_second_item_keeps_its_other_deals():
    first = inline_item(1, "first", keywords=("ramones",))
    second = inline_item(2, "second", keywords=("ramones", "geobasket"))
    listings = [inline_listing(1, "Ramones A", 20000), inline_listing(2, "Geobasket B", 30000)]
    sections = yahoo_scrape.build_sections([first, second], listings, set())
    assert [(s.item_name, [d.title for d in s.deals]) for s in sections] == [
        ("first", ["Ramones A"]), ("second", ["Geobasket B"]),
    ]


def test_rc15_c12_items_without_deals_or_price_make_no_section_and_order_is_input_order():
    items = [
        inline_item(1, "first", keywords=("ramones",)),
        inline_item(2, "no price", keywords=("ramones",), price=None),
        inline_item(3, "no match", keywords=("zzz",)),
        inline_item(4, "last", keywords=("geobasket",)),
    ]
    listings = [inline_listing(1, "Ramones A", 20000), inline_listing(2, "Geobasket B", 30000)]
    sections = yahoo_scrape.build_sections(items, listings, set())
    assert [s.item_name for s in sections] == ["first", "last"]


def test_rc15_c13_known_urls_are_in_no_section():
    listings = [inline_listing(1, yen=20000), inline_listing(2, yen=30000)]
    sections = yahoo_scrape.build_sections([inline_item()], listings, {f"{AUCTION}t1"})
    assert [d.url for s in sections for d in s.deals] == [f"{AUCTION}t2"]
    assert yahoo_scrape.build_sections([inline_item()], listings, {f"{AUCTION}t1", f"{AUCTION}t2"}) == []


def test_rc15_c13_known_urls_may_be_any_collection():
    listings = [inline_listing(1, yen=20000)]
    for known in ([f"{AUCTION}t1"], (f"{AUCTION}t1",), frozenset({f"{AUCTION}t1"})):
        assert yahoo_scrape.build_sections([inline_item()], listings, known) == []


# ---- Pipeline through main() (criteria 39-47) --------------------------------------


def test_c45_inserts_only_the_two_matching_cheap_listings(yahoo_http, seeded_db, sent_emails, db_conn):
    yahoo_scrape.main()
    assert [(r["url"], r["price_yen"]) for r in _rows(db_conn)] == [(URL_1, 50000), (URL_4, 100000)]


def test_rc15_c40_recorded_rows_carry_the_item_brand(yahoo_http, seeded_db, sent_emails, db_conn):
    yahoo_scrape.main()
    stored = db_conn.execute("SELECT url, brand_id FROM listings ORDER BY url").fetchall()
    assert [r["url"] for r in stored] == [URL_1, URL_4]
    assert {r["brand_id"] for r in stored} == {_item("rick owens ramones").brand_id}


def test_c46_one_email_sorted_by_discount_with_percentages(yahoo_http, seeded_db, sent_emails):
    yahoo_scrape.main()
    assert len(sent_emails) == 1
    subject, html, text = sent_emails[0]
    assert re.fullmatch(r"RainCheck: 2 new deals - \d{4}-\d{2}-\d{2}", subject)
    assert _row_urls(html) == [URL_1, URL_4]
    rows = [tr for tr in _soup(html).find_all("tr") if tr.find("td")]
    assert [r.find_all("td")[2].find("strong").get_text() for r in rows] == ["61.3% off", "22.5% off"]
    assert "cheaper than market" not in html and "cheaper than market" not in text
    assert text.index(URL_1) < text.index(URL_4)


def test_c47_second_run_inserts_no_new_rows(yahoo_http, seeded_db, sent_emails, db_conn, capsys):
    """RC-15 criterion 41: the second run sends nothing and says so."""
    yahoo_scrape.main()
    first = _rows(db_conn)
    capsys.readouterr()
    yahoo_scrape.main()
    assert _rows(db_conn) == first
    assert len(first) == 2
    assert len(sent_emails) == 1
    assert yahoo_scrape.NO_DEALS_LINE == "No new deals; no email sent."
    assert "No new deals; no email sent." in capsys.readouterr().out


def test_rc07_c24_malformed_fixture_pipeline_rows_and_email(
    fake_http, fixtures_dir, seeded_db, sent_emails, db_conn
):
    fake_http.add("GET", RICK_OWENS_URL, text=_html(fixtures_dir, "brand_page_malformed.html"))
    yahoo_scrape.main()
    rows = {r["url"]: r for r in _rows(db_conn)}
    assert set(rows) == {X1, Z2, X3}
    assert rows[Z2]["price_yen"] == 40000
    assert rows[X3]["image_url"] is None
    assert len(sent_emails) == 1
    html = sent_emails[0][1]
    assert _row_urls(html) == [X3, X1, Z2]
    soup = _soup(html)
    assert len(soup.find_all("img")) == 2
    first_cells = [tr.find_all("td")[0] for tr in soup.find_all("tr") if tr.find("td")]
    assert first_cells[0].get_text(strip=True) == "No image"
    assert first_cells[0].find("img") is None
    assert first_cells[1].find("img") is not None and first_cells[2].find("img") is not None
    discounts = [
        tr.find_all("td")[2].find("strong").get_text() for tr in soup.find_all("tr") if tr.find("td")
    ]
    # 76.75 is exact in binary, and format() rounds the tie to even, so this is always 76.8
    assert discounts == ["84.5% off", "76.8% off", "69.0% off"]


def test_rc07_c25_null_market_price_skips_item(
    yahoo_http, seeded_db, sent_emails, db_conn, capsys
):
    """RC-15 criterion 46."""
    db_conn.execute(
        "UPDATE items SET market_price_usd = NULL, manual_price_usd = NULL WHERE name = ?",
        ("rick owens ramones",),
    )
    db_conn.commit()
    yahoo_scrape.main()
    assert _rows(db_conn) == []
    assert sent_emails == []
    out = capsys.readouterr().out
    assert "Skipping rick owens ramones: no usable market price." in out
    assert "No new deals; no email sent." in out


def test_rc15_c42_deals_are_recorded_only_after_the_send(yahoo_http, seeded_db, monkeypatch):
    seen_during_send = []

    def inspecting_send(subject, html_body, text_body):
        seen_during_send.append(sql_rows(seeded_db, "SELECT url FROM listings"))
        return True

    monkeypatch.setattr(yahoo_scrape, "send_email", inspecting_send)
    yahoo_scrape.main()
    assert seen_during_send == [[]]
    assert [r[0] for r in sql_rows(seeded_db, "SELECT url FROM listings ORDER BY url")] == [URL_1, URL_4]


def test_rc04_c1_successful_digest_records_each_displayed_deals_item_association(
    yahoo_http, seeded_db, sent_emails
):
    yahoo_scrape.main()
    item_id = _item("rick owens ramones").id
    matches = sql_rows(
        seeded_db,
        "SELECT l.url, m.item_id FROM listing_matches m "
        "JOIN listings l ON l.id = m.listing_id ORDER BY l.url",
    )
    assert matches == [(URL_1, item_id), (URL_4, item_id)]
    assert len(sent_emails) == 1


def test_rc04_digest_persists_deal_association_for_each_item(
    fake_http, fixtures_dir, seeded_db, sent_emails
):
    html = _html(fixtures_dir, "brand_page_minimal.html").replace(
        "Rick Owens Geobasket 43", "Rick Owens Pod Shorts 43"
    )
    fake_http.add("GET", RICK_OWENS_URL, text=html)

    yahoo_scrape.main()

    item_ids = {
        "rick owens ramones": _item("rick owens ramones").id,
        "pod shorts": _item("pod shorts").id,
    }
    matches = sql_rows(
        seeded_db,
        "SELECT l.url, m.item_id FROM listing_matches m "
        "JOIN listings l ON l.id = m.listing_id ORDER BY l.url",
    )
    assert matches == [
        (URL_1, item_ids["rick owens ramones"]),
        (URL_3, item_ids["pod shorts"]),
        (URL_4, item_ids["rick owens ramones"]),
    ]
    assert len(sent_emails) == 1


def test_rc04_c5_and_c6_non_deal_is_not_stored_and_later_deal_is_sent(
    yahoo_http, seeded_db, sent_emails, db_conn
):
    db_conn.execute(
        "UPDATE items SET market_price_usd = 100.0 WHERE name = ?",
        ("rick owens ramones",),
    )
    db_conn.commit()
    yahoo_scrape.main()
    assert sql_rows(seeded_db, "SELECT url FROM listings") == []
    assert sql_rows(seeded_db, "SELECT listing_id, item_id FROM listing_matches") == []
    assert sent_emails == []

    db_conn.execute(
        "UPDATE items SET market_price_usd = 800.0 WHERE name = ?",
        ("rick owens ramones",),
    )
    db_conn.commit()
    yahoo_scrape.main()
    assert len(sent_emails) == 1
    assert {row[0] for row in sql_rows(seeded_db, "SELECT url FROM listings")} == {URL_1, URL_4}
    assert len(sql_rows(seeded_db, "SELECT listing_id FROM listing_matches")) == 2


def test_rc15_c43_failed_send_exits_1_with_one_error_line_and_records_nothing(
    yahoo_http, seeded_db, monkeypatch, capsys
):
    calls = []

    def failing_send(subject, html_body, text_body):
        calls.append(subject)
        return False

    monkeypatch.setattr(yahoo_scrape, "send_email", failing_send)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    assert exc.value.code == 1
    assert len(calls) == 1
    assert capsys.readouterr().err == (
        "Error: digest email not sent, so its deals were not recorded and "
        "will be offered again next run (2 deals).\n"
    )
    assert sql_rows(seeded_db, "SELECT url FROM listings") == []
    assert sql_rows(seeded_db, "SELECT listing_id, item_id FROM listing_matches") == []

    delivered = []

    def ok_send(subject, html_body, text_body):
        delivered.append((subject, html_body))
        return True

    monkeypatch.setattr(yahoo_scrape, "send_email", ok_send)
    yahoo_scrape.main()
    assert len(delivered) == 1
    assert _row_urls(delivered[0][1]) == [URL_1, URL_4]
    assert len(sql_rows(seeded_db, "SELECT url FROM listings")) == 2
    assert len(sql_rows(seeded_db, "SELECT listing_id, item_id FROM listing_matches")) == 2


def test_rc15_c43_failed_send_message_uses_singular_for_one_deal(
    yahoo_http, seeded_db, monkeypatch, capsys
):
    sql_run(seeded_db, "UPDATE items SET manual_price_usd = 400.0 WHERE name = ?", ("rick owens ramones",))
    monkeypatch.setattr(yahoo_scrape, "send_email", lambda s, h, t: False)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    assert exc.value.code == 1
    assert capsys.readouterr().err.endswith("will be offered again next run (1 deal).\n")


def test_rc15_c44_a_listing_stored_by_an_earlier_run_is_not_shown(
    yahoo_http, seeded_db, sent_emails
):
    db.insert_listing(_item("rick owens ramones").brand_id, URL_1, "Rick Owens Ramones High Top 42", 50000, None)
    yahoo_scrape.main()
    assert len(sent_emails) == 1
    assert sent_emails[0][0].startswith("RainCheck: 1 new deal - ")
    assert _row_urls(sent_emails[0][1]) == [URL_4]


def test_rc15_c45_top_five_per_item_end_to_end_and_leftovers_next_run(
    yahoo_http, seeded_db, sent_emails, monkeypatch
):
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda html: seven_ramones_listings())
    listings = seven_ramones_listings()
    yahoo_scrape.main()
    assert len(sent_emails) == 1
    subject, html, text = sent_emails[0]
    assert subject.startswith("RainCheck: 5 new deals - ")
    assert _row_urls(html) == [l.url for l in listings[:5]]
    assert "and 2 more new deals not shown" in html
    assert "and 2 more new deals not shown" in text
    stored = [r[0] for r in sql_rows(seeded_db, "SELECT url FROM listings ORDER BY url")]
    assert stored == sorted(l.url for l in listings[:5])

    yahoo_scrape.main()
    assert len(sent_emails) == 2
    subject2, html2, text2 = sent_emails[1]
    assert subject2.startswith("RainCheck: 2 new deals - ")
    assert _row_urls(html2) == [l.url for l in listings[5:]]
    assert "not shown" not in html2 and "not shown" not in text2


def test_rc15_c39_pipeline_main_prints_no_per_deal_console_lines(
    yahoo_http, seeded_db, sent_emails, capsys
):
    yahoo_scrape.main()
    out = capsys.readouterr().out
    assert "Title:" not in out and "Product Link:" not in out and "--- Yahoo Auctions Update" not in out


def test_c14_main_works_from_unrelated_cwd_and_emails_once_per_item(
    yahoo_http, test_catalog, seeded_db, sent_emails, monkeypatch, tmp_path
):
    """RC-15 criterion 39: one digest for all items."""
    monkeypatch.chdir(tmp_path)
    yahoo_scrape.main()
    assert len(sent_emails) == 1
    subject, html, _text = sent_emails[0]
    assert re.fullmatch(r"RainCheck: 2 new deals - \d{4}-\d{2}-\d{2}", subject)
    soup = _soup(html)
    assert [h.get_text() for h in soup.find_all("h2")] == [
        "rick owens ramones (market $800.00)"
    ]
    assert _row_urls(html) == [URL_1, URL_4]
    assert len(yahoo_http.calls) == 1
    method, url, kwargs = yahoo_http.calls[0]
    assert (method, url) == ("GET", RICK_OWENS_URL)
    assert kwargs.get("timeout") == 30


def test_rc15_c50_dry_run_end_to_end_prints_whole_digest_and_records(
    yahoo_http, seeded_db, monkeypatch, capsys
):
    import smtplib

    def boom(*args, **kwargs):
        raise AssertionError("smtplib.SMTP must not be constructed in dry run")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    monkeypatch.setenv("RAINCHECK_DRY_RUN", "1")
    yahoo_scrape.main()
    out = capsys.readouterr().out
    assert "[DRY RUN] Email not sent" in out
    assert re.search(r"^Subject: RainCheck: 2 new deals - \d{4}-\d{2}-\d{2}$", out, re.MULTILINE)
    assert "--- text/plain ---" in out
    assert "--- text/html ---" in out
    assert URL_1 in out
    assert [r[0] for r in sql_rows(seeded_db, "SELECT url FROM listings ORDER BY url")] == [URL_1, URL_4]

    yahoo_scrape.main()
    out2 = capsys.readouterr().out
    assert "No new deals; no email sent." in out2
    assert "[DRY RUN]" not in out2


def test_rc15_c41_empty_yahoo_page_sends_nothing(
    fake_http, fixtures_dir, seeded_db, sent_emails, monkeypatch, capsys
):
    fake_http.add("GET", RICK_OWENS_URL, text=_html(fixtures_dir, "brand_page_minimal.html"))
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda html: [])
    yahoo_scrape.main()
    assert sent_emails == []
    assert "No new deals; no email sent." in capsys.readouterr().out
    assert sql_rows(seeded_db, "SELECT url FROM listings") == []


# ---- RC-03b item-based pure rules moved to find_item_deals: see criteria 3, 4, 8 ----


def test_rc03b_c9_exclude_keywords_from_the_item_apply(ramones_item, minimal_listings):
    """RC-15 criterion 3."""
    item = dataclasses.replace(ramones_item, exclude_keywords=("high",))
    deals = yahoo_scrape.find_item_deals(item, minimal_listings, set())
    assert [d.url for d in deals] == [URL_4]


def test_rc03b_c10_brand_comes_from_the_item_not_a_name_lookup(ramones_item, minimal_listings):
    """RC-15 criterion 8."""
    item = dataclasses.replace(ramones_item, brand="Not In Database")
    deals = yahoo_scrape.find_item_deals(item, minimal_listings, set())
    assert [d.url for d in deals] == [URL_1, URL_4]
    assert {d.brand_id for d in deals} == {ramones_item.brand_id}


def test_rc03b_c11_effective_price_of_the_item_drives_the_deal_math(
    seeded_db, ramones_item, minimal_listings
):
    """RC-15 criterion 4."""
    db.update_market_price(ramones_item.id, 400.0)
    item = _item("rick owens ramones")
    assert item.effective_price_usd == 400.0
    deals = yahoo_scrape.find_item_deals(item, minimal_listings, set())
    assert [d.url for d in deals] == [URL_1]
    assert deals[0].discount_pct == pytest.approx(22.5)


def test_rc03b_c12_item_without_usable_price_is_skipped_with_a_line(
    yahoo_http, seeded_db, sent_emails, db_conn, capsys
):
    """RC-15 criterion 46."""
    db_conn.execute(
        "UPDATE items SET market_price_usd = NULL, manual_price_usd = NULL WHERE name = ?",
        ("rick owens ramones",),
    )
    db_conn.commit()
    yahoo_scrape.main()
    assert _rows(db_conn) == []
    assert sent_emails == []
    assert "Skipping rick owens ramones: no usable market price." in capsys.readouterr().out


# ---- Fetching and failure behavior (criteria 26-29) --------------------------------


def test_rc07_c26_rick_owens_url_is_canonical():
    assert RICK_OWENS_URL == (
        "https://auctions.yahoo.co.jp/category/list/23000"
        "?auccat=23000&s1=new&o1=d&brand_id=103538&n=100"
    )


def test_rc07_c27_fetch_returns_text_and_passes_timeout(fake_http):
    fake_http.add("GET", "https://example.test/page", text="<p>ok</p>")
    assert yahoo_scrape.fetch_yahoo_page("https://example.test/page") == "<p>ok</p>"
    assert len(fake_http.calls) == 1
    assert fake_http.calls[0][0:2] == ("GET", "https://example.test/page")
    assert fake_http.calls[0][2].get("timeout") == 30


def test_rc07_c27_fetch_decodes_japanese_as_utf8(fake_http):
    fake_http.add("GET", "https://example.test/jp", text="<p>テスト フリマ</p>")
    assert yahoo_scrape.fetch_yahoo_page("https://example.test/jp") == "<p>テスト フリマ</p>"


def test_rc07_c27_fetch_raises_on_http_error(fake_http):
    fake_http.add("GET", "https://example.test/err", text="oops", status=500)
    with pytest.raises(requests.HTTPError):
        yahoo_scrape.fetch_yahoo_page("https://example.test/err")


def test_rc07_c27_fetch_propagates_timeout(monkeypatch):
    def boom(url, **kwargs):
        raise requests.Timeout("slow")

    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(requests.Timeout):
        yahoo_scrape.fetch_yahoo_page("https://example.test/slow")


def test_rc07_c28_http_503_raises_with_no_email_and_no_rows(
    fake_http, test_catalog, seeded_db, sent_emails, db_conn
):
    fake_http.add("GET", RICK_OWENS_URL, text="Service Unavailable", status=503)
    with pytest.raises(requests.HTTPError):
        yahoo_scrape.main()
    assert sent_emails == []
    assert _rows(db_conn) == []


def test_rc07_c29_old_layout_page_raises_with_no_email_and_no_rows(
    fake_http, test_catalog, seeded_db, sent_emails, db_conn
):
    old = '<html><body><h3>Rick Owens Ramones</h3><span class="Product__priceValue u-textRed">1円</span></body></html>'
    fake_http.add("GET", RICK_OWENS_URL, text=old)
    with pytest.raises(yahoo_scrape.YahooPageError):
        yahoo_scrape.main()
    assert sent_emails == []
    assert _rows(db_conn) == []


# ---- RC-03b main() reads the database (criteria 13-21) ------------------------------

NO_ITEMS_LINE = "No active items in the database. Add items to catalog.json, then run python3 src/db.py --seed."


def test_rc03b_c13_main_reads_exclude_keywords_from_the_database(
    yahoo_http, seeded_db, sent_emails, db_conn
):
    item_id = _item("rick owens ramones").id
    sql_run(
        seeded_db,
        "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?, 'sneakers', 'exclude')",
        (item_id,),
    )
    yahoo_scrape.main()
    assert [r["url"] for r in _rows(db_conn)] == [URL_1]


def test_rc03b_c14_main_skips_inactive_items(yahoo_http, seeded_db, sent_emails, monkeypatch):
    """RC-15 criterion 48: find_item_deals is spied, since pod shorts has no deals anyway."""
    sql_run(seeded_db, "UPDATE items SET active = 0 WHERE name = ?", ("pod shorts",))
    real = yahoo_scrape.find_item_deals
    called_for = []

    def spy(item, listings, skip_urls):
        called_for.append(item.name)
        return real(item, listings, skip_urls)

    monkeypatch.setattr(yahoo_scrape, "find_item_deals", spy)
    yahoo_scrape.main()
    assert called_for == ["rick owens ramones"]
    assert len(sent_emails) == 1
    assert len(yahoo_http.calls) == 1


def test_rc03b_c15_main_does_not_read_catalog_json(
    yahoo_http, seeded_db, sent_emails, monkeypatch, tmp_path
):
    """RC-15 criterion 49."""
    monkeypatch.setattr(db, "CATALOG_PATH", tmp_path / "does_not_exist.json")
    yahoo_scrape.main()
    assert len(sent_emails) == 1
    assert sent_emails[0][0].startswith("RainCheck: 2 new deals - ")
    assert not hasattr(yahoo_scrape, "load_catalog")


def _assert_clean_schema_error(capsys, fake_http, sent_emails, exc, needle="python3 src/db.py"):
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert err.startswith("Error: ")
    assert needle in err
    assert "Traceback" not in err
    assert fake_http.calls == []
    assert sent_emails == []


def test_rc03b_c16_main_on_legacy_db_is_a_clean_error_before_any_network(
    legacy_db, fake_http, sent_emails, capsys
):
    before = sha256(legacy_db)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    _assert_clean_schema_error(capsys, fake_http, sent_emails, exc)
    assert sha256(legacy_db) == before


def test_rc03b_c17_main_with_no_database_file_is_a_clean_error_and_creates_nothing(
    fake_http, sent_emails, capsys, tmp_path
):
    path = tmp_path / "test.db"
    assert not path.exists()
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    _assert_clean_schema_error(capsys, fake_http, sent_emails, exc, "python3 src/db.py --seed")
    assert not path.exists()


def test_rc03b_c18_main_with_no_items_prints_one_line_and_returns(
    temp_db, fake_http, sent_emails, capsys
):
    yahoo_scrape.main()
    assert NO_ITEMS_LINE in capsys.readouterr().out
    assert fake_http.calls == []
    assert sent_emails == []


def test_rc03b_c19_main_on_newer_schema_version_is_a_clean_error(
    temp_db, fake_http, sent_emails, capsys
):
    set_user_version(temp_db, 2)
    before = sha256(temp_db)
    with pytest.raises(SystemExit) as exc:
        yahoo_scrape.main()
    _assert_clean_schema_error(capsys, fake_http, sent_emails, exc)
    assert sha256(temp_db) == before


def test_rc03b_c21_yahoo_job_uses_the_shared_matcher():
    import matching

    assert yahoo_scrape.title_matches is matching.title_matches


def test_rc15_c54_report_item_deals_is_gone():
    import inspect

    assert not hasattr(yahoo_scrape, "report_item_deals")
    assert yahoo_scrape.YEN_TO_USD == 0.0062
    source = inspect.getsource(yahoo_scrape)
    assert "Daily Refresh" not in source
    assert '"<br>".join' not in source


# ---- Amendment 1: unencodable text (lone surrogates) and insert failure after send ---


def _surrogate_page(bad_title=None, bad_image=None):
    """A results page with one valid Ramones deal and one record carrying a lone surrogate.

    json.dumps escapes the surrogate as the six characters \\ud800, which is how Yahoo's JSON
    could carry it; json.loads turns that back into a str that cannot be encoded as UTF-8.
    """
    import json

    good = {"auctionId": "t1000000001", "title": "Rick Owens Ramones High Top 42",
            "price": 50000, "imageUrl": "https://auc-pctr.c.yimg.jp/i/test/1.jpg"}
    bad = {"auctionId": "t1000000002", "title": bad_title or "Rick Owens Ramones Bad 44",
           "price": 20000, "imageUrl": bad_image or "https://auc-pctr.c.yimg.jp/i/test/2.jpg"}
    data = {"props": {"pageProps": {"initialState": {"search": {"items": {"listing": {
        "items": [good, bad]}}}}}}}
    raw = json.dumps(data)
    assert "\\ud800" in raw
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + raw + "</script></body></html>"
    )


@pytest.mark.parametrize(
    "kwargs",
    [{"bad_title": "Rick Owens Ramones \ud800 Bad"}, {"bad_image": "https://img.test/\ud800.jpg"}],
    ids=["title", "image_url"],
)
def test_rc15_c57_lone_surrogate_makes_the_record_invalid(kwargs, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        listings = yahoo_scrape.parse_listings(_surrogate_page(**kwargs))
    assert [l.url for l in listings] == [URL_1]
    warnings = [r for r in caplog.records if r.name == "yahoo_scrape" and r.levelno >= logging.WARNING]
    assert [w.getMessage() for w in warnings] == [
        "Skipped 1 of 2 Yahoo listing records: 1 invalid, 0 duplicate"
    ]
    for l in listings:
        l.title.encode("utf-8")
        if l.image_url is not None:
            l.image_url.encode("utf-8")


@pytest.mark.parametrize(
    "kwargs",
    [{"bad_title": "Rick Owens Ramones \ud800 Bad"}, {"bad_image": "https://img.test/\ud800.jpg"}],
    ids=["title", "image_url"],
)
def test_rc15_c57_main_sends_one_digest_with_the_valid_deal(kwargs, fake_http, seeded_db, sent_emails):
    fake_http.add("GET", RICK_OWENS_URL, text=_surrogate_page(**kwargs))
    yahoo_scrape.main()
    assert len(sent_emails) == 1
    subject, html, text = sent_emails[0]
    assert subject.startswith("RainCheck: 1 new deal - ")
    assert _row_urls(html) == [URL_1]
    html.encode("utf-8")
    text.encode("utf-8")
    assert [r[0] for r in sql_rows(seeded_db, "SELECT url FROM listings")] == [URL_1]


def test_rc15_insert_failure_after_send_propagates_and_next_run_sends_only_the_rest(
    yahoo_http, seeded_db, monkeypatch
):
    delivered = []

    def ok_send(subject, html_body, text_body):
        delivered.append(html_body)
        return True

    monkeypatch.setattr(yahoo_scrape, "send_email", ok_send)
    real_record_match = yahoo_scrape.record_listing_match
    calls = []

    def flaky_record_match(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 2:
            raise RuntimeError("database is locked")
        return real_record_match(*args, **kwargs)

    monkeypatch.setattr(yahoo_scrape, "record_listing_match", flaky_record_match)
    with pytest.raises(RuntimeError, match="database is locked"):
        yahoo_scrape.main()
    assert len(delivered) == 1
    assert [r[0] for r in sql_rows(seeded_db, "SELECT url FROM listings")] == [URL_1]

    monkeypatch.setattr(yahoo_scrape, "record_listing_match", real_record_match)
    yahoo_scrape.main()
    assert len(delivered) == 2
    assert _row_urls(delivered[1]) == [URL_4]
    assert [r[0] for r in sql_rows(seeded_db, "SELECT url FROM listings ORDER BY url")] == [URL_1, URL_4]

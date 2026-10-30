"""RC-15 digest builder tests (criteria 14-30). All pure: no database, network, or clock."""
from datetime import date
from dataclasses import dataclass

import pytest
from bs4 import BeautifulSoup

import digest as digest_module
from digest import Deal, ItemDeals, build_digest, is_safe_http_url

RUN_DATE = date(2026, 10, 26)
URL_1 = "https://auctions.yahoo.co.jp/jp/auction/t1000000001"
URL_4 = "https://auctions.yahoo.co.jp/jp/auction/t1000000004"
IMG_1 = "https://auc-pctr.c.yimg.jp/i/test/1.jpg"
IMG_4 = "https://auc-pctr.c.yimg.jp/i/test/4.jpg"

BANNED_TAGS = ["script", "style", "link", "iframe", "form", "object", "embed"]


def make_deal(**overrides):
    values = dict(
        item_id=1,
        brand_id=1,
        url=URL_1,
        title="Rick Owens Ramones High Top 42",
        price_yen=50000,
        price_usd=310.0,
        market_price_usd=800.0,
        discount_pct=61.25000000000001,
        image_url=IMG_1,
    )
    values.update(overrides)
    return Deal(**values)


def deal_1():
    return make_deal()


def deal_4():
    return make_deal(
        url=URL_4, title="Rick Owens Ramon Sneakers", price_yen=100000, price_usd=620.0,
        discount_pct=22.5, image_url=IMG_4,
    )


def section(name="rick owens ramones", market=800.0, deals=None, not_shown=0):
    deals = (deal_1(),) if deals is None else tuple(deals)
    return ItemDeals(name, market, deals, not_shown)


def soup_of(d):
    return BeautifulSoup(d.html, "html.parser")


def data_rows(table):
    return [tr for tr in table.find_all("tr") if tr.find("td")]


# ---- criterion 14 ------------------------------------------------------------------


def test_rc15_c14_no_shown_deals_returns_none():
    assert build_digest([], RUN_DATE) is None
    assert build_digest([ItemDeals("x", 800.0, ())], RUN_DATE) is None


def test_rc15_c14_sections_with_empty_deals_are_ignored_even_with_not_shown():
    assert build_digest([ItemDeals("x", 800.0, (), 3)], RUN_DATE) is None
    d = build_digest([ItemDeals("empty", 800.0, (), 3), section()], RUN_DATE)
    assert d is not None
    assert [h.get_text() for h in soup_of(d).find_all("h2")] == [
        "rick owens ramones (market $800.00)"
    ]


# ---- criteria 15, 16 ---------------------------------------------------------------


def test_rc15_c15_subject_for_one_deal():
    d = build_digest([section()], RUN_DATE)
    assert d.subject == "RainCheck: 1 new deal - 2026-10-26"


def test_rc15_c15_subject_counts_shown_deals_over_two_sections_not_not_shown():
    sections = [
        section(deals=[deal_1(), deal_4()], not_shown=4),
        section(name="pod shorts", market=300.0, deals=[make_deal(item_id=2, url=URL_4 + "x")]),
    ]
    d = build_digest(sections, RUN_DATE)
    assert d.subject == "RainCheck: 3 new deals - 2026-10-26"


def test_rc15_c15_subject_carries_no_seller_text():
    evil = make_deal(title="Evil\r\nBcc: x@example.com")
    d = build_digest([section(name="name\r\nBcc: y@example.com", deals=[evil])], RUN_DATE)
    assert d.subject == "RainCheck: 1 new deal - 2026-10-26"
    assert "\n" not in d.subject and "\r" not in d.subject


def test_rc15_c16_digest_deals_are_the_shown_deals_in_display_order_without_sorting():
    a, b = deal_4(), deal_1()  # deliberately not best-first: the builder must not sort
    c = make_deal(item_id=2, url=URL_4 + "z", title="Other")
    d = build_digest([section(deals=[a, b]), section(name="pod shorts", market=300.0, deals=[c])], RUN_DATE)
    assert d.deals == (a, b, c)
    assert isinstance(d.deals, tuple)


# ---- criteria 17-20 ----------------------------------------------------------------


def test_rc15_c17_document_structure_title_h1_h2_table_and_headers():
    sections = [
        section(deals=[deal_1(), deal_4()]),
        section(name="pod shorts", market=300.0, deals=[make_deal(item_id=2, url=URL_4 + "z", title="Pod")]),
    ]
    d = build_digest(sections, RUN_DATE)
    soup = soup_of(d)
    assert [t.get_text() for t in soup.find_all("title")] == [d.subject]
    assert [h.get_text() for h in soup.find_all("h1")] == [d.subject]
    assert [h.get_text() for h in soup.find_all("h2")] == [
        "rick owens ramones (market $800.00)", "pod shorts (market $300.00)",
    ]
    tables = soup.find_all("table")
    assert len(tables) == 2
    for table in tables:
        header = table.find("tr")
        assert [th.get_text() for th in header.find_all("th")] == [
            "Image", "Listing", "Price",
        ]
    rows = data_rows(tables[0])
    assert len(rows) == 2
    assert all(len(r.find_all("td")) == 3 for r in rows)
    assert [r.find_all("td")[1].get_text(" ", strip=True) for r in rows] == [
        "Rick Owens Ramones High Top 42", "Rick Owens Ramon Sneakers",
    ]
    assert len(data_rows(tables[1])) == 1


def test_rc15_c18_cells_show_formatted_yen_usd_market_and_discount():
    d = build_digest([section()], RUN_DATE)
    cells = data_rows(soup_of(d).find("table"))[0].find_all("td")
    assert len(cells) == 3
    assert cells[1].get_text(" ", strip=True) == "Rick Owens Ramones High Top 42"
    assert cells[2].get_text(" ", strip=True) == "¥50,000 $310.00 61.3% off"
    assert soup_of(d).find("h2").find("span").get_text() == "(market $800.00)"


def test_rc15_c18_thousands_separators_in_yen_usd_and_market():
    big = make_deal(price_yen=200000, price_usd=1240.0, market_price_usd=1500.0, discount_pct=17.3333)
    d = build_digest([section(market=1500.0, deals=[big])], RUN_DATE)
    cells = data_rows(soup_of(d).find("table"))[0].find_all("td")
    assert len(cells) == 3
    assert cells[2].get_text(" ", strip=True) == "¥200,000 $1,240.00 17.3% off"
    assert soup_of(d).find("h2").find("span").get_text() == "(market $1,500.00)"


def test_rc15_c19_image_cell_has_linked_img_with_exactly_five_attributes():
    d = build_digest([section()], RUN_DATE)
    cell = data_rows(soup_of(d).find("table"))[0].find_all("td")[0]
    link = cell.find("a")
    assert link is not None and link["href"] == URL_1
    imgs = cell.find_all("img")
    assert len(imgs) == 1
    img = imgs[0]
    assert img.parent is link
    assert set(img.attrs) == {"src", "alt", "width", "height", "style"}
    assert img["src"] == IMG_1
    assert img["alt"] == "Rick Owens Ramones High Top 42"
    assert img["width"] == "100" and img["height"] == "100"


def test_rc15_c19_no_image_url_renders_no_image_text_and_no_img():
    d = build_digest([section(deals=[make_deal(image_url=None)])], RUN_DATE)
    cell = data_rows(soup_of(d).find("table"))[0].find_all("td")[0]
    assert cell.get_text(strip=True) == "No image"
    assert cell.find("img") is None
    assert soup_of(d).find("img") is None


def test_rc15_c20_listing_cell_link_has_deal_url_and_display_title():
    d = build_digest([section()], RUN_DATE)
    cell = data_rows(soup_of(d).find("table"))[0].find_all("td")[1]
    links = cell.find_all("a")
    assert len(links) == 1
    assert links[0]["href"] == URL_1
    assert links[0].get_text() == "Rick Owens Ramones High Top 42"


# ---- Amendment 2: phone-friendly three-column layout -------------------------------


def _price_cell(d):
    return data_rows(soup_of(d).find("table"))[0].find_all("td")[2]


def test_rc15_a2_exactly_one_viewport_meta_in_head_after_charset():
    soup = soup_of(build_digest([section()], RUN_DATE))
    viewports = soup.find_all("meta", attrs={"name": "viewport"})
    assert len(viewports) == 1
    assert viewports[0]["content"] == "width=device-width, initial-scale=1"
    head = soup.find("head")
    assert viewports[0].parent is head
    metas = head.find_all("meta", recursive=False)
    charset_index = next(i for i, m in enumerate(metas) if m.has_attr("charset"))
    assert metas.index(viewports[0]) > charset_index


def test_rc15_a2_every_table_has_cellpadding_8_and_cellspacing_0():
    sections = [section(), section(name="pod shorts", market=300.0, deals=[make_deal(url=URL_4 + "z")])]
    tables = soup_of(build_digest(sections, RUN_DATE)).find_all("table")
    assert len(tables) == 2
    for table in tables:
        assert table["cellpadding"] == "8"
        assert table["cellspacing"] == "0"


def test_rc15_a2_header_alignment_image_listing_left_price_right():
    header = soup_of(build_digest([section()], RUN_DATE)).find("table").find("tr")
    ths = {th.get_text(): th for th in header.find_all("th")}
    assert list(ths) == ["Image", "Listing", "Price"]
    assert ths["Image"]["align"] == "left"
    assert ths["Listing"]["align"] == "left"
    assert ths["Price"]["align"] == "right"


def test_rc15_a2_price_cell_is_right_aligned_nowrap_with_one_strong_discount_and_two_br():
    cell = _price_cell(build_digest([section()], RUN_DATE))
    style = cell["style"].replace(" ", "")
    assert "white-space:nowrap" in style
    assert "text-align:right" in style
    strongs = cell.find_all("strong")
    assert len(strongs) == 1
    assert strongs[0].get_text() == "61.3% off"
    assert len(cell.find_all("br")) == 2


def test_rc15_a2_price_cell_discount_text_format_for_each_deal():
    d = build_digest([section(deals=[deal_1(), deal_4()])], RUN_DATE)
    rows = data_rows(soup_of(d).find("table"))
    assert [r.find_all("td")[2].find("strong").get_text() for r in rows] == ["61.3% off", "22.5% off"]


# ---- RC-19 embedded images and preview rendering -----------------------------------


@dataclass(frozen=True)
class PreparedImage:
    cid: str
    content_type: str
    data: bytes


def test_rc19_c1_digest_uses_one_cid_image_for_each_prepared_displayed_deal():
    photo = PreparedImage("photo-1", "image/jpeg", b"normalized-jpeg")
    d = build_digest([section()], RUN_DATE, inline_images={URL_1: photo})

    soup = soup_of(d)
    rows = data_rows(soup.find("table"))
    image = rows[0].find("img")
    assert len(rows) == 1
    assert image["src"] == "cid:photo-1"
    assert image["width"] == image["height"] == "100"
    assert image.parent["href"] == URL_1
    assert image["alt"] == "Rick Owens Ramones High Top 42"
    assert d.inline_images == (photo,)
    assert URL_1 not in image["src"]


def test_rc19_c3_image_aware_digest_has_no_remote_image_sources():
    photo = PreparedImage("opaque-cid", "image/png", b"png")
    d = build_digest([section()], RUN_DATE, inline_images={URL_1: photo})
    assert [img["src"] for img in soup_of(d).find_all("img")] == ["cid:opaque-cid"]
    assert IMG_1 not in d.html
    assert "Rick Owens Ramones High Top 42" in d.text
    assert URL_1 in d.text


def test_rc19_c12_preview_has_visible_label_in_subject_html_and_text():
    photo = PreparedImage("photo-1", "image/jpeg", b"jpeg")
    d = build_digest([section()], RUN_DATE, inline_images={URL_1: photo}, preview=True)
    assert d.subject.startswith("RainCheck PREVIEW:")
    text = d.text.lower()
    html = soup_of(d)
    assert "one-time live search" in text
    assert "may include previously seen listings" in text
    assert "has not changed raincheck listing history" in text
    rendered = html.get_text(" ", strip=True).lower()
    assert "one-time live search" in rendered
    assert "may include previously seen listings" in rendered
    assert "has not changed raincheck listing history" in rendered
    assert [r.find_all("td")[2].get_text(" ", strip=True) for r in rows] == [
        "¥50,000 $310.00 61.3% off", "¥100,000 $620.00 22.5% off",
    ]


def test_rc15_a2_every_deal_cell_is_vertically_top_aligned():
    d = build_digest([section(deals=[deal_1(), make_deal(image_url=None, url="javascript:x")])], RUN_DATE)
    rows = data_rows(soup_of(d).find("table"))
    assert len(rows) == 2
    for row in rows:
        tds = row.find_all("td")
        assert len(tds) == 3
        for td in tds:
            assert "vertical-align:top" in td["style"].replace(" ", "")


def test_rc15_a2_heading_has_exactly_one_market_span():
    sections = [section(), section(name="pod shorts", market=1234.5, deals=[make_deal(url=URL_4 + "z")])]
    h2s = soup_of(build_digest(sections, RUN_DATE)).find_all("h2")
    assert len(h2s) == 2
    assert [len(h.find_all("span")) for h in h2s] == [1, 1]
    assert [h.find("span").get_text() for h in h2s] == ["(market $800.00)", "(market $1,234.50)"]
    assert [h.get_text() for h in h2s] == [
        "rick owens ramones (market $800.00)", "pod shorts (market $1,234.50)",
    ]


def test_rc15_a2_market_price_appears_once_per_item_not_in_every_row():
    d = build_digest([section(deals=[deal_1(), deal_4()])], RUN_DATE)
    soup = soup_of(d)
    table = soup.find("table")
    assert "$800.00" not in table.get_text()
    assert [th.get_text() for th in table.find_all("th")] == ["Image", "Listing", "Price"]
    assert soup.get_text().count("$800.00") == 1


def test_rc15_a2_header_and_deal_cells_have_bottom_borders_and_image_column_is_100px():
    sections = [section(deals=[deal_1(), deal_4()]), section(name="pod shorts", market=300.0)]
    tables = soup_of(build_digest(sections, RUN_DATE)).find_all("table")
    assert len(tables) == 2
    for table in tables:
        header = table.find("tr")
        ths = header.find_all("th")
        assert len(ths) == 3
        for th in ths:
            assert "border-bottom" in th["style"].replace(" ", "")
        image_th = next(th for th in ths if th.get_text() == "Image")
        assert "width:100px" in image_th["style"].replace(" ", "")
        rows = data_rows(table)
        assert rows
        for row in rows:
            for td in row.find_all("td"):
                assert "border-bottom" in td["style"].replace(" ", "")


def test_rc15_a2_no_class_attributes_and_no_style_element():
    soup = soup_of(build_digest([section(not_shown=2)], RUN_DATE))
    assert soup.find("style") is None
    assert all("class" not in el.attrs for el in soup.find_all(True))


# ---- criteria 21-25: HTML safety ---------------------------------------------------

EVIL_TITLE = "<script>alert(\"x\")</script> & 'q'"
EVIL_NAME = "<b>bold</b> & co"
EVIL_IMAGE = 'https://img.test/a.jpg?w=1&h=2"onerror="alert(1)'


def test_rc15_c21_title_and_item_name_are_escaped_everywhere():
    d = build_digest([section(name=EVIL_NAME, deals=[make_deal(title=EVIL_TITLE)])], RUN_DATE)
    soup = soup_of(d)
    assert soup.find("script") is None
    assert soup.find("b") is None
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &#x27;q&#x27;" in d.html
    row = data_rows(soup.find("table"))[0]
    assert row.find_all("td")[1].find("a").get_text() == EVIL_TITLE
    assert row.find("img")["alt"] == EVIL_TITLE
    assert soup.find("h2").get_text() == EVIL_NAME + " (market $800.00)"
    assert soup.find("h2").find("span").get_text() == "(market $800.00)"
    # raw markup from the seller never appears unescaped
    assert "<script>" not in d.html
    assert "<b>bold</b>" not in d.html


def test_rc15_c22_image_url_with_quote_cannot_break_out_of_the_src_attribute():
    d = build_digest([section(deals=[make_deal(image_url=EVIL_IMAGE)])], RUN_DATE)
    img = soup_of(d).find("img")
    assert img is not None
    assert img["src"] == EVIL_IMAGE
    assert set(img.attrs) == {"src", "alt", "width", "height", "style"}
    assert "w=1&amp;h=2&quot;onerror=&quot;alert(1)" in d.html
    assert 'onerror="' not in d.html


@pytest.mark.parametrize(
    "url",
    [IMG_1, "HTTP://example.test/a.jpg", "http://example.test/a.jpg"],
)
def test_rc15_c23_safe_urls(url):
    assert is_safe_http_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:image/png;base64,AAAA",
        "vbscript:x",
        "ftp://example.test/a.jpg",
        "//example.test/a.jpg",
        "https:a.jpg",
        "https:///a.jpg",
        " https://example.test/a.jpg",
        "https://example.test/a b.jpg",
        "java\nscript:alert(1)",
        "https://example.test/\x00.jpg",
        "https://example.test/a.jpg\t",
        "https://example.test/\x7f.jpg",
        "https://example.test/ a.jpg",
        123,
    ],
)
def test_rc15_c23_unsafe_urls(url):
    assert is_safe_http_url(url) is False


def test_rc15_c24_javascript_image_url_renders_no_image_and_appears_nowhere():
    d = build_digest([section(deals=[make_deal(image_url="javascript:alert(1)")])], RUN_DATE)
    soup = soup_of(d)
    cell = data_rows(soup.find("table"))[0].find_all("td")[0]
    assert cell.get_text(strip=True) == "No image"
    assert soup.find("img") is None
    assert "javascript:" not in d.html
    assert "javascript:" not in d.text


def test_rc15_c24_javascript_listing_url_renders_plain_title_and_no_url_line():
    d = build_digest([section(deals=[make_deal(url="javascript:alert(1)")])], RUN_DATE)
    soup = soup_of(d)
    row = data_rows(soup.find("table"))[0]
    assert row.find("a") is None
    assert row.find_all("td")[1].get_text(" ", strip=True) == "Rick Owens Ramones High Top 42"
    assert "javascript:" not in d.html
    assert "javascript:" not in d.text
    assert d.text == (
        "RainCheck: 1 new deal - 2026-10-26\n"
        "\n"
        "rick owens ramones (market price $800.00)\n"
        "\n"
        "1. Rick Owens Ramones High Top 42\n"
        "   ¥50,000 ($310.00) - 61.3% below market\n"
    )


@pytest.mark.parametrize(
    "bad", ["data:text/html,<script>1</script>", "ftp://example.test/x", " https://example.test/x", "java\nscript:alert(1)"]
)
def test_rc15_c24_other_unsafe_listing_urls_are_dropped_from_html_and_text(bad):
    d = build_digest([section(deals=[make_deal(url=bad, image_url=bad)])], RUN_DATE)
    soup = soup_of(d)
    assert soup.find("a") is None
    assert soup.find("img") is None
    assert bad not in d.html and bad not in d.text
    assert "script:" not in d.text and "ftp:" not in d.text and "data:" not in d.text


def test_rc15_c25_adversarial_digest_has_no_dangerous_elements_or_event_attributes():
    deals = [
        make_deal(url=URL_1, title=EVIL_TITLE, image_url=EVIL_IMAGE),
        make_deal(url="javascript:alert(1)", title='<img src=x onerror=alert(1)>', image_url="javascript:alert(2)"),
        make_deal(url="data:text/html,x", title='"><iframe src=//evil></iframe>', image_url="data:image/png;base64,AAAA"),
        make_deal(url="ftp://example.test/a", title="</a><form action=//evil>", image_url="ftp://example.test/a.jpg"),
        make_deal(url=URL_4, title="x' onmouseover='alert(1)", image_url=" https://example.test/a.jpg"),
        make_deal(url=URL_4 + "b", title="<style>body{display:none}</style>", image_url=None),
    ]
    d = build_digest([section(name=EVIL_NAME + "<link rel=x>", deals=deals)], RUN_DATE)
    soup = soup_of(d)
    for tag in BANNED_TAGS:
        assert soup.find(tag) is None, tag
    for el in soup.find_all(True):
        assert not [a for a in el.attrs if a.lower().startswith("on")], el
        assert "class" not in el.attrs
    assert soup.find("b") is None
    # only the images whose URLs are safe were rendered
    assert [i["src"] for i in soup.find_all("img")] == [EVIL_IMAGE]


# ---- criteria 26-28: text part, not-shown line, title normalization -----------------


def test_rc15_c26_not_shown_sentence_plural_singular_and_absent():
    two = build_digest([section(not_shown=2)], RUN_DATE)
    one = build_digest([section(not_shown=1)], RUN_DATE)
    zero = build_digest([section(not_shown=0)], RUN_DATE)
    assert "and 2 more new deals not shown" in two.text
    assert "and 1 more new deal not shown" in one.text
    assert "more new deal" not in zero.text and "more new deal" not in zero.html
    # in HTML it follows the table, as a paragraph
    for d, sentence in ((two, "and 2 more new deals not shown"), (one, "and 1 more new deal not shown")):
        soup = soup_of(d)
        paragraph = [p for p in soup.find_all("p") if p.get_text() == sentence]
        assert len(paragraph) == 1
        assert soup.find("table") in paragraph[0].find_all_previous("table")
    # in text it follows the last block, as its own line, then the text still ends with one newline
    assert two.text.endswith("   " + URL_1 + "\n\nand 2 more new deals not shown\n")
    assert not two.text.endswith("\n\n")


def test_rc15_c26_not_shown_belongs_to_its_own_section():
    sections = [
        section(not_shown=2),
        section(name="pod shorts", market=300.0, deals=[make_deal(item_id=2, url=URL_4 + "z", title="Pod")]),
    ]
    d = build_digest(sections, RUN_DATE)
    assert d.text.count("not shown") == 1
    assert d.text.index("not shown") < d.text.index("pod shorts")


EXPECTED_TEXT = (
    "RainCheck: 2 new deals - 2026-10-26\n"
    "\n"
    "rick owens ramones (market price $800.00)\n"
    "\n"
    "1. Rick Owens Ramones High Top 42\n"
    "   ¥50,000 ($310.00) - 61.3% below market\n"
    "   https://auctions.yahoo.co.jp/jp/auction/t1000000001\n"
    "\n"
    "2. Rick Owens Ramon Sneakers\n"
    "   ¥100,000 ($620.00) - 22.5% below market\n"
    "   https://auctions.yahoo.co.jp/jp/auction/t1000000004\n"
)


def test_rc15_c27_text_body_is_exactly_the_documented_format():
    d = build_digest([section(deals=[deal_1(), deal_4()])], RUN_DATE)
    assert d.text == EXPECTED_TEXT


def test_rc15_c27_text_numbering_restarts_per_section_and_is_not_html_escaped():
    sections = [
        section(deals=[deal_1(), deal_4()]),
        section(name="a & b", market=300.0, deals=[make_deal(item_id=2, url=URL_4 + "z", title="<Tom> & \"Jerry\"")]),
    ]
    d = build_digest(sections, RUN_DATE)
    assert "a & b (market price $300.00)" in d.text
    assert '1. <Tom> & "Jerry"' in d.text
    assert "&amp;" not in d.text and "&lt;" not in d.text
    assert d.text.count("\n1. ") == 2
    assert d.text.endswith("\n") and not d.text.endswith("\n\n")


def test_rc15_c28_whitespace_in_titles_is_collapsed_in_html_alt_and_text():
    d = build_digest([section(deals=[make_deal(title="A\nB\t  C")])], RUN_DATE)
    soup = soup_of(d)
    row = data_rows(soup.find("table"))[0]
    assert row.find_all("td")[1].find("a").get_text() == "A B C"
    assert row.find("img")["alt"] == "A B C"
    assert "1. A B C\n" in d.text


# ---- criteria 29-30 ----------------------------------------------------------------


def test_rc15_c29_large_digest_stays_under_gmail_clipping_size_and_is_600px_wide():
    # Deliberately pathological input: 10 sections (the real catalog has 2) and titles made
    # entirely of "<&>", which grow about 4x when HTML-escaped and appear twice per row (alt and
    # link text). With the Amendment 2 layout this is about 101,960 bytes, while a realistic digest
    # (2 items x 5 deals, 65-character Japanese titles) is about 13 KB. A failure here after a small
    # markup change means the budget is tight for this extreme input, not that real emails are clipped.
    sections = []
    for s in range(10):
        deals = []
        for n in range(5):
            head = f"https://auc-pctr.c.yimg.jp/i/img{s}{n}.jpg?pri=l&w=300&h=300&nf_st=200&p="
            image = head + "a" * (330 - len(head))
            assert len(image) == 330 and "&" in image
            deals.append(
                make_deal(
                    item_id=s, url=f"https://auctions.yahoo.co.jp/jp/auction/t{s}{n:03d}",
                    title=("<&>" * 40)[:120], image_url=image,
                )
            )
        sections.append(section(name=f"item {s}", deals=deals))
    d = build_digest(sections, RUN_DATE)
    assert len(d.html.encode("utf-8")) < 102_000
    assert "max-width:600px" in d.html


def test_rc15_c30_build_digest_prints_nothing(capsys):
    build_digest([section(not_shown=1)], RUN_DATE)
    build_digest([], RUN_DATE)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_rc15_c30_digest_module_is_pure_in_its_imports():
    """Parses src/digest.py: it must import none of the I/O or job modules."""
    import ast
    from pathlib import Path

    source = Path(digest_module.__file__).read_text(encoding="utf-8")
    forbidden = {"db", "requests", "smtplib", "sqlite3", "sendemail", "yahoo_scrape"}
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported & forbidden == set()

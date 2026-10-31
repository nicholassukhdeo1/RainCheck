"""RC-20 countdown tests, using fixed times and offline job fixtures."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

import digest
import yahoo_scrape


RUN_DATE = date(2026, 10, 31)
CREATED_AT = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
END = CREATED_AT + timedelta(days=2, hours=3, minutes=4, seconds=59)
URL = "https://auctions.yahoo.co.jp/jp/auction/rc20-test"
IMAGE = "https://images.example.test/auction.jpg"
COUNTDOWN = "Time remaining at email creation:"


def _deal(kind="auction_watch", end=END, **changes):
    values = dict(
        item_id=1,
        brand_id=1,
        url=URL,
        title="Rick Owens Ramones High Top",
        price_yen=1000,
        price_usd=6.2,
        market_price_usd=800.0,
        discount_pct=99.225,
        image_url=IMAGE,
        kind=kind,
        scheduled_end_at=end,
        current_price_yen=500,
        potential_discount_pct=99.6,
    )
    values.update(changes)
    return digest.Deal(**values)


def _render(deals, *, created_at=CREATED_AT, preview=False):
    section = digest.ItemDeals("rick owens ramones", 800.0, tuple(deals))
    return digest.build_digest(
        [section], RUN_DATE, created_at=created_at, preview=preview,
    )


def _soup(email):
    return BeautifulSoup(email.html, "html.parser")


def _listing_text(email, row=0):
    return _soup(email).find("table").find_all("tr")[row + 1].find_all("td")[1].get_text(" ", strip=True)


def test_rc20_c1_two_days_three_hours_four_minutes_shows_in_both_bodies_beside_jst_end():
    email = _render([_deal()])
    expected = "Time remaining at email creation: 2d 3h 4m"
    listing_text = _listing_text(email)
    assert expected in listing_text
    assert "Scheduled end 2026-11-03 00:04 JST (may change)" in listing_text
    assert expected in email.text
    assert "Scheduled end 2026-11-03 00:04 JST (may change)" in email.text


@pytest.mark.parametrize(
    "seconds, expected",
    [(59, "less than 1m"), (60, "0d 0h 1m")],
)
def test_rc20_c2_subminute_and_exact_minute_countdowns(seconds, expected):
    email = _render([_deal(end=CREATED_AT + timedelta(seconds=seconds))])
    assert f"{COUNTDOWN} {expected}" in email.html
    assert f"{COUNTDOWN} {expected}" in email.text


@pytest.mark.parametrize(
    "created, end, expected",
    [
        (
            datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc),
            datetime(2026, 1, 2, 23, 29, tzinfo=timezone.utc),
            "0d 23h 59m",
        ),
        (
            datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc),
            datetime(2026, 1, 2, 23, 30, tzinfo=timezone.utc),
            "1d 0h 0m",
        ),
        (
            datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc),
            datetime(2026, 1, 3, 0, 30, tzinfo=timezone.utc),
            "1d 1h 0m",
        ),
        (
            datetime(2026, 1, 2, 8, 0, tzinfo=timezone(timedelta(hours=9))),
            datetime(2026, 1, 4, 2, 4, tzinfo=timezone.utc),
            "2d 3h 4m",
        ),
    ],
)
def test_rc20_c3_countdown_uses_elapsed_instants_across_offsets_and_calendar_days(
    created, end, expected
):
    email = _render([_deal(end=end)], created_at=created)
    assert f"{COUNTDOWN} {expected}" in email.html
    assert f"{COUNTDOWN} {expected}" in email.text


def test_rc20_c4_buy_now_and_watch_have_countdowns_but_fixed_price_does_not():
    deals = [
        _deal("auction_buy_now", url=URL + "-buy"),
        _deal("fixed_price", url=URL + "-fixed", scheduled_end_at=END),
        _deal("auction_watch", url=URL + "-watch"),
    ]
    email = _render(deals)
    rows = _soup(email).find("table").find_all("tr")[1:]
    assert len(rows) == 3
    assert COUNTDOWN in rows[0].find_all("td")[1].get_text(" ", strip=True)
    assert COUNTDOWN not in rows[1].find_all("td")[1].get_text(" ", strip=True)
    assert COUNTDOWN in rows[2].find_all("td")[1].get_text(" ", strip=True)
    assert email.text.count(f"{COUNTDOWN} 2d 3h 4m") == 2


def test_rc20_c5_same_end_in_one_digest_uses_identical_countdown_for_every_row():
    email = _render([_deal(), _deal(url=URL + "-second")])
    listing_cells = [
        row.find_all("td")[1].get_text(" ", strip=True)
        for row in _soup(email).find("table").find_all("tr")[1:]
    ]
    assert all(f"{COUNTDOWN} 2d 3h 4m" in cell for cell in listing_cells)
    assert email.text.count(f"{COUNTDOWN} 2d 3h 4m") == 2


@pytest.mark.parametrize("delta", [timedelta(0), -timedelta(minutes=1)])
def test_rc20_c6_elapsed_or_equal_end_warns_without_claiming_auction_closed_or_sold(delta):
    end = CREATED_AT + delta
    email = _render([_deal(end=end)])
    warning = "Scheduled end reached or passed at email creation; check listing"
    assert warning in email.html
    assert warning in email.text
    assert "closed" not in email.html.lower() and "sold" not in email.html.lower()
    assert "closed" not in email.text.lower() and "sold" not in email.text.lower()
    expected_end = end.astimezone(timezone(timedelta(hours=9))).strftime(
        "Scheduled end %Y-%m-%d %H:%M JST (may change)"
    )
    assert expected_end in email.html
    assert expected_end in email.text


@pytest.mark.parametrize(
    "end",
    [
        None,
        datetime(2026, 11, 1, 12, 0),
        "not-a-datetime",
        datetime.min.replace(tzinfo=timezone(timedelta(hours=14))),
    ],
)
def test_rc20_c7_missing_naive_malformed_or_unrepresentable_end_is_unavailable(end):
    email = _render([_deal(end=end)])
    assert len(email.deals) == 1
    for body in (email.html, email.text):
        assert "Time remaining unavailable" in body
        assert "Scheduled end unavailable" in body


def test_rc20_c8_auction_requires_aware_created_at_but_fixed_only_does_not():
    auction_section = digest.ItemDeals("Ramones", 800.0, (_deal(),))
    with pytest.raises(ValueError):
        digest.build_digest([auction_section], RUN_DATE)
    with pytest.raises(ValueError):
        digest.build_digest(
            [auction_section], RUN_DATE,
            created_at=datetime(2026, 10, 31, 12, 0),
        )

    fixed = digest.build_digest(
        [digest.ItemDeals("Ramones", 800.0, (_deal("fixed_price"),))], RUN_DATE
    )
    assert fixed is not None
    assert COUNTDOWN not in fixed.html and COUNTDOWN not in fixed.text


@pytest.mark.parametrize("mode", ["daily", "preview", "send_preview"])
def test_rc20_c9_job_passes_one_captured_utc_instant_and_its_local_date_to_digest(
    mode, fake_http, seeded_db, monkeypatch, capsys
):
    observed_at = datetime(2026, 10, 31, 6, 59, 59, tzinfo=timezone.utc)
    local_at_boundary = datetime(
        2026, 10, 30, 23, 59, 59, tzinfo=timezone(timedelta(hours=-7))
    )
    now_calls = []

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = observed_at if tz is not None else observed_at.replace(tzinfo=None)
            value = cls(
                base.year, base.month, base.day, base.hour, base.minute,
                base.second, base.microsecond, tzinfo=base.tzinfo,
            )
            now_calls.append((tz, value))
            return value

        def astimezone(self, tz=None):
            if tz is None:
                return local_at_boundary
            return super().astimezone(tz)

    monkeypatch.setattr(yahoo_scrape, "datetime", FrozenDateTime)
    listing = yahoo_scrape.YahooListing(
        "rc20job", URL, "Rick Owens Ramones", 1000, IMAGE, False, None,
        END,
    )
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda _html: [listing])
    fake_http.add("GET", yahoo_scrape.RICK_OWENS_URL, text="unused")
    fake_http.add(
        "GET", yahoo_scrape.FRANKFURTER_RATE_URL,
        json={"date": "2026-10-31", "base": "JPY", "quote": "USD", "rate": 0.0062},
    )
    monkeypatch.setattr(
        yahoo_scrape, "prepare_listing_image",
        lambda _url, _image: SimpleNamespace(
            cid="rc20-job-photo", content_type="image/jpeg", data=b"photo"
        ),
    )

    original_builder = yahoo_scrape.build_digest
    calls = []

    def capture_builder(sections, run_date, **kwargs):
        calls.append((run_date, dict(kwargs), len(now_calls)))
        return original_builder(sections, run_date, **kwargs)

    monkeypatch.setattr(yahoo_scrape, "build_digest", capture_builder)
    if mode == "daily":
        assert yahoo_scrape.main() == 0
    elif mode == "preview":
        assert yahoo_scrape.main(preview=True) == 0
    else:
        monkeypatch.setenv("RAINCHECK_DRY_RUN", "0")
        monkeypatch.setattr(yahoo_scrape, "send_email", lambda *args, **kwargs: True)
        assert yahoo_scrape.main(preview=True, send_preview=True) == 0

    assert len(calls) == 1
    run_date, kwargs, call_count = calls[0]
    assert kwargs["created_at"] == observed_at
    assert kwargs["created_at"].utcoffset() == timedelta(0)
    assert run_date == local_at_boundary.date()
    assert call_count > 0
    assert now_calls[call_count - 1][1] == kwargs["created_at"]


def test_rc20_c10_send_and_record_order_and_preview_modes_remain_unchanged(
    fake_http, seeded_db, db_conn, monkeypatch, sent_emails, capsys
):
    observed_at = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = observed_at if tz is not None else observed_at.replace(tzinfo=None)
            return cls(
                value.year, value.month, value.day, value.hour, value.minute,
                value.second, value.microsecond, tzinfo=value.tzinfo,
            )

    monkeypatch.setattr(yahoo_scrape, "datetime", FixedDateTime)
    listing = yahoo_scrape.YahooListing(
        "rc20c10", URL, "Rick Owens Ramones", 1000, IMAGE, False, None, END,
    )
    monkeypatch.setattr(yahoo_scrape, "parse_listings", lambda _html: [listing])
    fake_http.add("GET", yahoo_scrape.RICK_OWENS_URL, text="unused")
    fake_http.add(
        "GET", yahoo_scrape.FRANKFURTER_RATE_URL,
        json={"date": "2026-10-31", "base": "JPY", "quote": "USD", "rate": 0.0062},
    )
    monkeypatch.setattr(
        yahoo_scrape, "prepare_listing_image",
        lambda _url, _image: SimpleNamespace(
            cid="rc20-c10-photo", content_type="image/jpeg", data=b"photo"
        ),
    )
    send_calls = []

    def fail_send_once(subject, html, text, **kwargs):
        send_calls.append((subject, html, text))
        return False

    monkeypatch.setattr(yahoo_scrape, "send_email", fail_send_once)
    with pytest.raises(SystemExit):
        yahoo_scrape.main()
    assert len(send_calls) == 1
    assert db_conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 0

    def check_send_before_recording(subject, html, text, **kwargs):
        assert db_conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 0
        send_calls.append((subject, html, text))
        return True

    monkeypatch.setattr(yahoo_scrape, "send_email", check_send_before_recording)
    yahoo_scrape.main()
    assert len(send_calls) == 2
    assert db_conn.execute("SELECT COUNT(*) FROM listing_matches").fetchone()[0] == 1

    def snapshot_database():
        return (
            tuple(tuple(row) for row in db_conn.execute("SELECT * FROM listings ORDER BY id")),
            tuple(tuple(row) for row in db_conn.execute(
                "SELECT * FROM listing_matches ORDER BY listing_id, item_id"
            )),
        )

    snapshot = snapshot_database()
    monkeypatch.setattr(yahoo_scrape, "send_email", lambda *args, **kwargs: pytest.fail("printed preview sent email"))
    assert yahoo_scrape.main(preview=True) == 0
    assert "Time remaining at email creation:" in capsys.readouterr().out
    assert snapshot_database() == snapshot

    preview_sends = []
    monkeypatch.setenv("RAINCHECK_DRY_RUN", "0")
    monkeypatch.setattr(
        yahoo_scrape, "send_email",
        lambda subject, html, text, **kwargs: preview_sends.append((subject, html, text)) or True,
    )
    assert yahoo_scrape.main(preview=True, send_preview=True) == 0
    assert len(preview_sends) == 1
    assert "Time remaining at email creation:" in preview_sends[0][1]
    assert snapshot_database() == snapshot


def test_rc20_c11_html_keeps_three_columns_escapes_long_title_and_fixed_table_layout():
    title = "Rick Owens Ramones " + ("long title & detail " * 15) + "<script>bad()</script>"
    email = _render([_deal(title=title)])
    soup = _soup(email)
    table = soup.find("table")
    assert [th.get_text() for th in table.find("tr").find_all("th")] == [
        "Image", "Listing", "Price",
    ]
    assert "table-layout:fixed" in table["style"]
    assert "overflow-wrap:anywhere" in soup.find("div")["style"]
    assert COUNTDOWN in _listing_text(email)
    cells = soup.find("table").find_all("tr")[1].find_all("td")
    assert cells[0].find("img") is not None
    assert "¥1,000" in cells[2].get_text(" ", strip=True)
    assert soup.find("script") is None
    assert "&lt;script&gt;bad()&lt;/script&gt;" in email.html

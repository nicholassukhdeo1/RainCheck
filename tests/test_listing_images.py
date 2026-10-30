"""RC-19 listing-specific Yahoo image resolution, with all HTTP calls stubbed."""
import struct
import zlib

import pytest
import requests

from listing_images import prepare_listing_image


LISTING_URL = "https://auctions.yahoo.co.jp/jp/auction/t1234567890"
IMAGE_URL = (
    "https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/"
    "image/dr000/auc0210/users/45a6ce28d3cf2f865f425e68b2890ed9a088ceac/"
    "i-img1200x1200-1792990662638j63eeo.jpg"
)


def _png(width=1, height=1, color=(30, 80, 120)):
    """Build a deterministic RGB PNG without Pillow or external fixture files."""
    def chunk(kind, payload):
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    scanline = b"\x00" + bytes(color) * width
    pixels = scanline * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


class ImageResponse:
    def __init__(self, body=b"", *, status=200, content_type="image/png", chunks=None):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.body = body
        self.chunks = chunks if chunks is not None else [body]
        self.closed = False

    def iter_content(self, chunk_size):
        yield from self.chunks

    def close(self):
        self.closed = True


class ImageSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        return value


def test_rc19_c4_rejects_unsafe_schemes_hosts_credentials_ports_and_whitespace_without_requests():
    unsafe_urls = [
        "http://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/dr000/auc0210/users/45a6ce28d3cf2f865f425e68b2890ed9a088ceac/i-img100x100-1792990662638j63eeo-photo.jpg",
        "https://evil.example/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/dr000/auc0210/users/45a6ce28d3cf2f865f425e68b2890ed9a088ceac/i-img100x100-1792990662638j63eeo-photo.jpg",
        "https://user@auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/dr000/auc0210/users/45a6ce28d3cf2f865f425e68b2890ed9a088ceac/i-img100x100-1792990662638j63eeo-photo.jpg",
        "https://auc-pctr.c.yimg.jp:444/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/dr000/auc0210/users/45a6ce28d3cf2f865f425e68b2890ed9a088ceac/i-img100x100-1792990662638j63eeo-photo.jpg",
        "https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/dr000/auc0210/users/45a6ce28d3cf2f865f425e68b2890ed9a088ceac/i-img100x100-1792990662638j63eeo-photo.jpg\n.evil",
    ]
    for url in unsafe_urls:
        session = ImageSession({})
        assert prepare_listing_image(LISTING_URL, url, session=session) is None
        assert session.calls == []


def test_rc19_c4_rejects_stock_path_but_accepts_fixture_observed_listing_photo_path():
    stock_url = "https://auc-pctr.c.yimg.jp/i/stock/photo.jpg"
    session = ImageSession({})
    assert prepare_listing_image(LISTING_URL, stock_url, session=session) is None
    assert session.calls == []

    # This path structure is present in the saved Yahoo results-page capture.
    session = ImageSession({IMAGE_URL: ImageResponse(_png())})
    assert prepare_listing_image(LISTING_URL, IMAGE_URL, session=session) is not None
    assert [call[0] for call in session.calls] == [IMAGE_URL]


def test_rc19_c4_redirect_is_rejected_without_contacting_redirect_destination():
    response = ImageResponse(status=302, content_type="text/html")
    session = ImageSession({IMAGE_URL: response})
    assert prepare_listing_image(LISTING_URL, IMAGE_URL, session=session) is None
    assert len(session.calls) == 1
    assert session.calls[0][1].get("allow_redirects") is False
    assert response.closed


def test_rc19_c5_rejects_http_errors_unsupported_mime_corrupt_data_and_closes_response():
    cases = [
        ImageResponse(status=404, body=b"not found", content_type="text/plain"),
        ImageResponse(status=500, body=b"error", content_type="image/png"),
        ImageResponse(body=_png(), content_type="image/svg+xml"),
        ImageResponse(body=b"<html>not an image</html>", content_type="image/png"),
    ]
    for response in cases:
        session = ImageSession({IMAGE_URL: response})
        assert prepare_listing_image(LISTING_URL, IMAGE_URL, session=session) is None
        assert response.closed


def test_rc19_c5_rejects_download_over_one_mib_without_retaining_response():
    response = ImageResponse(chunks=[b"x" * (1024 * 1024 + 1)])
    session = ImageSession({IMAGE_URL: response})
    assert prepare_listing_image(LISTING_URL, IMAGE_URL, session=session) is None
    assert response.closed


def test_rc19_c5_transport_timeout_defers_photo_without_retrying_destination():
    session = ImageSession({IMAGE_URL: requests.Timeout("timed out")})
    assert prepare_listing_image(LISTING_URL, IMAGE_URL, session=session) is None
    assert len(session.calls) == 1
    assert session.calls[0][1].get("timeout") is not None


def test_rc19_c6_valid_png_becomes_bounded_raster_thumbnail_with_opaque_cid():
    response = ImageResponse(_png(5, 3))
    session = ImageSession({IMAGE_URL: response})
    image = prepare_listing_image(LISTING_URL, IMAGE_URL, session=session)
    assert image is not None
    assert image.content_type in {"image/jpeg", "image/png"}
    assert isinstance(image.data, bytes) and image.data
    assert len(image.data) <= 80 * 1024
    assert image.cid and LISTING_URL not in image.cid and "@example.com" not in image.cid
    assert response.closed


def test_rc19_c6_portrait_photo_keeps_its_ratio_in_encoded_image_or_html():
    from datetime import date
    from io import BytesIO

    from PIL import Image
    from bs4 import BeautifulSoup

    from digest import Deal, ItemDeals, build_digest

    photo = prepare_listing_image(
        LISTING_URL, IMAGE_URL,
        session=ImageSession({IMAGE_URL: ImageResponse(_png(2, 4))}),
    )
    assert photo is not None
    with Image.open(BytesIO(photo.data)) as prepared:
        prepared_width, prepared_height = prepared.size

    deal = Deal(
        1, 1, LISTING_URL, "Rick Owens Ramones portrait", 1000, 6.2,
        100.0, 93.8, IMAGE_URL,
    )
    digest = build_digest(
        [ItemDeals("rick owens ramones", 100.0, (deal,))], date(2026, 10, 27),
        inline_images={LISTING_URL: photo},
    )
    image = BeautifulSoup(digest.html, "html.parser").find("img")
    assert image["src"] == f"cid:{photo.cid}"
    if prepared_width == prepared_height == 100:
        # A square encoded canvas is the approved padding strategy.
        assert image.get("width") == image.get("height") == "100"
    else:
        # Otherwise the markup must use natural dimensions or preserve height.
        style = image.get("style", "").replace(" ", "").lower()
        width = int(image.get("width", prepared_width))
        height = int(image.get("height", prepared_height))
        assert width * prepared_height == height * prepared_width or "height:auto" in style


@pytest.mark.parametrize("bad_url", [
    "https://auc-pctr.c.yimg.jp/i/na_170x170.png",
    "https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp/image/dr000/auc0210/users/45a6ce28d3cf2f865f425e68b2890ed9a088ceac/i-img100x100-1792990662638j63eeo-na_170x170.png?nf=1",
])
def test_rc19_c6_known_yahoo_fallback_art_is_rejected(bad_url):
    session = ImageSession({})
    assert prepare_listing_image(LISTING_URL, bad_url, session=session) is None
    assert session.calls == []


def test_rc19_c7_missing_result_image_uses_identity_matched_detail_photo():
    page = (
        '<link rel="canonical" href="' + LISTING_URL + '">'
        '<meta property="og:image" content="' + IMAGE_URL + '">'
    )
    detail = ImageResponse(page.encode(), content_type="text/html; charset=utf-8")
    photo = ImageResponse(_png())
    session = ImageSession({LISTING_URL: detail, IMAGE_URL: photo})
    image = prepare_listing_image(LISTING_URL, None, session=session)
    assert image is not None
    assert [call[0] for call in session.calls] == [LISTING_URL, IMAGE_URL]
    assert detail.closed and photo.closed


def test_rc19_c7_detail_photo_for_different_listing_is_rejected():
    other = "https://auctions.yahoo.co.jp/jp/auction/t0987654321"
    page = (
        '<link rel="canonical" href="' + other + '">'
        '<meta property="og:image" content="' + IMAGE_URL + '">'
    )
    detail = ImageResponse(page.encode(), content_type="text/html; charset=utf-8")
    session = ImageSession({LISTING_URL: detail})
    assert prepare_listing_image(LISTING_URL, None, session=session) is None
    assert [call[0] for call in session.calls] == [LISTING_URL]
    assert detail.closed


def test_rc19_c7_detail_redirect_and_oversized_page_are_rejected():
    redirect = ImageResponse(status=301, content_type="text/html")
    session = ImageSession({LISTING_URL: redirect})
    assert prepare_listing_image(LISTING_URL, None, session=session) is None
    assert redirect.closed
    assert session.calls[0][1].get("allow_redirects") is False

    huge_page = ImageResponse(chunks=[b"x" * (2 * 1024 * 1024 + 1)], content_type="text/html")
    session = ImageSession({LISTING_URL: huge_page})
    assert prepare_listing_image(LISTING_URL, None, session=session) is None
    assert huge_page.closed


@pytest.mark.parametrize("malformed", [
    '<link rel><meta property="og:image" content="' + IMAGE_URL + '">',
    '<link rel="canonical" href="' + LISTING_URL + '"><meta property>',
])
def test_rc19_c7_malformed_none_link_or_meta_attributes_defer_without_crashing(malformed):
    detail = ImageResponse(malformed.encode(), content_type="text/html; charset=utf-8")
    session = ImageSession({LISTING_URL: detail})
    assert prepare_listing_image(LISTING_URL, None, session=session) is None
    assert [call[0] for call in session.calls] == [LISTING_URL]
    assert detail.closed


def test_rc19_c5_blocked_stream_read_obeys_remaining_wall_clock_deadline():
    import listing_images
    from time import perf_counter, sleep

    class SlowResponse(ImageResponse):
        read_timeout = None

        def iter_content(self, chunk_size):
            sleep(self.read_timeout)
            raise requests.Timeout("simulated blocked streaming read")

    response = SlowResponse(chunks=[])
    class Session(ImageSession):
        def get(self, url, **kwargs):
            response.read_timeout = kwargs["timeout"][1]
            return super().get(url, **kwargs)

    session = Session({IMAGE_URL: response})
    with listing_images.image_budget() as budget:
        budget.deadline = listing_images.time.monotonic() + 0.15
        started = perf_counter()
        assert prepare_listing_image(LISTING_URL, IMAGE_URL, session=session) is None
        elapsed = perf_counter() - started
    assert elapsed < 0.3
    assert response.read_timeout <= 0.15
    assert response.closed
    # A timed-out stream is terminal, so no detail-page request follows.
    assert [call[0] for call in session.calls] == [IMAGE_URL]


def test_rc19_c20_shared_image_phase_stops_after_aggregate_deadline(monkeypatch):
    import listing_images

    current = [100.0]
    monkeypatch.setattr(listing_images.time, "monotonic", lambda: current[0])

    class AlmostSlowResponse(ImageResponse):
        def iter_content(self, chunk_size):
            current[0] += 7.5
            yield _png()

    response = AlmostSlowResponse(chunks=[])
    session = ImageSession({IMAGE_URL: response})
    second_image_url = IMAGE_URL.replace(
        "1792990662638j63eeo.jpg", "1792990782154lzf1gj.jpg"
    )
    with listing_images.image_budget():
        first = prepare_listing_image(LISTING_URL, IMAGE_URL, session=session)
        assert first is not None
        current[0] += 293.0
        second = prepare_listing_image(
            "https://auctions.yahoo.co.jp/jp/auction/t1234567891", second_image_url,
            session=session,
        )
    assert second is None
    assert len(session.calls) == 1
    assert response.closed

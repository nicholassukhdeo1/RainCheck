"""Download only Yahoo photos tied to a listing, keeping bounded thumbnails in memory."""
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import BytesIO
from contextvars import ContextVar
import logging
import re
import signal
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4
import warnings

from PIL import Image, ImageOps
import requests

logger = logging.getLogger(__name__)
IMAGE_HOST = "auc-pctr.c.yimg.jp"
IMAGE_BYTES = 1024 * 1024
DETAIL_BYTES = 2 * 1024 * 1024
PART_BYTES = 80 * 1024
# Both auction and flea-market photos in the captured Yahoo results use this path.
# A generic file elsewhere on the CDN is not evidence of a listing photo.
LISTING_PHOTO_PATH = re.compile(
    r"/i/auctions\.c\.yimg\.jp/images\.auctions\.yahoo\.co\.jp/image/"
    r"dr[0-9]{3}/auc[0-9]{4}/(?:user/[a-f0-9]{64}|users/[a-f0-9]{40})/"
    r"i-img[0-9]+x[0-9]+-[a-z0-9]+\.(?:jpg|jpeg|png|webp|gif)\Z"
)
MIME_FORMATS = {
    "image/jpeg": "JPEG", "image/png": "PNG",
    "image/webp": "WEBP", "image/gif": "GIF",
}
_current_budget = ContextVar("image_budget", default=None)


@dataclass(frozen=True)
class PreparedImage:
    cid: str
    content_type: str
    data: bytes


@dataclass
class ImageBudget:
    remaining: int = 60
    deadline: float = 0.0
    cache: dict = field(default_factory=dict)

    def usable(self):
        return self.remaining > 0 and time.monotonic() < self.deadline


@contextmanager
def image_budget():
    """Share one request and time allowance across every candidate in a run."""
    budget = ImageBudget(deadline=time.monotonic() + 300)
    token = _current_budget.set(budget)
    try:
        yield budget
    finally:
        _current_budget.reset(token)


def _clean_url(value, host, path_prefix):
    if not isinstance(value, str) or not value or any(
        c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value
    ):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname != host or parsed.port is not None
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.path.startswith("//")
                or not parsed.path.startswith(path_prefix)):
            return None
    except ValueError:
        return None
    return parsed


def _image_url(value):
    parsed = _clean_url(value, IMAGE_HOST, "/i/")
    if parsed is None:
        return None
    lower_path = parsed.path.lower()
    # Yahoo's nf_* query keys can replace a failed listing image with generic art.
    if ("na_170x170" in lower_path or "noimage" in lower_path
            or "ogp" in lower_path or "social" in lower_path
            or LISTING_PHOTO_PATH.fullmatch(lower_path) is None):
        return None
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                       if not k.lower().startswith("nf")])
    return urlunsplit(("https", IMAGE_HOST, parsed.path, query, ""))


def _listing_url(value):
    for host, prefix in (("auctions.yahoo.co.jp", "/jp/auction/"),
                         ("paypayfleamarket.yahoo.co.jp", "/item/")):
        parsed = _clean_url(value, host, prefix)
        if parsed and not parsed.query:
            listing_id = parsed.path[len(prefix):]
            if listing_id and listing_id.isascii() and listing_id.isalnum():
                return urlunsplit(("https", host, parsed.path, "", ""))
    return None


class _DeadlineExceeded(BaseException):
    """Raised by SIGALRM so Requests cannot retry an expired download."""


class _DeadlineUnavailable(Exception):
    """A wall-clock alarm cannot safely be installed in this execution context."""


@contextmanager
def _wall_clock_timeout(seconds):
    """Interrupt a slow stream even when its socket keeps receiving small chunks."""
    if seconds <= 0:
        raise _DeadlineExceeded()
    # Unix signals run on the main Python thread. Refuse the request if another
    # timer is active rather than replacing a caller's process-wide alarm.
    if (threading.current_thread() is not threading.main_thread()
            or not hasattr(signal, "setitimer") or not hasattr(signal, "SIGALRM")):
        raise _DeadlineUnavailable()
    previous_delay, previous_interval = signal.getitimer(signal.ITIMER_REAL)
    if previous_delay or previous_interval:
        raise _DeadlineUnavailable()
    previous_handler = signal.getsignal(signal.SIGALRM)

    def expired(_signal_number, _frame):
        raise _DeadlineExceeded()

    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _download(session, url, limit, allowed_types, budget):
    if not budget.usable():
        return None, None, "budget exhausted"
    budget.remaining -= 1
    response = None
    started = time.monotonic()
    remaining = budget.deadline - started
    try:
        # Requests' socket timeout is an idle limit; the signal is the total limit.
        with _wall_clock_timeout(min(8, remaining)):
            response = session.get(url, stream=True, allow_redirects=False, verify=True,
                                   timeout=(3, min(5, max(0.1, remaining))))
            if response.status_code != 200:
                return None, None, "redirect" if 300 <= response.status_code < 400 else "HTTP status"
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if mime not in allowed_types:
                return None, None, "content type"
            try:
                if int(response.headers.get("Content-Length", "0")) > limit:
                    return None, None, "oversized response"
            except ValueError:
                return None, None, "content length"
            data = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if time.monotonic() - started >= 8 or time.monotonic() >= budget.deadline:
                    return None, None, "deadline"
                data.extend(chunk)
                if len(data) > limit:
                    return None, None, "oversized response"
            if time.monotonic() - started >= 8 or time.monotonic() >= budget.deadline:
                return None, None, "deadline"
            return bytes(data), mime, None
    except (_DeadlineExceeded, _DeadlineUnavailable):
        return None, None, "deadline"
    except (requests.RequestException, OSError, KeyError):
        return None, None, "download failure"
    finally:
        if response is not None:
            response.close()


class _PhotoMeta(HTMLParser):
    def __init__(self):
        super().__init__()
        self.identity = None
        self.photo = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        rel = attrs.get("rel")
        if tag == "link" and isinstance(rel, str) and "canonical" in rel.lower():
            self.identity = attrs.get("href")
        elif tag == "meta":
            property_value = attrs.get("property")
            prop = property_value.lower() if isinstance(property_value, str) else ""
            if prop == "og:url" and self.identity is None:
                self.identity = attrs.get("content")
            elif prop == "og:image" and self.photo is None:
                self.photo = attrs.get("content")


def _detail_photo(listing_url, session, budget):
    data, _, reason = _download(session, listing_url, DETAIL_BYTES, {"text/html"}, budget)
    if data is None:
        return None, reason
    parser = _PhotoMeta()
    try:
        with _wall_clock_timeout(budget.deadline - time.monotonic()):
            parser.feed(data.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeError, _DeadlineExceeded, _DeadlineUnavailable, AttributeError):
        return None, "invalid detail page"
    if _listing_url(parser.identity) != listing_url:
        return None, "detail identity mismatch"
    return _image_url(parser.photo), "missing detail photo"


def _normalize(data, mime, budget=None):
    if not data:
        return None
    try:
        timer = (_wall_clock_timeout(budget.deadline - time.monotonic())
                 if budget is not None else nullcontext())
        with timer, warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as opened:
                if opened.format != MIME_FORMATS[mime] or opened.width < 1 or opened.height < 1:
                    return None
                if opened.width * opened.height > 4_000_000:
                    return None
                opened.seek(0)
                opened.load()
                with ImageOps.exif_transpose(opened) as oriented:
                    with oriented.convert("RGBA") as picture:
                        picture.thumbnail((100, 100), Image.Resampling.LANCZOS)
                        # The markup uses a square box, so letterbox instead of stretching.
                        with Image.new("RGBA", (100, 100), "white") as canvas:
                            offset = ((100 - picture.width) // 2, (100 - picture.height) // 2)
                            canvas.alpha_composite(picture, offset)
                            with canvas.convert("RGB") as opaque:
                                output = BytesIO()
                                opaque.save(output, format="JPEG", quality=85, optimize=True)
                                encoded = output.getvalue()
                                if len(encoded) <= PART_BYTES:
                                    return PreparedImage(uuid4().hex, "image/jpeg", encoded)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning, EOFError, _DeadlineExceeded, _DeadlineUnavailable):
        return None
    return None


def _prepared_from_url(candidate, session, budget):
    # Keep only bounded normalized bytes in the run cache, never original downloads.
    if candidate in budget.cache:
        cached, reason = budget.cache[candidate]
        if cached is None:
            return None, reason
        return PreparedImage(uuid4().hex, cached.content_type, cached.data), None
    data, mime, reason = _download(session, candidate, IMAGE_BYTES, MIME_FORMATS, budget)
    if data is None:
        budget.cache[candidate] = (None, reason)
        return None, reason
    photo = _normalize(data, mime, budget)
    reason = None if photo else "invalid raster"
    budget.cache[candidate] = (photo, reason)
    return photo, reason


def prepare_listing_image(listing_url, image_url, *, session=None):
    """Return a normalized photo or None; never use a photo from another listing."""
    listing = _listing_url(listing_url)
    if listing is None:
        return None
    session = session or requests
    budget = _current_budget.get() or ImageBudget(deadline=time.monotonic() + 300)
    candidate = _image_url(image_url)
    if image_url is not None and candidate is None:
        return None
    if candidate:
        photo, reason = _prepared_from_url(candidate, session, budget)
        if photo:
            return photo
        if reason in ("redirect", "download failure", "deadline"):
            logger.info("Listing %s image rejected: %s", listing.rsplit("/", 1)[-1], reason)
            return None
    # A verified detail identity is required before trying its og:image.
    candidate, reason = _detail_photo(listing, session, budget)
    if candidate:
        photo, reason = _prepared_from_url(candidate, session, budget)
        if photo:
            return photo
    logger.info("Listing %s image unavailable: %s", listing.rsplit("/", 1)[-1], reason)
    return None

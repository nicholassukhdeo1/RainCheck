"""Small bounded retry policy for transient HTTP response statuses."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import time

import requests


logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_SECONDS = 5.0
MAX_TOTAL_WAIT_SECONDS = 10.0


def _retry_after_seconds(response, now):
    """Return a usable Retry-After delay, or None when the header is invalid."""
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("Retry-After")
    if not isinstance(value, str):
        return None
    try:
        if not value.isdecimal():
            raise ValueError
        delay = float(int(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            current = now()
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            delay = (retry_at - current).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if delay < 0:
        return None
    return delay


def request_with_retry(method, url, *, timeout, sleep=time.sleep, now=None, **kwargs):
    """Retry 429 and 5xx responses at most twice, without retrying transport errors."""
    now = now or (lambda: datetime.now(timezone.utc))
    request = getattr(requests, method.lower())
    total_wait = 0.0
    host = requests.utils.urlparse(url).hostname or "HTTP service"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = request(url, timeout=timeout, **kwargs)
        status = getattr(response, "status_code", 200)
        if status != 429 and not 500 <= status <= 599:
            return response
        if attempt == MAX_ATTEMPTS:
            return response

        retry_after = _retry_after_seconds(response, now)
        if retry_after is not None and retry_after > MAX_RETRY_AFTER_SECONDS:
            return response
        backoff = 0.5 * (2 ** (attempt - 1))
        delay = max(backoff, retry_after or 0.0)
        if total_wait + delay > MAX_TOTAL_WAIT_SECONDS:
            return response
        logger.warning("Retrying %s request after HTTP %d (attempt %d)", host, status, attempt)
        sleep(delay)
        total_wait += delay
    return response

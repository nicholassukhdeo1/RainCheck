"""RC-10 bounded HTTP retry behavior, exercised with offline responses."""
from datetime import datetime, timezone

import pytest
import requests

from conftest import FakeResponse


@pytest.fixture
def retry_module():
    import importlib

    return importlib.import_module("http_retry")


def _retry(retry_module, monkeypatch, responses, *, now=None):
    waits = []
    calls = []
    responses = list(responses)

    def request(url, **kwargs):
        calls.append(("GET", url, kwargs))
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(requests, "get", request)
    result = retry_module.request_with_retry(
        "GET", "https://service.example/path", timeout=10,
        sleep=waits.append, now=now or (lambda: datetime(2026, 10, 27, tzinfo=timezone.utc)),
    )
    return result, calls, waits


def test_rc10_c1_successful_first_response_has_one_attempt_and_no_wait(retry_module, monkeypatch):
    response = FakeResponse(json_data={"ok": True})
    result, calls, waits = _retry(retry_module, monkeypatch, [response])
    assert result is response
    assert len(calls) == 1
    assert calls[0][2]["timeout"] == 10
    assert waits == []


def test_rc10_c2_two_503_responses_use_bounded_backoff_then_return_success(retry_module, monkeypatch):
    final = FakeResponse(json_data={"ok": True})
    result, calls, waits = _retry(
        retry_module, monkeypatch,
        [FakeResponse(status=503), FakeResponse(status=503), final],
    )
    assert result is final
    assert len(calls) == 3
    assert waits == [0.5, 1.0]


def test_rc10_c3_all_rate_limit_and_server_statuses_are_retryable(retry_module, monkeypatch):
    for status in [429, *range(500, 600)]:
        _, calls, waits = _retry(
            retry_module, monkeypatch, [FakeResponse(status=status), FakeResponse(status=200)]
        )
        assert len(calls) == 2, f"HTTP {status} should retry once"
        assert waits == [0.5], f"HTTP {status} should use normal first backoff"


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_rc10_c3_client_statuses_are_returned_without_retry(retry_module, monkeypatch, status):
    response, calls, waits = _retry(retry_module, monkeypatch, [FakeResponse(status=status)])
    assert len(calls) == 1
    assert waits == []
    with pytest.raises(requests.HTTPError):
        response.raise_for_status()


def test_rc10_c4_retry_after_seconds_uses_larger_delay(retry_module, monkeypatch):
    _, calls, waits = _retry(
        retry_module, monkeypatch,
        [FakeResponse(status=429, headers={"Retry-After": "2"}), FakeResponse()],
    )
    assert len(calls) == 2
    assert waits == [2.0]


def test_rc10_c4_retry_after_http_date_uses_frozen_clock(retry_module, monkeypatch):
    _, calls, waits = _retry(
        retry_module, monkeypatch,
        [FakeResponse(status=503, headers={"Retry-After": "Tue, 27 Oct 2026 00:00:03 GMT"}), FakeResponse()],
        now=lambda: datetime(2026, 10, 27, tzinfo=timezone.utc),
    )
    assert len(calls) == 2
    assert waits == [3.0]


def test_rc10_c5_retry_after_over_cap_returns_response_without_retry(retry_module, monkeypatch):
    response, calls, waits = _retry(
        retry_module, monkeypatch,
        [FakeResponse(status=429, headers={"Retry-After": "6"})],
    )
    assert len(calls) == 1
    assert waits == []
    with pytest.raises(requests.HTTPError):
        response.raise_for_status()


def test_rc10_c5_huge_retry_after_returns_status_without_integer_overflow(retry_module, monkeypatch):
    response, calls, waits = _retry(
        retry_module, monkeypatch,
        [FakeResponse(status=429, headers={"Retry-After": "9" * 400})],
    )
    assert len(calls) == 1
    assert waits == []
    with pytest.raises(requests.HTTPError):
        response.raise_for_status()


def test_rc10_c5_cumulative_planned_wait_never_exceeds_ten_seconds(retry_module, monkeypatch):
    _, calls, waits = _retry(
        retry_module, monkeypatch,
        [
            FakeResponse(status=503, headers={"Retry-After": "5"}),
            FakeResponse(status=503, headers={"Retry-After": "5"}),
            FakeResponse(status=200),
        ],
    )
    assert len(calls) == 3
    assert waits == [5.0, 5.0]
    assert sum(waits) <= 10.0


@pytest.mark.parametrize("header", ["bad", "-1", "Tue, 26 Oct 2026 23:59:59 GMT"])
def test_rc10_c5_invalid_retry_after_uses_normal_backoff(retry_module, monkeypatch, header):
    _, calls, waits = _retry(
        retry_module, monkeypatch,
        [FakeResponse(status=503, headers={"Retry-After": header}), FakeResponse()],
    )
    assert len(calls) == 2
    assert waits == [0.5]


def test_rc10_c6_exhausted_retries_return_final_http_error_without_body_parsing(retry_module, monkeypatch):
    class UnparseableResponse(FakeResponse):
        def json(self):
            raise AssertionError("the retry boundary must not parse response bodies")

    responses = [UnparseableResponse(text="not json", status=503) for _ in range(3)]
    response, calls, waits = _retry(retry_module, monkeypatch, responses)
    assert len(calls) == 3
    assert waits == [0.5, 1.0]
    with pytest.raises(requests.HTTPError):
        response.raise_for_status()


@pytest.mark.parametrize("error", [requests.Timeout("timeout"), requests.ConnectionError("offline")])
def test_rc10_c7_transport_errors_propagate_without_retry(retry_module, monkeypatch, error):
    waits = []
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs))
        raise error

    monkeypatch.setattr(requests, "get", request)
    with pytest.raises(type(error)):
        retry_module.request_with_retry(
            "GET", "https://service.example/path", timeout=30,
            sleep=waits.append,
        )
    assert len(calls) == 1
    assert waits == []

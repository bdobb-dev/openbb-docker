"""The OpenBB REST path: classify HTTP failures, normalise the payload."""

import pytest

from tick_lab.reference.base import ReferenceError
from tick_lab.reference.eodhd_api import EodhdApiAdapter, classify_status


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def adapter():
    return EodhdApiAdapter("https://openbb.example.ts.net", "user", "pass")


@pytest.mark.parametrize(
    "status,kind",
    [(401, "auth"), (403, "entitlement"), (404, "not_covered"), (500, "transport")],
)
def test_status_codes_are_classified(status, kind):
    assert classify_status(status, "").kind == kind


def test_results_are_normalised_to_bar_columns(monkeypatch):
    payload = {"results": [
        {"date": "2023-05-12T13:30:00", "open": 310.55, "high": 310.65,
         "low": 310.0, "close": 310.03, "volume": 446343},
    ]}
    a = adapter()
    monkeypatch.setattr(a, "_get", lambda *args, **kw: FakeResponse(200, payload))
    out = a.fetch("MSFT", "2023-05-12", "2023-05-12", "1m")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert str(out.index.tz) == "UTC"
    assert out.iloc[0].close == 310.03


def test_empty_results_are_classified_as_empty(monkeypatch):
    a = adapter()
    monkeypatch.setattr(a, "_get", lambda *args, **kw: FakeResponse(200, {"results": []}))
    with pytest.raises(ReferenceError) as exc:
        a.fetch("MSFT", "2023-05-12", "2023-05-12", "1m")
    assert exc.value.kind == "empty"


def test_forbidden_becomes_an_entitlement_error(monkeypatch):
    a = adapter()
    monkeypatch.setattr(a, "_get", lambda *args, **kw: FakeResponse(403, text="Forbidden"))
    with pytest.raises(ReferenceError) as exc:
        a.fetch("GOOG", "2023-05-12", "2023-05-12", "1m")
    assert exc.value.kind == "entitlement"
    assert "GOOG" in exc.value.detail


def test_a_transport_level_failure_is_classified_as_transport(monkeypatch):
    """A connection error, timeout, etc. from `requests` itself (not an HTTP
    status the server returned) is still a real, non-steppable problem."""
    import requests

    a = adapter()

    def _raise(*args, **kwargs):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(a, "_get", _raise)
    with pytest.raises(ReferenceError) as exc:
        a.fetch("MSFT", "2023-05-12", "2023-05-12", "1m")
    assert exc.value.kind == "transport"


def test_fetch_propagates_a_programming_error_unclassified(monkeypatch):
    """A coding bug (e.g. a typo'd attribute) is outside `requests`' own
    exception hierarchy and must come straight through, not be reported as
    a generic 'no interval could serve' failure."""
    a = adapter()

    def _raise(*args, **kwargs):
        raise AttributeError("'NoneType' object has no attribute 'status_code'")

    monkeypatch.setattr(a, "_get", _raise)
    with pytest.raises(AttributeError):
        a.fetch("MSFT", "2023-05-12", "2023-05-12", "1m")

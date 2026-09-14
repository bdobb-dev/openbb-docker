"""Mapping yfinance behaviour onto classified ReferenceErrors."""

import pandas as pd
import pytest
from yfinance.exceptions import YFException

from tick_lab.reference.base import ReferenceError, fetch_finest
from tick_lab.reference.yfinance_adapter import YFinanceAdapter, classify, exclusive_end

RETENTION_1M = (
    '$MSFT: possibly delisted; no price data found  (1m 2023-05-12 -> 2023-05-13) '
    '(Yahoo error = "1m data not available for startTime=1683864000 and '
    'endTime=1683950400. The requested range must be within the last 30 days.")'
)
RETENTION_1H = (
    '$MSFT: possibly delisted; no price data found  (1h 2023-05-12 -> 2023-05-13) '
    '(Yahoo error = "1h data not available for startTime=1683864000 and '
    'endTime=1683950400. The requested range must be within the last 730 days.")'
)


def test_classifies_the_1m_retention_message():
    err = classify(RETENTION_1M)
    assert err.kind == "retention"
    assert "last 30 days" in err.detail


def test_classifies_the_1h_retention_message():
    assert classify(RETENTION_1H).kind == "retention"


def test_classifies_an_unknown_message_as_transport():
    """Unrecognized messages default to a NON-steppable kind, not `empty`.

    Stepping down the ladder is a privilege earned only by messages we
    positively recognise (retention), plus the genuinely-empty-frame case
    handled separately in `fetch`.
    """
    err = classify("something else entirely")
    assert err.kind == "transport"
    assert err.kind not in ("retention", "empty")


def test_supported_intervals_cover_the_ladder():
    assert "1m" in YFinanceAdapter().supported_intervals
    assert "1d" in YFinanceAdapter().supported_intervals


def test_normalises_columns_and_index(monkeypatch):
    adapter = YFinanceAdapter()

    raw = pd.DataFrame(
        {"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5], "Volume": [100]},
        index=pd.DatetimeIndex(["2023-05-12 09:30:00"], tz="America/New_York"),
    )
    monkeypatch.setattr(adapter, "_history", lambda *a, **k: raw)

    out = adapter.fetch("MSFT", "2023-05-12", "2023-05-13", "1m")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert str(out.index.tz) == "UTC"


def test_empty_frame_becomes_a_classified_error(monkeypatch):
    adapter = YFinanceAdapter()
    monkeypatch.setattr(adapter, "_history", lambda *a, **k: pd.DataFrame())
    with pytest.raises(ReferenceError) as exc:
        adapter.fetch("MSFT", "2023-05-12", "2023-05-13", "1m")
    assert exc.value.kind == "empty"


def _raising(exc):
    def _history(*args, **kwargs):
        raise exc

    return _history


def test_fetch_classifies_a_raised_retention_message_as_steppable(monkeypatch):
    adapter = YFinanceAdapter()
    monkeypatch.setattr(adapter, "_history", _raising(YFException(RETENTION_1M)))

    with pytest.raises(ReferenceError) as exc:
        adapter.fetch("MSFT", "2023-05-12", "2023-05-13", "1m")

    assert exc.value.kind == "retention"
    assert "last 30 days" in exc.value.detail
    assert exc.value.kind in ("retention", "empty")  # steppable


def test_fetch_classifies_an_unrecognized_yfinance_error_as_non_steppable(monkeypatch):
    adapter = YFinanceAdapter()
    monkeypatch.setattr(
        adapter,
        "_history",
        _raising(YFException("Too Many Requests. Rate limited. Try after a while.")),
    )

    with pytest.raises(ReferenceError) as exc:
        adapter.fetch("MSFT", "2023-05-12", "2023-05-13", "1m")

    assert exc.value.kind not in ("retention", "empty")


def test_fetch_propagates_a_programming_error_unclassified(monkeypatch):
    """A coding bug (e.g. a typo'd attribute) must never masquerade as a
    data-availability problem -- it is outside yfinance's own exception
    hierarchy and must come straight through."""
    adapter = YFinanceAdapter()
    monkeypatch.setattr(
        adapter, "_history", _raising(AttributeError("'NoneType' object has no attribute 'json'"))
    )

    with pytest.raises(AttributeError):
        adapter.fetch("MSFT", "2023-05-12", "2023-05-13", "1m")


def test_fetch_finest_stops_immediately_on_a_non_steppable_adapter_error(monkeypatch):
    """An unrecognized yfinance failure must halt the ladder at the first
    interval tried, not walk it to exhaustion."""
    adapter = YFinanceAdapter()
    calls: list[str] = []

    def _history(symbol, start, end, interval):
        calls.append(interval)
        raise YFException("Too Many Requests. Rate limited. Try after a while.")

    monkeypatch.setattr(adapter, "_history", _history)

    with pytest.raises(ReferenceError) as exc:
        fetch_finest(adapter, "MSFT", "2023-05-12", "2023-05-13")

    assert exc.value.kind not in ("retention", "empty")
    assert calls == ["1m"]


def test_date_shaped_end_is_widened_because_yahoo_end_is_exclusive():
    """`--date X` means that whole session, but Yahoo's `end` is exclusive.

    Passing start==end returns zero rows at EVERY interval, so the ladder walks
    all the way down and reports a source failure for a date with good data.
    """
    assert exclusive_end("2023-05-12") == pd.Timestamp("2023-05-13")


def test_end_with_a_time_of_day_is_passed_through():
    assert exclusive_end("2023-05-12 15:30:00") == "2023-05-12 15:30:00"


def test_none_end_stays_none():
    assert exclusive_end(None) is None


def test_missing_columns_become_a_classified_error(monkeypatch):
    """A shape surprise from Yahoo must not escape as a bare KeyError.

    Both EODHD adapters classify this; yfinance was the odd sibling.
    """
    adapter = YFinanceAdapter()
    monkeypatch.setattr(
        adapter, "_history",
        lambda *a, **k: pd.DataFrame(
            {"Open": [1.0], "Close": [1.5]},
            index=pd.DatetimeIndex(["2023-05-12 09:30:00"], tz="America/New_York"),
        ),
    )
    with pytest.raises(ReferenceError) as exc:
        adapter.fetch("MSFT", "2023-05-12", "2023-05-13", "1m")
    assert exc.value.kind == "transport"
    assert "high" in exc.value.detail


def test_rows_come_back_sorted(monkeypatch):
    adapter = YFinanceAdapter()
    idx = pd.DatetimeIndex(
        ["2023-05-12 09:31:00", "2023-05-12 09:30:00"], tz="America/New_York"
    )
    monkeypatch.setattr(
        adapter, "_history",
        lambda *a, **k: pd.DataFrame(
            {"Open": [2.0, 1.0], "High": [2.0, 1.0], "Low": [2.0, 1.0],
             "Close": [2.0, 1.0], "Volume": [2, 1]},
            index=idx,
        ),
    )
    out = adapter.fetch("MSFT", "2023-05-12", "2023-05-13", "1m")
    assert out.index.is_monotonic_increasing

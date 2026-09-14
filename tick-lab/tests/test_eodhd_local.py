"""In-process OpenBB: the same call, locally -- including the 403 path."""

import pandas as pd
import pytest

from tick_lab.reference.base import ReferenceError
from tick_lab.reference.eodhd_local import EodhdLocalAdapter, classify_exception


def test_forbidden_is_classified_as_entitlement():
    err = classify_exception(RuntimeError("403 Client Error: Forbidden for url: ..."))
    assert err.kind == "entitlement"


def test_unauthorized_is_classified_as_auth():
    assert classify_exception(RuntimeError("401 Unauthorized")).kind == "auth"


def test_empty_message_is_classified_as_empty():
    assert classify_exception(RuntimeError("no data found")).kind == "empty"


def test_unrecognised_message_is_classified_as_transport_not_empty():
    """The contract: an unrecognised failure must land on a NON-steppable
    kind. Defaulting to `empty` would let `fetch_finest` silently step down
    the ladder past a real bug instead of surfacing it."""
    err = classify_exception(RuntimeError("something the classifier has never seen before"))
    assert err.kind == "transport"


def test_missing_openbb_is_reported_actionably(monkeypatch):
    adapter = EodhdLocalAdapter()

    def boom(*_args, **_kwargs):
        raise ImportError("No module named 'openbb'")

    monkeypatch.setattr(adapter, "_historical", boom)
    with pytest.raises(ReferenceError) as exc:
        adapter.fetch("MSFT", "2023-05-12", "2023-05-12", "1m")
    assert exc.value.kind == "transport"
    assert "pip install" in exc.value.detail


def test_frame_is_normalised(monkeypatch):
    adapter = EodhdLocalAdapter()
    frame = pd.DataFrame(
        {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10]},
        index=pd.DatetimeIndex(["2023-05-12 13:30:00"], tz="UTC"),
    )
    monkeypatch.setattr(adapter, "_historical", lambda *a, **k: frame)
    out = adapter.fetch("MSFT", "2023-05-12", "2023-05-12", "1m")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert str(out.index.tz) == "UTC"


def test_naive_index_is_localised_to_utc(monkeypatch):
    adapter = EodhdLocalAdapter()
    frame = pd.DataFrame(
        {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10]},
        index=pd.DatetimeIndex(["2023-05-12 13:30:00"]),
    )
    monkeypatch.setattr(adapter, "_historical", lambda *a, **k: frame)
    out = adapter.fetch("MSFT", "2023-05-12", "2023-05-12", "1m")
    assert str(out.index.tz) == "UTC"


def test_empty_frame_is_classified_as_empty(monkeypatch):
    adapter = EodhdLocalAdapter()
    monkeypatch.setattr(adapter, "_historical", lambda *a, **k: pd.DataFrame())
    with pytest.raises(ReferenceError) as exc:
        adapter.fetch("MSFT", "2023-05-12", "2023-05-12", "1m")
    assert exc.value.kind == "empty"


def test_fetch_propagates_a_programming_error_unclassified(monkeypatch):
    """A coding bug in our own call (e.g. a typo'd kwarg) is not a
    data-availability problem and must come straight through, not be
    reported as a generic classified failure -- see eodhd_api's and
    yfinance's adapters for the same rule."""
    adapter = EodhdLocalAdapter()

    def boom(*_args, **_kwargs):
        raise TypeError("historical() got an unexpected keyword argument 'symbl'")

    monkeypatch.setattr(adapter, "_historical", boom)
    with pytest.raises(TypeError):
        adapter.fetch("MSFT", "2023-05-12", "2023-05-12", "1m")

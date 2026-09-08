# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The fetcher: default window, cache metadata on the response, provider wiring."""

from datetime import date

import pytest
from openbb_core.provider.abstract.annotated_result import AnnotatedResult
from openbb_core.provider.standard_models.crypto_historical import (
    CryptoHistoricalData,
    CryptoHistoricalQueryParams,
)
from openbb_core.provider.standard_models.currency_historical import (
    CurrencyHistoricalData,
    CurrencyHistoricalQueryParams,
)
from openbb_core.provider.standard_models.equity_historical import (
    EquityHistoricalData,
    EquityHistoricalQueryParams,
)
from openbb_core.provider.standard_models.etf_historical import (
    EtfHistoricalData,
    EtfHistoricalQueryParams,
)
from openbb_core.provider.standard_models.index_historical import (
    IndexHistoricalData,
    IndexHistoricalQueryParams,
)

from openbb_kdb.models import historical
from openbb_kdb.models.historical import (
    KdbCryptoHistoricalFetcher,
    KdbCurrencyHistoricalFetcher,
    KdbEquityHistoricalFetcher,
    KdbEtfHistoricalFetcher,
    KdbIndexHistoricalFetcher,
    _default_window,
)

FETCHERS = [
    (KdbEquityHistoricalFetcher, EquityHistoricalQueryParams, EquityHistoricalData,
     "EquityHistorical"),
    (KdbEtfHistoricalFetcher, EtfHistoricalQueryParams, EtfHistoricalData, "EtfHistorical"),
    (KdbCryptoHistoricalFetcher, CryptoHistoricalQueryParams, CryptoHistoricalData,
     "CryptoHistorical"),
    (KdbCurrencyHistoricalFetcher, CurrencyHistoricalQueryParams, CurrencyHistoricalData,
     "CurrencyHistorical"),
    (KdbIndexHistoricalFetcher, IndexHistoricalQueryParams, IndexHistoricalData,
     "IndexHistorical"),
]


def test_defaults_to_a_one_year_window():
    """The chart opens on 1 year; the default must match it."""
    q = KdbEquityHistoricalFetcher.transform_query({"symbol": "AAPL"})
    assert (q.end_date - q.start_date).days >= 364
    assert q.end_date <= date.today()


def test_explicit_dates_are_preserved():
    q = KdbEquityHistoricalFetcher.transform_query(
        {"symbol": "AAPL", "start_date": date(2022, 1, 1), "end_date": date(2024, 1, 1)}
    )
    assert (q.start_date, q.end_date) == (date(2022, 1, 1), date(2024, 1, 1))


def test_transform_data_attaches_cache_metadata():
    """The HUD reads this straight off extra['results_metadata']."""
    q = KdbEquityHistoricalFetcher.transform_query({"symbol": "AAPL"})
    rows = [{"date": date(2024, 1, 2), "open": 1.0, "high": 2.0,
             "low": 0.5, "close": 1.5, "volume": 100}]
    meta = {"cache": "hit", "rows_from_cache": 1, "rows_from_upstream": 0,
            "gaps_fetched": 0, "upstream_ms": 0.0, "kdb_ms": 1.2}
    out = KdbEquityHistoricalFetcher.transform_data(q, {"rows": rows, "meta": meta})
    assert isinstance(out, AnnotatedResult)
    assert out.metadata["cache"] == "hit"
    assert len(out.result) == 1


def test_provider_registers_all_five_models():
    from openbb_kdb import kdb_provider

    assert set(kdb_provider.fetcher_dict) == {
        "EquityHistorical", "EtfHistorical", "CryptoHistorical",
        "CurrencyHistorical", "IndexHistorical",
    }


# -- _default_window ---------------------------------------------------------


def test_default_window_only_end_date_given_derives_start_a_year_before_it():
    """Regression: deriving both bounds from 'today' inverted an end-only request."""
    out = _default_window({"symbol": "AAPL", "end_date": date(2015, 6, 1)})
    assert out["end_date"] == date(2015, 6, 1)
    assert out["start_date"] == date(2014, 6, 1)
    assert out["start_date"] < out["end_date"]


def test_default_window_only_start_date_given_derives_end_as_today():
    out = _default_window({"symbol": "AAPL", "start_date": date(2015, 6, 1)})
    assert out["start_date"] == date(2015, 6, 1)
    assert out["end_date"] == date.today()


def test_default_window_neither_given_stays_one_year_back_from_today():
    """Unchanged behaviour: this is the demo chart's opening window."""
    out = _default_window({"symbol": "AAPL"})
    assert out["end_date"] == date.today()
    assert (out["end_date"] - out["start_date"]).days >= 364


def test_default_window_both_given_inverted_raises_naming_both_values():
    with pytest.raises(Exception) as exc_info:
        _default_window(
            {"symbol": "AAPL", "start_date": date(2024, 1, 1), "end_date": date(2020, 1, 1)}
        )
    message = str(exc_info.value)
    assert "2024-01-01" in message
    assert "2020-01-01" in message


def test_default_window_both_given_in_order_is_untouched():
    out = _default_window(
        {"symbol": "AAPL", "start_date": date(2022, 1, 1), "end_date": date(2024, 1, 1)}
    )
    assert (out["start_date"], out["end_date"]) == (date(2022, 1, 1), date(2024, 1, 1))


# -- all five fetchers, parametrized -----------------------------------------


@pytest.mark.parametrize("fetcher_cls,query_cls,data_cls,model_name", FETCHERS)
def test_transform_query_binds_the_right_query_model(fetcher_cls, query_cls, data_cls,
                                                       model_name):
    q = fetcher_cls.transform_query({"symbol": "AAPL"})
    assert isinstance(q, query_cls)


@pytest.mark.parametrize("fetcher_cls,query_cls,data_cls,model_name", FETCHERS)
def test_transform_data_binds_the_right_data_model_and_carries_metadata(
    fetcher_cls, query_cls, data_cls, model_name
):
    q = fetcher_cls.transform_query({"symbol": "AAPL"})
    rows = [{"date": date(2024, 1, 2), "open": 1.0, "high": 2.0,
             "low": 0.5, "close": 1.5, "volume": 100}]
    meta = {"cache": "hit", "rows_from_cache": 1, "rows_from_upstream": 0,
            "gaps_fetched": 0, "upstream_ms": 0.0, "kdb_ms": 1.2}
    out = fetcher_cls.transform_data(q, {"rows": rows, "meta": meta})
    assert isinstance(out, AnnotatedResult)
    assert out.metadata == meta
    assert len(out.result) == 1
    assert isinstance(out.result[0], data_cls)


# -- aextract_data / shared session+cache wiring -----------------------------


class _FakeCache:
    """Records the arguments `_extract` passes to `cache.get`."""

    def __init__(self, rows, meta):
        self._rows = rows
        self._meta = meta
        self.calls: list[dict] = []

    async def get(self, **kwargs):
        self.calls.append(kwargs)
        return self._rows, self._meta


@pytest.mark.parametrize("fetcher_cls,query_cls,data_cls,model_name", FETCHERS)
async def test_aextract_data_passes_model_name_and_returns_rows_and_meta_shape(
    monkeypatch, fetcher_cls, query_cls, data_cls, model_name
):
    rows = [{"date": date(2024, 1, 2), "close": 1.5}]
    meta = {"cache": "partial", "rows_from_cache": 1, "rows_from_upstream": 1,
            "gaps_fetched": 1, "upstream_ms": 5.0, "kdb_ms": 0.5}
    fake_cache = _FakeCache(rows, meta)
    monkeypatch.setattr(historical, "_cache", lambda credentials: fake_cache)

    q = fetcher_cls.transform_query({"symbol": "AAPL"})
    result = await fetcher_cls.aextract_data(q, credentials={"kdb_upstream": "eodhd"})

    assert result == {"rows": rows, "meta": meta}
    assert len(fake_cache.calls) == 1
    assert fake_cache.calls[0]["model"] == model_name
    assert fake_cache.calls[0]["symbol"] == "AAPL"

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Static map of provider credentials: display names, public demo values,
and the cheapest authenticated probe request per provider.

Probes build an httpx.Request from the FULL parsed credentials dict so
multi-var providers (Alpaca key+secret) work. A provider with probe=None is
reported as test 'skipped'. invalid_markers are substrings of a 2xx body that
mean the key was rejected anyway (providers that never 401)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import httpx


@dataclass(frozen=True)
class ProbeSpec:
    build: Callable[[dict[str, str]], httpx.Request | None]
    invalid_markers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Provider:
    name: str
    env_var: str
    demo_values: tuple[str, ...] = ()
    probe: ProbeSpec | None = None


def _get(values: dict[str, str], var: str) -> str | None:
    v = values.get(var, "").strip()
    return v or None


def _eodhd(values: dict[str, str]) -> httpx.Request | None:
    k = _get(values, "EODHD_API_KEY")
    if not k:
        return None
    return httpx.Request(
        "GET",
        "https://eodhd.com/api/eod/AAPL.US",
        params={"api_token": k, "fmt": "json", "period": "d", "order": "d", "limit": 1},
    )


def _fmp(values: dict[str, str]) -> httpx.Request | None:
    k = _get(values, "FMP_API_KEY")
    if not k:
        return None
    return httpx.Request(
        "GET", "https://financialmodelingprep.com/api/v3/profile/AAPL", params={"apikey": k}
    )


def _fred(values: dict[str, str]) -> httpx.Request | None:
    k = _get(values, "FRED_API_KEY")
    if not k:
        return None
    return httpx.Request(
        "GET",
        "https://api.stlouisfed.org/fred/series",
        params={"series_id": "GNPCA", "api_key": k, "file_type": "json"},
    )


def _tiingo(values: dict[str, str]) -> httpx.Request | None:
    k = _get(values, "TIINGO_TOKEN")
    if not k:
        return None
    return httpx.Request("GET", "https://api.tiingo.com/api/test", params={"token": k})


def _bls(values: dict[str, str]) -> httpx.Request | None:
    k = _get(values, "BLS_API_KEY")
    if not k:
        return None
    return httpx.Request(
        "GET",
        "https://api.bls.gov/publicAPI/v2/timeseries/data/CUUR0000SA0",
        params={"registrationkey": k, "latest": "true"},
    )


def _eia(values: dict[str, str]) -> httpx.Request | None:
    k = _get(values, "EIA_API_KEY")
    if not k:
        return None
    return httpx.Request(
        "GET", "https://api.eia.gov/v2/seriesid/STEO.WTIPUUS.M", params={"api_key": k}
    )


def _intrinio(values: dict[str, str]) -> httpx.Request | None:
    k = _get(values, "INTRINIO_API_KEY")
    if not k:
        return None
    return httpx.Request(
        "GET", "https://api-v2.intrinio.com/companies/AAPL", params={"api_key": k}
    )


def _alpaca(values: dict[str, str]) -> httpx.Request | None:
    kid, sec = _get(values, "ALPACA_API_KEY"), _get(values, "ALPACA_API_SECRET")
    if not (kid and sec):
        return None
    return httpx.Request(
        "GET",
        "https://data.alpaca.markets/v2/stocks/AAPL/bars/latest",
        headers={"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec},
    )


def _alpha_vantage(values: dict[str, str]) -> httpx.Request | None:
    k = _get(values, "ALPHA_VANTAGE_API_KEY")
    if not k:
        return None
    return httpx.Request(
        "GET",
        "https://www.alphavantage.co/query",
        params={"function": "GLOBAL_QUOTE", "symbol": "AAPL", "apikey": k},
    )


def _p(name, env_var, demo=(), probe=None):
    return Provider(name=name, env_var=env_var, demo_values=demo, probe=probe)


PROVIDERS: dict[str, Provider] = {
    p.env_var: p
    for p in [
        _p("FMP", "FMP_API_KEY", ("demo",), ProbeSpec(_fmp, ("Invalid API KEY",))),
        _p("FRED", "FRED_API_KEY", (), ProbeSpec(_fred, ("api_key",))),
        _p("Tiingo", "TIINGO_TOKEN", (), ProbeSpec(_tiingo)),
        _p(
            "Alpha Vantage",
            "ALPHA_VANTAGE_API_KEY",
            ("demo",),
            ProbeSpec(_alpha_vantage, ("Invalid API call", "apikey")),
        ),
        _p("Nasdaq Data Link", "NASDAQ_API_KEY"),
        _p("EIA", "EIA_API_KEY", (), ProbeSpec(_eia, ("API_KEY_MISSING", "invalid api_key"))),
        _p("BLS", "BLS_API_KEY", (), ProbeSpec(_bls, ("REQUEST_NOT_PROCESSED",))),
        _p("CFTC", "CFTC_APP_TOKEN"),
        _p("Congress.gov", "CONGRESS_GOV_API_KEY"),
        _p("EconDB", "ECONDB_API_KEY"),
        _p("BizToc", "BIZTOC_API_KEY"),
        _p("Intrinio", "INTRINIO_API_KEY", (), ProbeSpec(_intrinio)),
        _p("Benzinga", "BENZINGA_API_KEY"),
        _p("TradingEconomics", "TRADINGECONOMICS_API_KEY"),
        _p("Tradier", "TRADIER_API_KEY"),
        _p("Alpaca", "ALPACA_API_KEY", (), ProbeSpec(_alpaca)),
        _p("Alpaca (secret)", "ALPACA_API_SECRET"),
        _p("EODHD", "EODHD_API_KEY", ("demo",), ProbeSpec(_eodhd)),
    ]
}

# Non-credential configuration vars that legitimately live in credentials.env.
IGNORE: frozenset[str] = frozenset(
    {
        "TRADIER_ACCOUNT_TYPE",
        "KDB_CONTAINER",
        "KDB_HOST",
        "KDB_PORT",
        "S3_CONTAINER",
        "ARCTICDB_BUCKET",
        "ARCTICDB_LIBRARY",
        "ARCTICDB_URI",
    }
)


def is_demo(env_var: str, value: str) -> bool:
    p = PROVIDERS.get(env_var)
    if p is None or not value:
        return False
    return value.lower() in {d.lower() for d in p.demo_values}

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Upstream resolution: any registered provider, resolved by name."""

import pytest

from openbb_kdb.upstream import UpstreamError, fetch_gap


class FakeFetcher:
    last_call = None

    @classmethod
    async def fetch_data(cls, params, credentials=None, **kwargs):
        FakeFetcher.last_call = (params, credentials)
        return [{"date": "2024-01-02", "close": 1.0}]


class FakeProvider:
    def __init__(self, fetchers, credentials=None):
        self.fetcher_dict = fetchers
        if credentials is not None:
            self.credentials = credentials


def install_registry(monkeypatch, providers):
    import openbb_kdb.upstream as up

    monkeypatch.setattr(up, "_load_registry", lambda: providers)
    up._REGISTRY_CACHE = None


@pytest.mark.asyncio
async def test_fetches_through_the_named_provider(monkeypatch):
    install_registry(monkeypatch, {"eodhd": FakeProvider({"EquityHistorical": FakeFetcher})})
    rows = await fetch_gap("eodhd", "EquityHistorical", {"symbol": "AAPL"}, {"k": "v"})
    assert rows == [{"date": "2024-01-02", "close": 1.0}]
    assert FakeFetcher.last_call == ({"symbol": "AAPL"}, {"k": "v"})


class FakeCredentialsModel:
    """Stands in for openbb_core's process-wide Credentials singleton."""

    def __init__(self, values):
        self._values = values

    def __getattr__(self, name):
        return self._values.get(name)


def install_global_credentials(monkeypatch, values):
    import openbb_core.app.model.credentials as creds_mod
    import openbb_core.provider.query_executor as qe_mod

    monkeypatch.setattr(creds_mod, "Credentials", lambda: FakeCredentialsModel(values))

    def fake_filter(credentials, provider, require_credentials):
        return {
            field: v
            for field in provider.credentials
            if (v := credentials.get(field)) is not None
        }

    monkeypatch.setattr(qe_mod.QueryExecutor, "filter_credentials", staticmethod(fake_filter))


@pytest.mark.asyncio
async def test_upstream_credential_is_resolved_from_the_global_set_when_missing(monkeypatch):
    """The request-scoped credentials fetch_gap receives are kdb's own (empty,
    since kdb declares none) -- the real regression this guards: a cache miss
    must not 502 with "Missing EODHD credential" just because kdb itself has
    no key of its own."""
    install_registry(
        monkeypatch,
        {"eodhd": FakeProvider({"EquityHistorical": FakeFetcher}, credentials=["eodhd_api_key"])},
    )
    install_global_credentials(monkeypatch, {"eodhd_api_key": "real-key"})
    await fetch_gap("eodhd", "EquityHistorical", {"symbol": "AAPL"}, {})
    assert FakeFetcher.last_call == ({"symbol": "AAPL"}, {"eodhd_api_key": "real-key"})


@pytest.mark.asyncio
async def test_an_already_present_upstream_credential_is_not_overridden(monkeypatch):
    install_registry(
        monkeypatch,
        {"eodhd": FakeProvider({"EquityHistorical": FakeFetcher}, credentials=["eodhd_api_key"])},
    )
    install_global_credentials(monkeypatch, {"eodhd_api_key": "global-key"})
    await fetch_gap(
        "eodhd", "EquityHistorical", {"symbol": "AAPL"}, {"eodhd_api_key": "explicit-key"}
    )
    assert FakeFetcher.last_call == ({"symbol": "AAPL"}, {"eodhd_api_key": "explicit-key"})


@pytest.mark.asyncio
async def test_provider_declaring_no_credentials_gets_credentials_passed_through_untouched(
    monkeypatch,
):
    """A provider that never declares credentials (kdb itself, or a test
    double) must not trigger the global-credentials lookup at all."""
    install_registry(monkeypatch, {"noauth": FakeProvider({"EquityHistorical": FakeFetcher})})
    await fetch_gap("noauth", "EquityHistorical", {"symbol": "AAPL"}, {"k": "v"})
    assert FakeFetcher.last_call == ({"symbol": "AAPL"}, {"k": "v"})


@pytest.mark.asyncio
async def test_any_provider_works_not_just_eodhd(monkeypatch):
    """KDB_UPSTREAM must accept any registered provider."""
    install_registry(monkeypatch, {"yfinance": FakeProvider({"EquityHistorical": FakeFetcher})})
    rows = await fetch_gap("yfinance", "EquityHistorical", {"symbol": "AAPL"}, None)
    assert rows


@pytest.mark.asyncio
async def test_unknown_provider_raises_upstream_error(monkeypatch):
    install_registry(monkeypatch, {"eodhd": FakeProvider({})})
    with pytest.raises(UpstreamError, match="nosuch"):
        await fetch_gap("nosuch", "EquityHistorical", {}, None)


@pytest.mark.asyncio
async def test_provider_without_the_model_raises(monkeypatch):
    install_registry(monkeypatch, {"eodhd": FakeProvider({"EquityHistorical": FakeFetcher})})
    with pytest.raises(UpstreamError, match="CryptoHistorical"):
        await fetch_gap("eodhd", "CryptoHistorical", {}, None)


@pytest.mark.asyncio
async def test_kdb_may_not_be_its_own_upstream(monkeypatch):
    """Guards against infinite recursion through the provider registry."""
    install_registry(monkeypatch, {"kdb": FakeProvider({"EquityHistorical": FakeFetcher})})
    with pytest.raises(UpstreamError, match="itself"):
        await fetch_gap("kdb", "EquityHistorical", {}, None)


class PydanticLikeRow:
    """Stands in for an OpenBB Data model: not a dict, but has model_dump()."""

    def __init__(self, payload):
        self._payload = payload

    def model_dump(self):
        return self._payload


class AnnotatedResultLike:
    """Stands in for OpenBB's AnnotatedResult: wraps the list via .result."""

    def __init__(self, result):
        self.result = result


class UnconvertibleRow:
    """Neither a dict nor something with model_dump()."""


@pytest.mark.asyncio
async def test_pydantic_like_rows_are_converted_to_dicts(monkeypatch):
    class ModelFetcher:
        @classmethod
        async def fetch_data(cls, params, credentials=None, **kwargs):
            return [PydanticLikeRow({"date": "2024-01-02", "close": 1.0})]

    install_registry(monkeypatch, {"eodhd": FakeProvider({"EquityHistorical": ModelFetcher})})
    rows = await fetch_gap("eodhd", "EquityHistorical", {"symbol": "AAPL"}, None)
    assert rows == [{"date": "2024-01-02", "close": 1.0}]


@pytest.mark.asyncio
async def test_annotated_result_like_wrapper_is_unwrapped(monkeypatch):
    class AnnotatedFetcher:
        @classmethod
        async def fetch_data(cls, params, credentials=None, **kwargs):
            return AnnotatedResultLike([{"date": "2024-01-02", "close": 1.0}])

    install_registry(monkeypatch, {"eodhd": FakeProvider({"EquityHistorical": AnnotatedFetcher})})
    rows = await fetch_gap("eodhd", "EquityHistorical", {"symbol": "AAPL"}, None)
    assert rows == [{"date": "2024-01-02", "close": 1.0}]


@pytest.mark.asyncio
async def test_none_result_returns_empty_list(monkeypatch):
    class NoneFetcher:
        @classmethod
        async def fetch_data(cls, params, credentials=None, **kwargs):
            return None

    install_registry(monkeypatch, {"eodhd": FakeProvider({"EquityHistorical": NoneFetcher})})
    rows = await fetch_gap("eodhd", "EquityHistorical", {"symbol": "AAPL"}, None)
    assert rows == []


@pytest.mark.asyncio
async def test_unconvertible_row_raises_upstream_error_naming_provider(monkeypatch):
    class BadRowFetcher:
        @classmethod
        async def fetch_data(cls, params, credentials=None, **kwargs):
            return [UnconvertibleRow()]

    install_registry(monkeypatch, {"eodhd": FakeProvider({"EquityHistorical": BadRowFetcher})})
    with pytest.raises(UpstreamError, match="eodhd"):
        await fetch_gap("eodhd", "EquityHistorical", {"symbol": "AAPL"}, None)


@pytest.mark.asyncio
async def test_registry_load_failure_raises_upstream_error_and_does_not_poison_cache(
    monkeypatch,
):
    import openbb_kdb.upstream as up

    def boom():
        raise RuntimeError("extensions could not be loaded")

    monkeypatch.setattr(up, "_load_registry", boom)
    up._REGISTRY_CACHE = None

    with pytest.raises(UpstreamError):
        await fetch_gap("eodhd", "EquityHistorical", {}, None)

    # A later call must be able to retry rather than staying poisoned.
    install_registry(monkeypatch, {"eodhd": FakeProvider({"EquityHistorical": FakeFetcher})})
    rows = await fetch_gap("eodhd", "EquityHistorical", {"symbol": "AAPL"}, {"k": "v"})
    assert rows == [{"date": "2024-01-02", "close": 1.0}]


@pytest.mark.asyncio
async def test_core_internals_drift_falls_back_to_the_environment(monkeypatch):
    """`Credentials` / `QueryExecutor.filter_credentials` are openbb-core
    internals, not documented API. If a core release moves or resignatures
    them, the cache must still serve: fall back to reading the credential from
    the environment rather than propagating an ImportError/TypeError out of a
    code path the caller never asked about."""
    import openbb_core.provider.query_executor as qe_mod

    install_registry(
        monkeypatch,
        {"eodhd": FakeProvider({"EquityHistorical": FakeFetcher}, credentials=["eodhd_api_key"])},
    )

    def exploded(*_args, **_kwargs):
        raise TypeError("filter_credentials() got an unexpected keyword argument")

    monkeypatch.setattr(qe_mod.QueryExecutor, "filter_credentials", staticmethod(exploded))
    monkeypatch.setenv("EODHD_API_KEY", "from-env")

    await fetch_gap("eodhd", "EquityHistorical", {"symbol": "AAPL"}, {})
    assert FakeFetcher.last_call == ({"symbol": "AAPL"}, {"eodhd_api_key": "from-env"})


@pytest.mark.asyncio
async def test_core_internals_drift_without_env_still_reaches_the_upstream(monkeypatch):
    """With the internals broken AND no env credential, the gap fetch must
    still call the upstream fetcher -- which raises its own meaningful
    UnauthorizedError -- rather than dying inside this helper."""
    import openbb_core.provider.query_executor as qe_mod

    install_registry(
        monkeypatch,
        {"eodhd": FakeProvider({"EquityHistorical": FakeFetcher}, credentials=["eodhd_api_key"])},
    )

    def exploded(*_args, **_kwargs):
        raise ImportError("cannot import name 'filter_credentials'")

    monkeypatch.setattr(qe_mod.QueryExecutor, "filter_credentials", staticmethod(exploded))
    monkeypatch.delenv("EODHD_API_KEY", raising=False)

    await fetch_gap("eodhd", "EquityHistorical", {"symbol": "AAPL"}, {})
    assert FakeFetcher.last_call == ({"symbol": "AAPL"}, {"eodhd_api_key": None})

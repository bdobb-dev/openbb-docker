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
    def __init__(self, fetchers):
        self.fetcher_dict = fetchers


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

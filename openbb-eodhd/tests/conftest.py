# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for openbb-eodhd tests."""

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest


# ============================================================
# Mock helpers
# ============================================================

def _make_mock_pykx():
    """Return a mock pykx module for modules that import it."""
    mock_kx = MagicMock()
    mock_kx.q = MagicMock()
    return mock_kx


# ============================================================
# Async helpers
# ============================================================

def run_async(async_fn, *args, **kwargs):
    """Run an async function synchronously via asyncio.run."""
    return asyncio.run(async_fn(*args, **kwargs))


# ============================================================
# Sample EODHD bar data
# ============================================================

@pytest.fixture
def eod_bar_data() -> list[dict]:
    return [
        {"date": "2024-01-02", "open": 150.0, "high": 153.0, "low": 149.0,
         "close": 152.0, "volume": 1000000, "adjusted_close": 151.5},
        {"date": "2024-01-03", "open": 152.0, "high": 155.0, "low": 151.0,
         "close": 154.0, "volume": 1200000, "adjusted_close": 153.5},
    ]


@pytest.fixture
def intraday_bar_data() -> list[dict]:
    return [
        {"timestamp": 1704150000, "open": 150.0, "high": 151.0, "low": 149.5,
         "close": 150.5, "volume": 50000},
        {"timestamp": 1704153600, "open": 150.5, "high": 152.0, "low": 150.0,
         "close": 151.0, "volume": 45000},
    ]


@pytest.fixture
def null_close_bar_data() -> list[dict]:
    return [
        {"date": "2024-01-02", "open": 150.0, "high": 153.0, "low": 149.0,
         "close": 152.0, "volume": 1000000},
        {"date": "2024-01-03", "open": None, "high": None, "low": None,
         "close": None, "volume": 0},
    ]


@pytest.fixture
def multi_symbol_bar_data() -> list[dict]:
    return [
        {"_symbol": "AAPL", "date": "2024-01-02", "open": 150.0,
         "high": 153.0, "low": 149.0, "close": 152.0, "volume": 1000000},
        {"_symbol": "MSFT", "date": "2024-01-02", "open": 300.0,
         "high": 305.0, "low": 299.0, "close": 302.0, "volume": 2000000},
    ]


@pytest.fixture
def eodhd_credentials() -> dict[str, str]:
    return {"eodhd_api_key": "test_key_123"}


# ============================================================
# Mock official-SDK client (eodhd.APIClient)
# ============================================================

@pytest.fixture
def mock_eodhd_client():
    """Return a factory that creates a MagicMock official-SDK client.

    Every SDK method returns `response` (or, when `response` is callable, its
    per-call result). Patch `get_client` at the module under test, e.g.
    `patch("openbb_eodhd.models._bars.get_client", return_value=client)`.
    """

    def _make(response: Any) -> MagicMock:
        client = MagicMock()
        side_effect = response if callable(response) else lambda *a, **k: response
        for method in (
            "get_eod_historical_stock_market_data",
            "get_intraday_historical_data",
            "get_historical_dividends_data",
            "get_historical_splits_data",
            "get_fundamentals_data",
        ):
            getattr(client, method).side_effect = side_effect
        return client

    return _make

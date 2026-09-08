# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for live-grid tests."""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_ws_client_factory():
    """Stand-in for feeds._build_client; records every client it builds."""
    built: list[MagicMock] = []

    def factory(api_key: str, feed: str, symbols: list[str]) -> MagicMock:
        client = MagicMock()
        client.feed = feed
        client.symbols = list(symbols)
        client.data_list = []
        client.running = True
        built.append(client)
        return client

    factory.built = built
    return factory

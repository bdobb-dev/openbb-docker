# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""kdb+ read-through cache provider for the OpenBB Platform."""

from openbb_core.provider.abstract.provider import Provider

from openbb_kdb.models.historical import (
    KdbCryptoHistoricalFetcher,
    KdbCurrencyHistoricalFetcher,
    KdbEquityHistoricalFetcher,
    KdbEtfHistoricalFetcher,
    KdbIndexHistoricalFetcher,
)

__all__ = ["kdb_provider"]

kdb_provider = Provider(
    name="kdb",
    website="https://kx.com",
    description=(
        "In-memory kdb+ read-through cache. Serves cached bars, fetches only the "
        "missing ranges from the upstream provider (KDB_UPSTREAM, default eodhd), "
        "and passes through when kdb+ is unavailable."
    ),
    fetcher_dict={
        "EquityHistorical": KdbEquityHistoricalFetcher,
        "EtfHistorical": KdbEtfHistoricalFetcher,
        "CryptoHistorical": KdbCryptoHistoricalFetcher,
        "CurrencyHistorical": KdbCurrencyHistoricalFetcher,
        "IndexHistorical": KdbIndexHistoricalFetcher,
    },
)

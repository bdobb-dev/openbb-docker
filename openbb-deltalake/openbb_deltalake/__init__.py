# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Delta Lake integration for OpenBB (provider + OBBject accessor + generic store)."""

from openbb_core.app.model.extension import Extension
from openbb_core.provider.abstract.provider import Provider

from openbb_deltalake.accessor import DeltaLakeAccessor
from openbb_deltalake.models.historical import (
    DeltaLakeCryptoHistoricalFetcher,
    DeltaLakeCurrencyHistoricalFetcher,
    DeltaLakeEquityHistoricalFetcher,
    DeltaLakeEtfHistoricalFetcher,
    DeltaLakeIndexHistoricalFetcher,
)
from openbb_deltalake.store import DeltaStore, store

__all__ = ["deltalake_provider", "ext", "DeltaStore", "store"]

deltalake_provider = Provider(
    name="deltalake",
    website="https://delta.io",
    description=(
        "Serve bars stored in a Delta Lake library through the standard OpenBB "
        "interface (equity/etf/crypto/currency/index historical). Pair with the "
        "`.deltalake` OBBject accessor and the `openbb_deltalake.store` API to "
        "persist and read back ANY data offline."
    ),
    # No credentials: connection is configured via DELTA_URI / DELTA_S3_* /
    # DELTA_LIBRARY env vars or per-call query params.
    credentials=None,
    fetcher_dict={
        "EquityHistorical": DeltaLakeEquityHistoricalFetcher,
        "EtfHistorical": DeltaLakeEtfHistoricalFetcher,
        "CryptoHistorical": DeltaLakeCryptoHistoricalFetcher,
        "CurrencyHistorical": DeltaLakeCurrencyHistoricalFetcher,
        "IndexHistorical": DeltaLakeIndexHistoricalFetcher,
    },
    repr_name="Delta Lake",
)

ext = Extension(
    name="deltalake",
    description="Persist OBBject results to a Delta Lake library.",
)
DeltaLake = ext.obbject_accessor(DeltaLakeAccessor)

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Delta Lake OBBject accessor (write path).

Attaches a `.deltalake` namespace to every OBBject result so any query can be
persisted to a Delta table and managed:

    res = obb.equity.price.historical("AAPL", provider="yfinance")
    res.deltalake.write("AAPL")                 # store into the default library
    res.deltalake.append("AAPL")                # append new rows
    res.deltalake.list_symbols()                # catalog
    res.deltalake.read_metadata("AAPL")
    res.deltalake.delete("AAPL")

All methods accept optional `library=` / `uri=` overrides. For reading arbitrary
data back (and for use without an existing result), see `openbb_deltalake.store`.
"""

from typing import Any, Optional


class DeltaLakeAccessor:
    """Persist and manage OBBject results in a Delta Lake library."""

    def __init__(self, obbject):
        """Bind the accessor to its OBBject and build a default store."""
        self._obbject = obbject
        self._default_store = _make_store(None, None)

    def _store(self, uri: Optional[str], library: Optional[str]):
        """Return the default store, or a new one if overrides are given."""
        if uri is None and library is None:
            return self._default_store
        return _make_store(uri, library)

    def write(
        self,
        key: str,
        *,
        library: Optional[str] = None,
        uri: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Write this result to the Delta library as a new version (overwrites the symbol)."""
        return self._store(uri, library).write(key, self._obbject, metadata=metadata)

    def append(
        self, key: str, *, library: Optional[str] = None, uri: Optional[str] = None
    ) -> dict[str, Any]:
        """Append this result to an existing symbol."""
        return self._store(uri, library).append(key, self._obbject)

    def list_symbols(
        self, *, library: Optional[str] = None, uri: Optional[str] = None
    ) -> list[str]:
        """List symbols stored in the library."""
        return self._store(uri, library).list_symbols()

    def read_metadata(
        self, key: str, *, library: Optional[str] = None, uri: Optional[str] = None
    ) -> Any:
        """Read the metadata stored alongside `key`."""
        return self._store(uri, library).read_metadata(key)

    def delete(
        self, key: str, *, library: Optional[str] = None, uri: Optional[str] = None
    ) -> dict[str, Any]:
        """Delete a symbol from the library."""
        return self._store(uri, library).delete(key)


def _make_store(uri: Optional[str], library: Optional[str]):
    """Build a DeltaStore, resolving config on first access."""
    # pylint: disable=import-outside-toplevel
    from openbb_deltalake.store import DeltaStore

    return DeltaStore(uri=uri, library=library)

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Fetching a cache miss from whichever provider the user configured.

Providers are resolved BY NAME through OpenBB's registry rather than imported,
which is what lets KDB_UPSTREAM be any installed provider instead of hardwiring
EODHD.
"""

from typing import Any

_REGISTRY_CACHE: dict[str, Any] | None = None


class UpstreamError(Exception):
    """The configured upstream provider cannot serve this request."""


def _load_registry() -> dict[str, Any]:
    """Map provider name -> Provider object."""
    from openbb_core.provider.registry import RegistryLoader

    return RegistryLoader.from_extensions().providers


def _registry() -> dict[str, Any]:
    global _REGISTRY_CACHE  # noqa: PLW0603
    if _REGISTRY_CACHE is None:
        try:
            registry = _load_registry()
        except Exception as exc:
            # Do NOT assign to _REGISTRY_CACHE here: a failed load must not
            # poison the cache, so the next call can retry.
            raise UpstreamError("Could not load OpenBB's provider registry.") from exc
        _REGISTRY_CACHE = registry
    return _REGISTRY_CACHE


def _with_upstream_credentials(prov: Any, credentials: dict | None) -> dict | None:
    """Fill in the upstream provider's OWN credential(s) (e.g. eodhd_api_key).

    ``credentials`` here is whatever OpenBB's router resolved for the
    ORIGINAL request -- scoped to the "kdb" provider, which declares no
    credentials of its own (it's a cache layer, not a data source). A
    cache-miss gap-fetch calls straight into the upstream provider's own
    fetcher, which needs ITS key, so that key must be resolved separately
    from the same process-wide credential set OpenBB's router itself reads
    from (env vars / user_settings.json) -- not from the kdb-scoped dict this
    function received. A provider that declares no credentials (or a test
    double that doesn't set the attribute) is returned untouched, and an
    already-present value is never overridden.
    """
    declared = getattr(prov, "credentials", None)
    if not declared:
        return credentials
    merged = dict(credentials or {})
    if all(merged.get(field) for field in declared):
        return merged

    # `Credentials` and `QueryExecutor.filter_credentials` are openbb-core
    # INTERNALS -- neither appears in the ODP developer docs, whose public
    # surface is Provider/Fetcher/Data/QueryParams/Extension/OBBject plus
    # provider.utils.{helpers,errors}. They are the only way to reach the
    # process-wide credential set the router itself reads, so this extension
    # uses them deliberately; but a core release is free to move or resignature
    # them without it being a breaking change to anything documented.
    #
    # So treat this as best-effort. If it breaks, fall back to reading the
    # environment directly and carry on: the upstream fetcher validates its own
    # credentials and raises a meaningful UnauthorizedError when they are
    # missing, which is a far better failure than this cache exploding with an
    # ImportError on a code path the caller never asked about.
    try:
        from openbb_core.app.model.credentials import Credentials
        from openbb_core.provider.query_executor import QueryExecutor

        raw = {field: getattr(Credentials(), field, None) for field in declared}
        resolved = QueryExecutor.filter_credentials(raw, prov, require_credentials=False)
    except Exception:  # noqa: BLE001 - any core-internals drift lands here
        resolved = _credentials_from_env(declared)

    for field, value in resolved.items():
        if not merged.get(field):
            merged[field] = value
    return merged


def _credentials_from_env(declared) -> dict:
    """Read declared credential fields straight from the environment.

    The documented fallback for provider keys: openbb-core reads
    ``~/.openbb_platform/.env`` and the process environment, where a
    credential named ``eodhd_api_key`` is spelled ``EODHD_API_KEY``.
    """
    from os import environ

    return {field: environ.get(field.upper()) for field in declared}


def _normalize_rows(provider: str, result: Any) -> list[dict]:
    """Turn a fetcher's result into a plain list of dicts.

    Accepts: None (no data), a list of dicts, a list of pydantic-style models
    (anything with ``model_dump()``), or an ``AnnotatedResult``-like wrapper
    exposing the list via ``.result``.
    """
    if result is None:
        return []
    rows = getattr(result, "result", result)
    if rows is None:
        return []
    normalized = []
    for row in rows:
        if isinstance(row, dict):
            normalized.append(row)
        elif hasattr(row, "model_dump"):
            normalized.append(row.model_dump())
        else:
            raise UpstreamError(
                f"Provider {provider!r} returned a row of type "
                f"{type(row).__name__!r} that this cache cannot consume "
                "(expected a dict or an object with model_dump())."
            )
    return normalized


async def fetch_gap(
    provider: str, model: str, params: dict, credentials: dict | None
) -> list[dict]:
    """Fetch one missing range from the upstream provider."""
    if provider.lower() == "kdb":
        raise UpstreamError(
            "kdb cannot be its own upstream (KDB_UPSTREAM must not point back to itself)."
        )
    registry = _registry()
    prov = registry.get(provider)
    if prov is None:
        raise UpstreamError(
            f"Upstream provider {provider!r} is not installed. Available: {sorted(registry)}"
        )
    fetcher = prov.fetcher_dict.get(model)
    if fetcher is None:
        raise UpstreamError(
            f"Provider {provider!r} does not implement {model}. Implements: "
            f"{sorted(prov.fetcher_dict)}"
        )
    credentials = _with_upstream_credentials(prov, credentials)
    # Deliberately NOT wrapped: OpenBB assigns meaning to specific exceptions
    # raised by fetch_data (e.g. EmptyDataError), and this cache must be
    # transparent -- provider="kdb" should behave exactly like the upstream
    # provider plus caching, so those exceptions propagate unchanged.
    result = await fetcher.fetch_data(params, credentials)
    return _normalize_rows(provider, result)

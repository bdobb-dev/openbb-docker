# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The logical catalog: declared relations, their capabilities, and external Delta libraries."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from openbb_deltalake.utils import fs_and_root
from pyarrow import fs as pafs

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import Context
from security_master_api.store.schemas import RELATIONS

SILVER_MODES = ("current_corrected", "known_at", "effective_on", "delta_snapshot")
BRONZE_MODES = ("captured_by", "delta_snapshot")
OPS_MODES = ("delta_snapshot",)


@dataclass(frozen=True)
class Relation:
    layer: str
    name: str
    description: str
    kind: str
    modes: tuple[str, ...]
    keys: tuple[str, ...]
    depends_on: tuple[str, ...] = ()

    @property
    def full(self) -> str:
        return f"{self.layer}.{self.name}"

    def as_dict(self) -> dict:
        return {"layer": self.layer, "name": self.name, "description": self.description,
                "kind": self.kind, "modes": list(self.modes), "keys": list(self.keys),
                "depends_on": list(self.depends_on)}


_KEYS = {
    "silver.issuers": ("issuer_id",), "silver.instruments": ("instrument_id",),
    "silver.securities": ("security_id",), "silver.listings": ("listing_id",),
    "silver.identifiers": ("identifier_type", "identifier"),
    "silver.fundamental_facts": ("issuer_id", "fact", "period_end"),
    "silver.prices_normalized": ("listing_id", "market_date"),
    "silver.corporate_actions": ("listing_id", "action_type", "ex_date"),
    "silver.universe_membership": ("universe_id", "listing_id"),
    "silver.exchanges": ("exchange_id",), "silver.calendar_authorities": ("authority_id",),
    "silver.calendar_rules": ("rule_id",),
    "silver.calendar_exceptions": ("calendar_id", "session_date", "assertion_domain"),
    "silver.market_sessions": ("calendar_id", "session_date"),
    "silver.session_interruptions": ("calendar_id", "session_date", "interruption_start"),
    "bronze.source_captures": ("capture_id",), "bronze.request_log": ("request_id",),
    "bronze.payload_rows": ("capture_id", "ordinal"), "bronze.ingestion_runs": ("run_id",),
    "ops.jobs": ("job_id", "seq"), "ops.job_events": ("event_id",),
    "ops.receipts": ("execution_id",),
}

GOLD: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
    # name: (keys, depends_on, description)
    "security_master": (("listing_id",), ("silver.listings", "silver.identifiers",
                        "silver.instruments", "silver.issuers", "silver.securities"),
                        "Resolved securities, listings and local coverage."),
    "price_daily": (("listing_id", "market_date"), ("silver.prices_normalized", "silver.listings"),
                    "Correctable daily prices by stable listing."),
    "corporate_actions": (("listing_id", "action_type", "ex_date"),
                          ("silver.corporate_actions", "silver.listings"),
                          "Splits, dividends and reorganizations."),
    "universe_membership": (("universe_id", "listing_id"),
                            ("silver.universe_membership", "silver.listings"),
                            "Point-in-time index and universe tenures."),
    "market_calendar": (("calendar_id", "session_date"),
                        ("silver.market_sessions", "silver.calendar_exceptions",
                         "silver.exchanges", "silver.session_interruptions"),
                        "Exchange sessions joined to holiday and market-session evidence."),
    "odp_equity_info": (("listing_id",), ("silver.listings", "silver.issuers",
                        "silver.fundamental_facts"),
                        "The EquityInfo projection."),
}


def _declared() -> list[Relation]:
    out = []
    for full in RELATIONS:
        layer, name = full.split(".", 1)
        modes = {"silver": SILVER_MODES, "bronze": BRONZE_MODES, "ops": OPS_MODES}[layer]
        out.append(Relation(layer, name, f"{layer} relation {name}", "delta_table", modes,
                            _KEYS[full]))
    for name, (keys, deps, description) in GOLD.items():
        out.append(Relation("gold", name, description, "view", SILVER_MODES, keys, deps))
    return out


def external_relations(settings: Settings) -> list[Relation]:
    if not settings.delta_base:
        return []
    filesystem, root = fs_and_root(settings.delta_base, settings.storage_options)
    out = []
    for library in settings.external_libraries:
        try:
            infos = filesystem.get_file_info(pafs.FileSelector(f"{root}/{library}", recursive=False))
        except (FileNotFoundError, OSError):
            continue
        for info in infos:
            if info.type != pafs.FileType.Directory:
                continue
            symbol = info.base_name
            log = filesystem.get_file_info(f"{info.path}/_delta_log")
            if log.type == pafs.FileType.Directory:
                out.append(Relation("bronze", f"{library}.{symbol}",
                                    f"Delta table {library}/{symbol} (external evidence)",
                                    "external_delta", ("delta_snapshot", "captured_by"), ()))
    return out


def catalog(settings: Settings) -> list[Relation]:
    return _declared() + external_relations(settings)


def catalog_index(settings: Settings) -> dict[str, Relation]:
    """The catalog keyed by full name, built once and passed down.

    `external_relations` lists a remote object store, so building the catalog is the
    expensive part of a lookup. Every function below takes this index so one request pays
    for it once instead of once per relation it happens to mention.
    """
    return {rel.full: rel for rel in catalog(settings)}


def relation(settings: Settings, full: str, index: dict[str, Relation] | None = None) -> Relation:
    rel = (index if index is not None else catalog_index(settings)).get(full)
    if rel is None:
        raise DomainError("QUERY_REJECTED", f"unknown relation: {full}", {"relation": full})
    return rel


def expand(settings: Settings, relations: Iterable[str],
           index: dict[str, Relation] | None = None) -> set[str]:
    index = catalog_index(settings) if index is None else index
    out: set[str] = set()
    for full in relations:
        rel = relation(settings, full, index)
        out.add(full)
        out |= set(rel.depends_on)
    return out


def check_modes(settings: Settings, ctx: Context, relations: Iterable[str],
                index: dict[str, Relation] | None = None) -> None:
    index = catalog_index(settings) if index is None else index
    for full in relations:
        rel = relation(settings, full, index)
        if ctx.mode not in rel.modes:
            raise DomainError(
                "TEMPORAL_MODE_UNSUPPORTED",
                f"{ctx.mode} is not supported by {full}",
                {"relation": full, "supported_modes": list(rel.modes)},
            )

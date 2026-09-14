"""`cli.sync_reference` end to end on a real Delta lake: fixture reference
feeds -> security master -> membership -> fundamentals, then the manifest
generator reads what it wrote. Phase-0 (2026-09-14) found nothing built the
master/membership, so `vault manifest --generate` had no identity to read.
"""
import datetime as dt
from pathlib import Path

from deltalake import DeltaTable

from tick_vault import cli
from tick_vault.capture import CaptureStore, FakeTransport
from tick_vault.engine import ManifestBuilder
from tick_vault.loop import LoopContext
from tick_vault.schemas import create_all

FX = Path(__file__).parent.parent / "fixtures" / "eodhd_ref"
NOW = dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.timezone.utc)


def _read(root, table):
    layer, name = table.split(".", 1)
    return DeltaTable(f"{root}/{layer}/{name}").to_pandas()


def _sync(tmp_path):
    root = str(tmp_path / "lake")
    create_all(root)
    transport = FakeTransport([
        (200, (FX / "exchange_symbols.json").read_bytes()),
        (200, (FX / "delisted_symbols.json").read_bytes()),
        (200, (FX / "symbol_changes.json").read_bytes()),
        (200, (FX / "components_gspc.json").read_bytes()),
        (200, (FX / "fundamentals.json").read_bytes()),  # AAPL.US
        (404, b'{"error": "not found"}'),                # TWTR.US
    ])
    ctx = LoopContext(root=root, transport=transport, store=CaptureStore(root),
                      api_token="KEY", clock=lambda: NOW)
    return root, transport, cli.sync_reference(ctx)


def test_sync_builds_master_membership_and_issue_ids(tmp_path):
    root, transport, summary = _sync(tmp_path)

    assert summary == {"members": 2, "resolved": 2, "ambiguous": 0, "unresolved": 0, "fundamentals": 1}
    assert [u.split("?")[0].rsplit("/", 1)[-1] for u in transport.requested_urls[-2:]] == ["AAPL.US", "TWTR.US"]
    assert set(_read(root, "silver.index_membership_version")["resolution_status"]) == {"RESOLVED"}
    ids = set(_read(root, "silver.identifier_assignment_version")["id_value"])
    assert "037833100" in ids  # AAPL's CUSIP, attached from fundamentals
    assert "BK" not in ids     # BK -> BNY touches no constituent: outside the universe


def test_manifest_reads_synced_membership_end_exclusive(tmp_path):
    root, _, _ = _sync(tmp_path)

    # TWTR: StartDate 2018-06-01, EndDate 2022-10-27 = first NON-member session
    rows = ManifestBuilder(root).generate(dt.date(2022, 10, 24), dt.date(2022, 10, 31))

    per_week = rows.groupby("week_monday")["listing_id"].nunique().to_dict()
    assert per_week == {dt.date(2022, 10, 24): 2, dt.date(2022, 10, 31): 1}
    assert set(rows["status"]) == {"PENDING"}

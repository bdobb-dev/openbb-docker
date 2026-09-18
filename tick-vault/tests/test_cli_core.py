"""Runnable (pyarrow/deltalake/pytest-free) tests for `tick_vault.cli`
(task-10 brief): argparse wiring (each verb dispatches to its handler,
monkeypatched), the calibration-report gate refusal/override on
`backfill --loop`, and `format_status`'s pure formatting."""
import datetime as dt
import os
import shutil
import tempfile

import pandas as pd

from tick_vault import cli


def _tmpdir():
    return tempfile.mkdtemp(prefix="tick_vault_cli_test_")


class _Recorder:
    """A fake handler: records whatever positional args it was called with."""

    def __init__(self, return_value=0):
        self.calls = []
        self.return_value = return_value

    def __call__(self, *args):
        self.calls.append(args)
        return self.return_value


def _patch_handler(name, recorder):
    original = getattr(cli, name)
    setattr(cli, name, recorder)
    return original


def _restore_handler(name, original):
    setattr(cli, name, original)


# ---------------------------------------------------------------------------
# parser wiring: each verb dispatches to its handler
# ---------------------------------------------------------------------------

def test_main_dispatches_backfill_to_handle_backfill():
    recorder = _Recorder()
    original = _patch_handler("handle_backfill", recorder)
    try:
        rc = cli.main(["backfill", "--week", "2026-08-31"], ctx_factory=lambda: "CTX")
        assert rc == 0
        assert len(recorder.calls) == 1
        args, ctx_factory = recorder.calls[0]
        assert args.command == "backfill"
        assert args.week == "2026-08-31"
        assert ctx_factory() == "CTX"
    finally:
        _restore_handler("handle_backfill", original)


def test_main_dispatches_settle_to_handle_settle():
    recorder = _Recorder()
    original = _patch_handler("handle_settle", recorder)
    try:
        cli.main(["settle", "--week", "2026-08-31"])
        assert len(recorder.calls) == 1
        assert recorder.calls[0][0].week == "2026-08-31"
    finally:
        _restore_handler("handle_settle", original)


def test_main_dispatches_manifest_to_handle_manifest():
    recorder = _Recorder()
    original = _patch_handler("handle_manifest", recorder)
    try:
        cli.main(["manifest", "--generate", "2016-01-04", "2026-08-31"])
        assert len(recorder.calls) == 1
        assert recorder.calls[0][0].generate == ["2016-01-04", "2026-08-31"]
    finally:
        _restore_handler("handle_manifest", original)


def test_main_dispatches_reference_to_handle_reference():
    recorder = _Recorder()
    original = _patch_handler("handle_reference", recorder)
    try:
        cli.main(["reference", "--sync"])
        assert len(recorder.calls) == 1
        assert recorder.calls[0][0].sync is True
    finally:
        _restore_handler("handle_reference", original)


def test_main_dispatches_figi_to_handle_figi():
    recorder = _Recorder()
    original = _patch_handler("handle_figi", recorder)
    try:
        cli.main(["figi", "--drain"])
        assert len(recorder.calls) == 1
        assert recorder.calls[0][0].drain is True
    finally:
        _restore_handler("handle_figi", original)


def test_main_dispatches_status_to_handle_status():
    recorder = _Recorder()
    original = _patch_handler("handle_status", recorder)
    try:
        cli.main(["status"])
        assert len(recorder.calls) == 1
        assert recorder.calls[0][0].command == "status"
    finally:
        _restore_handler("handle_status", original)


def test_main_dispatches_calibrate_to_handle_calibrate():
    recorder = _Recorder()
    original = _patch_handler("handle_calibrate", recorder)
    try:
        cli.main(["calibrate", "--week", "2026-08-31", "--legacy-root", "/some/path"])
        assert len(recorder.calls) == 1
        args = recorder.calls[0][0]
        assert args.week == "2026-08-31"
        assert args.legacy_root == "/some/path"
    finally:
        _restore_handler("handle_calibrate", original)


def test_backfill_requires_mutually_exclusive_group():
    try:
        cli.build_parser().parse_args(["backfill"])
        raise AssertionError("expected SystemExit (argparse required group)")
    except SystemExit:
        pass


# ---------------------------------------------------------------------------
# calibration gate refusal / override
# ---------------------------------------------------------------------------

def test_backfill_loop_refuses_without_calibration_report():
    d = _tmpdir()
    try:
        report_path = os.path.join(d, "does-not-exist.md")
        old_env = os.environ.get("CALIBRATION_REPORT")
        os.environ["CALIBRATION_REPORT"] = report_path
        try:
            args = cli.build_parser().parse_args(["backfill", "--loop"])
            try:
                cli.handle_backfill(args, ctx_factory=lambda: "CTX")
                raise AssertionError("expected SystemExit refusal")
            except SystemExit as exc:
                assert report_path in str(exc)
                assert "calibrate" in str(exc)
        finally:
            if old_env is None:
                os.environ.pop("CALIBRATION_REPORT", None)
            else:
                os.environ["CALIBRATION_REPORT"] = old_env
    finally:
        shutil.rmtree(d)


def test_backfill_loop_proceeds_with_calibration_report_present():
    d = _tmpdir()
    try:
        report_path = os.path.join(d, "ep15-calibration.md")
        with open(report_path, "w") as f:
            f.write("# calibration report\n")
        old_env = os.environ.get("CALIBRATION_REPORT")
        os.environ["CALIBRATION_REPORT"] = report_path

        # C2 fix-round: `--loop` also refuses when `ctx.reconcile_step` is
        # None, so this "proceeds" test needs a fake ctx that HAS one
        # wired (a bare sentinel string, as this test used before the
        # fix-round, has no such attribute and would now be refused).
        class _FakeReconcileConfiguredCtx:
            reconcile_step = staticmethod(lambda *a, **k: None)

        fake_ctx = _FakeReconcileConfiguredCtx()

        recorder = _Recorder(return_value=0)
        original = _patch_handler("run_backfill_loop", recorder)
        try:
            args = cli.build_parser().parse_args(["backfill", "--loop"])
            rc = cli.handle_backfill(args, ctx_factory=lambda: fake_ctx)
            assert rc == 0
            assert len(recorder.calls) == 1
            assert recorder.calls[0][0] is fake_ctx
        finally:
            _restore_handler("run_backfill_loop", original)
            if old_env is None:
                os.environ.pop("CALIBRATION_REPORT", None)
            else:
                os.environ["CALIBRATION_REPORT"] = old_env
    finally:
        shutil.rmtree(d)


def test_backfill_loop_override_flag_bypasses_gate_and_logs_note():
    d = _tmpdir()
    try:
        report_path = os.path.join(d, "does-not-exist.md")
        old_env = os.environ.get("CALIBRATION_REPORT")
        os.environ["CALIBRATION_REPORT"] = report_path

        recorder = _Recorder(return_value=0)
        original_loop = _patch_handler("run_backfill_loop", recorder)

        opened = []
        closed = []

        class _FakeCtx:
            pass

        def fake_open_run(ctx, run_type, *, note=None):
            opened.append((run_type, note))
            return "run_fake"

        def fake_close_run(ctx, run_id, *, status=None, run_type=None):
            closed.append((run_id, status))

        original_open = cli.loop_mod.open_run
        original_close = cli.loop_mod.close_run
        cli.loop_mod.open_run = fake_open_run
        cli.loop_mod.close_run = fake_close_run
        try:
            args = cli.build_parser().parse_args(["backfill", "--loop", "--i-know-what-im-doing"])
            rc = cli.handle_backfill(args, ctx_factory=lambda: _FakeCtx())
            assert rc == 0
            assert len(opened) == 1
            assert opened[0][0] == "GATE_OVERRIDE"
            assert "i-know-what-im-doing" in opened[0][1] or "override" in opened[0][1]
            assert len(closed) == 1
            assert len(recorder.calls) == 1
            assert isinstance(recorder.calls[0][0], _FakeCtx)
        finally:
            cli.loop_mod.open_run = original_open
            cli.loop_mod.close_run = original_close
            _restore_handler("run_backfill_loop", original_loop)
            if old_env is None:
                os.environ.pop("CALIBRATION_REPORT", None)
            else:
                os.environ["CALIBRATION_REPORT"] = old_env
    finally:
        shutil.rmtree(d)


# ---------------------------------------------------------------------------
# I7: reference --sync / figi --drain / manifest --generate / calibrate
# each open+close one ops.ingestion_run row
# ---------------------------------------------------------------------------

def _run_type_rows(writer):
    return [(df.iloc[0]["run_type"], df.iloc[0]["status"]) for _table, df in writer.writes]


def test_handle_manifest_opens_and_closes_ingestion_run():
    writer = _FakeIngestionRunWriter()
    now = dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.timezone.utc)
    ctx = cli.loop_mod.LoopContext(root="mem://test", clock=lambda: now, ingestion_run_writer=writer)

    class _FakeBuilder:
        def __init__(self, root, manifest_reader=None):
            self.root = root

        def generate(self, first, last):
            return [{"work_id": "wrk_1"}]

    original = cli.ManifestBuilder
    cli.ManifestBuilder = _FakeBuilder
    try:
        args = cli.build_parser().parse_args(["manifest", "--generate", "2016-01-04", "2026-08-31"])
        rc = cli.handle_manifest(args, ctx_factory=lambda: ctx)
        assert rc == 0
    finally:
        cli.ManifestBuilder = original

    assert [table for table, _df in writer.writes] == ["ops.ingestion_run", "ops.ingestion_run"]
    assert _run_type_rows(writer) == [
        ("manifest_generate", "RUNNING"),
        ("manifest_generate", "COMPLETED"),
    ]


def test_handle_reference_opens_and_closes_ingestion_run():
    writer = _FakeIngestionRunWriter()
    now = dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.timezone.utc)
    ctx = cli.loop_mod.LoopContext(root="mem://test", clock=lambda: now, ingestion_run_writer=writer)

    original = cli.sync_reference
    cli.sync_reference = lambda ctx: {"members": 0}
    try:
        args = cli.build_parser().parse_args(["reference", "--sync"])
        rc = cli.handle_reference(args, ctx_factory=lambda: ctx)
        assert rc == 0
    finally:
        cli.sync_reference = original

    assert _run_type_rows(writer) == [
        ("reference_sync", "RUNNING"),
        ("reference_sync", "COMPLETED"),
    ]


def test_handle_figi_opens_and_closes_ingestion_run():
    writer = _FakeIngestionRunWriter()
    now = dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.timezone.utc)
    ctx = cli.loop_mod.LoopContext(root="mem://test", clock=lambda: now, ingestion_run_writer=writer)

    class _FakeFigiWorker:
        def __init__(self, transport, store, root, api_token):
            pass

        def run_batch(self):
            return {"resolved": 0}

    import tick_vault.figi as figi_mod

    original = figi_mod.FigiWorker
    figi_mod.FigiWorker = _FakeFigiWorker
    try:
        args = cli.build_parser().parse_args(["figi", "--drain"])
        rc = cli.handle_figi(args, ctx_factory=lambda: ctx)
        assert rc == 0
    finally:
        figi_mod.FigiWorker = original

    assert _run_type_rows(writer) == [
        ("figi_drain", "RUNNING"),
        ("figi_drain", "COMPLETED"),
    ]


# ---------------------------------------------------------------------------
# C2: backfill --loop also refuses without ctx.reconcile_step configured
# ---------------------------------------------------------------------------

def test_backfill_loop_refuses_without_reconcile_step():
    d = _tmpdir()
    try:
        report_path = os.path.join(d, "ep15-calibration.md")
        with open(report_path, "w") as f:
            f.write("# calibration report\n")
        old_env = os.environ.get("CALIBRATION_REPORT")
        os.environ["CALIBRATION_REPORT"] = report_path

        class _FakeNoReconcileCtx:
            reconcile_step = None

        try:
            args = cli.build_parser().parse_args(["backfill", "--loop"])
            try:
                cli.handle_backfill(args, ctx_factory=lambda: _FakeNoReconcileCtx())
                raise AssertionError("expected SystemExit refusal")
            except SystemExit as exc:
                assert "reconcil" in str(exc).lower()
                assert "i-know-what-im-doing" in str(exc)
        finally:
            if old_env is None:
                os.environ.pop("CALIBRATION_REPORT", None)
            else:
                os.environ["CALIBRATION_REPORT"] = old_env
    finally:
        shutil.rmtree(d)


def test_backfill_loop_override_logs_reconcile_missing_note():
    d = _tmpdir()
    try:
        report_path = os.path.join(d, "ep15-calibration.md")
        with open(report_path, "w") as f:
            f.write("# calibration report\n")
        old_env = os.environ.get("CALIBRATION_REPORT")
        os.environ["CALIBRATION_REPORT"] = report_path

        recorder = _Recorder(return_value=0)
        original_loop = _patch_handler("run_backfill_loop", recorder)

        opened = []
        closed = []

        class _FakeNoReconcileCtx:
            reconcile_step = None

        def fake_open_run(ctx, run_type, *, note=None):
            opened.append((run_type, note))
            return "run_fake"

        def fake_close_run(ctx, run_id, *, status=None, run_type=None):
            closed.append((run_id, status))

        original_open = cli.loop_mod.open_run
        original_close = cli.loop_mod.close_run
        cli.loop_mod.open_run = fake_open_run
        cli.loop_mod.close_run = fake_close_run
        try:
            args = cli.build_parser().parse_args(["backfill", "--loop", "--i-know-what-im-doing"])
            rc = cli.handle_backfill(args, ctx_factory=lambda: _FakeNoReconcileCtx())
            assert rc == 0
            assert len(opened) == 1
            assert "reconcil" in opened[0][1].lower()
            assert len(closed) == 1
        finally:
            cli.loop_mod.open_run = original_open
            cli.loop_mod.close_run = original_close
            _restore_handler("run_backfill_loop", original_loop)
            if old_env is None:
                os.environ.pop("CALIBRATION_REPORT", None)
            else:
                os.environ["CALIBRATION_REPORT"] = old_env
    finally:
        shutil.rmtree(d)


# ---------------------------------------------------------------------------
# C2: _pandas_reconcile_step - wired ctx with a fake ReferenceClient and a
# fake existing_ticks_reader calls ctx.gate.check with an incrementing
# sequence number
# ---------------------------------------------------------------------------

class _FakeGateRecorder:
    """Fake `tick_vault.reconcile.Gate`: records every `check(sequence_
    number, report)` call and always returns a fixed decision, so the
    test can assert on the sequence numbers `_pandas_reconcile_step`
    passed through without depending on real divergence math."""

    def __init__(self):
        self.calls = []

    def check(self, sequence_number, report):
        from tick_vault.reconcile import GateDecision

        self.calls.append((sequence_number, report.status))
        return GateDecision(hold=False, reason="PASS")


def _ticks_df_for_reconcile_test():
    import datetime as _dt

    from tick_vault.tick_parser import parse_tick_payload

    observed_at = _dt.datetime(2026, 7, 6, 20, 0, tzinfo=_dt.timezone.utc)
    trade_date = _dt.date(2026, 7, 6)  # a Monday

    def _ts_ms(hour, minute):
        from zoneinfo import ZoneInfo

        local = _dt.datetime(
            trade_date.year, trade_date.month, trade_date.day, hour, minute,
            tzinfo=ZoneInfo("America/New_York"),
        )
        epoch = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
        return (local.astimezone(_dt.timezone.utc) - epoch) // _dt.timedelta(milliseconds=1)

    payload = [
        {"ts": _ts_ms(9, 30), "price": 100.0, "shares": 100, "seq": 1, "sl": "@   ",
         "mkt": "Q", "sub_mkt": "", "ex": "US"},
        {"ts": _ts_ms(15, 59), "price": 101.0, "shares": 50, "seq": 2, "sl": "@   ",
         "mkt": "Q", "sub_mkt": "", "ex": "US"},
    ]
    return parse_tick_payload(
        payload, capture_id="cap_1", listing_id="lst_a", instrument_id="ins_a",
        observed_at=observed_at,
    )


class _FakeReferenceClientForReconcile:
    """Fake `tick_vault.eodhd_reference.ReferenceClient`: returns a
    canned EOD frame whose close/volume EXACTLY match the fake ticks
    frame's own aggregated daily bar, so `reconcile_tranche` reports
    `PASS` (this test is about sequence-number plumbing, not divergence
    math, which `tests/test_reconcile_core.py` already covers)."""

    def __init__(self, transport, store, api_token):
        self.calls = []

    def get_eod(self, symbol, from_date, to_date, *, exchange=None, observed_at=None):
        import pandas as pd

        self.calls.append((symbol, from_date, to_date, exchange))
        df = pd.DataFrame([
            {
                "date": dt.date(2026, 7, 6), "open": 100.0, "high": 101.0,
                "low": 100.0, "close": 101.0, "adjusted_close": 101.0, "volume": 150.0,
            },
        ])
        return df, None


def test_pandas_reconcile_step_calls_gate_check_with_incrementing_sequence():
    import tick_vault.eodhd_reference as eodhd_reference_mod

    ticks_df = _ticks_df_for_reconcile_test()
    fake_gate = _FakeGateRecorder()

    ctx = cli.loop_mod.LoopContext(
        root="mem://test",
        transport=object(),
        store=object(),
        api_token="real-eod-key",
        existing_ticks_reader=lambda root, listing_id, week_monday=None: ticks_df,
        gate=fake_gate,
    )
    item = {
        "listing_id": "lst_a",
        "vendor_symbol_at_date": "AAPL.US",
        "week_monday": dt.date(2026, 7, 6),
        "eodhd_exchange_code": "US",
    }

    original_client = eodhd_reference_mod.ReferenceClient
    eodhd_reference_mod.ReferenceClient = _FakeReferenceClientForReconcile
    try:
        decision_0 = cli._pandas_reconcile_step(ctx, item, "unused_fetch_result", 0)
        decision_1 = cli._pandas_reconcile_step(ctx, item, "unused_fetch_result", 1)
    finally:
        eodhd_reference_mod.ReferenceClient = original_client

    assert decision_0.hold is False
    assert decision_1.hold is False
    assert fake_gate.calls == [(0, "PASS"), (1, "PASS")]


def test_pandas_reconcile_step_returns_none_without_existing_ticks_reader():
    ctx = cli.loop_mod.LoopContext(root="mem://test")
    item = {"listing_id": "lst_a", "vendor_symbol_at_date": "AAPL.US", "week_monday": dt.date(2026, 7, 6)}
    assert cli._pandas_reconcile_step(ctx, item, "unused", 0) is None


def test_build_ctx_from_env_wires_reconcile_step_only_with_real_api_key():
    old_key = os.environ.get("EOD_API_KEY")
    try:
        os.environ.pop("EOD_API_KEY", None)
        ctx_no_key = cli.build_ctx_from_env()
        assert ctx_no_key.reconcile_step is None

        os.environ["EOD_API_KEY"] = "a-real-looking-key"
        ctx_with_key = cli.build_ctx_from_env()
        assert ctx_with_key.reconcile_step is cli._pandas_reconcile_step
        assert ctx_with_key.existing_ticks_reader is cli._default_existing_ticks_reader
    finally:
        if old_key is None:
            os.environ.pop("EOD_API_KEY", None)
        else:
            os.environ["EOD_API_KEY"] = old_key


# ---------------------------------------------------------------------------
# format_status (pure)
# ---------------------------------------------------------------------------

def test_format_status_basic():
    text = cli.format_status(
        {"PENDING": 10, "COMPLETE": 5},
        (None, None),
        open_dq_count=2,
        budget_state={"consecutive_waits": 1, "max_consecutive_waits": 12},
    )
    assert "PENDING: 10" in text
    assert "COMPLETE: 5" in text
    assert "open data-quality issues: 2" in text
    assert "1/12 consecutive 429 waits" in text


def test_format_status_no_frontier():
    text = cli.format_status({}, None, 0, {})
    assert "frontier (PENDING weeks): none" in text


# ---------------------------------------------------------------------------
# handle_calibrate: measured bytes_written (task-10 fix-round - this used
# to be hardcoded to 0, so every calibration report's projected
# storage-TB figure always read zero in a real run)
# ---------------------------------------------------------------------------

class _FakeCalibrateCtx:
    """A minimal stand-in for `loop_mod.LoopContext`, exposing exactly
    the attributes `handle_calibrate` touches - including `config`/
    `ingestion_run_writer`/`reconcile_step` now that `handle_calibrate`
    brackets its work with `loop_mod.open_run`/`close_run` (I7 fix-round)
    and honestly reports whether reconciliation ran (I3 fix-round)."""

    def __init__(self, root, manifest_df, legacy_root=None):
        self.root = root
        self._manifest_df = manifest_df
        self.legacy_progress_root = legacy_root
        self.transport = None
        self.store = None
        self.span_memory = None
        self.budget = None
        self.api_token = "REDACTED"
        self.tick_writer = None
        self.existing_ticks_reader = None
        self.verifier = None
        self.config = cli.loop_mod.LoopConfig()
        self.reconcile_step = None
        self.ingestion_run_writer = _FakeIngestionRunWriter()

    def manifest_reader(self, root):
        return self._manifest_df

    def now(self):
        return dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)


class _FakeIngestionRunWriter:
    """Fake `ops.ingestion_run` writer for `LoopContext`-shaped fakes in
    this file - avoids `tick_vault.capture.append_rows`'s real (lazy
    pyarrow/deltalake) default, which is unavailable in this sandbox."""

    def __init__(self):
        self.writes = []

    def __call__(self, root, table, df):
        self.writes.append((table, df.copy()))


def test_handle_calibrate_measures_real_bytes_written():
    from tick_vault.engine import FetchResult

    d = _tmpdir()
    try:
        week = dt.date(2026, 8, 31)
        manifest_df = pd.DataFrame([
            {"work_id": "wrk_a", "listing_id": "lst_a", "week_monday": week},
            {"work_id": "wrk_b", "listing_id": "lst_b", "week_monday": week},
        ])
        ctx = _FakeCalibrateCtx(root=d, manifest_df=manifest_df)

        def fake_fetch_week(transport, store, root, item, **kwargs):
            # Write a real file under the fake VAULT_ROOT, like a real
            # bronze capture would - this is what dir_bytes must detect.
            bronze_dir = os.path.join(root, "bronze")
            os.makedirs(bronze_dir, exist_ok=True)
            with open(os.path.join(bronze_dir, f"{item['work_id']}.bin"), "wb") as f:
                f.write(b"x" * 12345)
            return FetchResult(status="COMPLETE", rows_written=100, captures=1, wall_minutes=0.5)

        import tick_vault.engine as engine_mod
        original_fetch_week = engine_mod.fetch_week
        engine_mod.fetch_week = fake_fetch_week

        old_env = os.environ.get("CALIBRATION_REPORT")
        report_path = os.path.join(d, "report.md")
        os.environ["CALIBRATION_REPORT"] = report_path
        try:
            args = cli.build_parser().parse_args(["calibrate", "--week", "2026-08-31"])
            rc = cli.handle_calibrate(args, ctx_factory=lambda: ctx)
            assert rc == 0
        finally:
            engine_mod.fetch_week = original_fetch_week
            if old_env is None:
                os.environ.pop("CALIBRATION_REPORT", None)
            else:
                os.environ["CALIBRATION_REPORT"] = old_env

        assert os.path.isfile(report_path)
        content = open(report_path).read()
        assert "bytes_written: 24690" in content  # 2 x 12345 bytes written
        # projected_storage_tb must be nonzero now that bytes are measured.
        import re
        m = re.search(r"projected_storage_tb: ([0-9.]+)", content)
        assert m is not None
        assert float(m.group(1)) > 0.0
    finally:
        shutil.rmtree(d)


def test_backfill_loop_workers_flag_replaces_the_frozen_config():
    # Regression (2026-09-17): LoopConfig is frozen, so `--workers N` must
    # REBUILD it. Assigning the field raised FrozenInstanceError in production
    # while every ctx stub in these tests has no `.config` to catch it.
    d = _tmpdir()
    try:
        report_path = os.path.join(d, "ep15-calibration.md")
        with open(report_path, "w") as f:
            f.write("# calibration report\n")
        old_env = os.environ.get("CALIBRATION_REPORT")
        os.environ["CALIBRATION_REPORT"] = report_path

        ctx = cli.loop_mod.LoopContext(root="mem://test", reconcile_step=(lambda *a, **k: None))
        assert ctx.config.workers == 1  # default: the serial loop

        recorder = _Recorder(return_value=0)
        original = _patch_handler("run_backfill_loop", recorder)
        try:
            args = cli.build_parser().parse_args(["backfill", "--loop", "--workers", "6"])
            assert cli.handle_backfill(args, ctx_factory=lambda: ctx) == 0
            assert ctx.config.workers == 6
            assert len(recorder.calls) == 1
        finally:
            _restore_handler("run_backfill_loop", original)
            if old_env is None:
                os.environ.pop("CALIBRATION_REPORT", None)
            else:
                os.environ["CALIBRATION_REPORT"] = old_env
    finally:
        shutil.rmtree(d)

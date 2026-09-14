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

        recorder = _Recorder(return_value=0)
        original = _patch_handler("run_backfill_loop", recorder)
        try:
            args = cli.build_parser().parse_args(["backfill", "--loop"])
            rc = cli.handle_backfill(args, ctx_factory=lambda: "FAKE_CTX")
            assert rc == 0
            assert len(recorder.calls) == 1
            assert recorder.calls[0][0] == "FAKE_CTX"
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
    the attributes `handle_calibrate` touches."""

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

    def manifest_reader(self, root):
        return self._manifest_df

    def now(self):
        return dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)


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

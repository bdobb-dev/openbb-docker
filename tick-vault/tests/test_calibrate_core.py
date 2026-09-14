"""Runnable (pyarrow/deltalake/pytest-free) tests for `tick_vault.calibrate`
(task-10 brief): legacy sip_backfill progress-JSON parsing, the
projection math (exact hand-computed expected numbers per the module's
own worked example), and the report writer."""
import datetime as dt
import json
import os
import shutil
import tempfile

from tick_vault.calibrate import (
    CalibrationMeasurements,
    calibration_projection,
    read_legacy_wall_minutes,
    write_calibration_report,
)


def _tmpdir():
    return tempfile.mkdtemp(prefix="tick_vault_calibrate_test_")


# ---------------------------------------------------------------------------
# read_legacy_wall_minutes
# ---------------------------------------------------------------------------

def test_read_legacy_wall_minutes_parses_crafted_json_files():
    d = _tmpdir()
    try:
        with open(os.path.join(d, "2024-01-01.json"), "w") as f:
            json.dump({
                "wall_minutes": 42.5,
                "finished": "2024-01-02T00:00:00+00:00",
                "settled": "2024-01-03T00:00:00+00:00",
                "done": {"AAPL": True},
                "failed": {},
                "empty": {},
            }, f)
        with open(os.path.join(d, "2024-01-08.json"), "w") as f:
            json.dump({
                "wall_minutes": 37.0,
                "finished": None,
                "settled": None,
                "done": {},
                "failed": {"XYZ": "timeout"},
                "empty": {},
            }, f)
        # a non-json file must be ignored
        with open(os.path.join(d, "notes.txt"), "w") as f:
            f.write("ignore me")

        df = read_legacy_wall_minutes(d)
        assert list(df["week"]) == [dt.date(2024, 1, 1), dt.date(2024, 1, 8)]
        assert list(df["wall_minutes"]) == [42.5, 37.0]
        assert df.iloc[0]["finished"] == "2024-01-02T00:00:00+00:00"
        assert df.iloc[1]["finished"] is None
    finally:
        shutil.rmtree(d)


def test_read_legacy_wall_minutes_missing_dir_returns_empty_frame():
    df = read_legacy_wall_minutes("/no/such/directory/at/all")
    assert df.empty
    assert list(df.columns) == ["week", "wall_minutes", "finished", "settled"]


def test_read_legacy_wall_minutes_none_returns_empty_frame():
    df = read_legacy_wall_minutes(None)
    assert df.empty


def test_read_legacy_wall_minutes_skips_bad_json():
    d = _tmpdir()
    try:
        with open(os.path.join(d, "2024-01-01.json"), "w") as f:
            f.write("{not valid json")
        with open(os.path.join(d, "not-a-date.json"), "w") as f:
            json.dump({"wall_minutes": 1.0}, f)
        df = read_legacy_wall_minutes(d)
        assert df.empty
    finally:
        shutil.rmtree(d)


# ---------------------------------------------------------------------------
# calibration_projection - exact hand-computed expected numbers
# ---------------------------------------------------------------------------

def test_calibration_projection_hand_computed_numbers():
    # measured.symbols == universe_size (505) - the calibration week
    # covers the whole S&P 500, so the per-symbol rate * universe_size
    # collapses back to the raw measured totals (see module docstring).
    measured = CalibrationMeasurements(
        calls=300, wall_minutes=40.0, rows=50_000, bytes_written=2_000_000,
        symbols=505, week=dt.date(2026, 8, 31),
    )
    projection = calibration_projection(
        measured, universe_size=505, weeks_total=522, halving_overhead=1.15,
    )

    assert projection["projected_calls"] == round(300 * 522 * 1.15)  # 180090
    assert projection["projected_calls"] == 180090

    expected_wall_minutes = 40.0 * 522 * 1.15
    assert abs(projection["projected_wall_minutes"] - expected_wall_minutes) < 1e-9
    expected_wall_clock_days = expected_wall_minutes / (60.0 * 24.0) / 6
    assert abs(projection["projected_wall_clock_days"] - expected_wall_clock_days) < 1e-9

    assert projection["projected_rows"] == round(50_000 * 522)
    assert projection["projected_rows"] == 26_100_000

    expected_bytes = round(2_000_000 * 522)
    assert projection["projected_bytes"] == expected_bytes
    assert abs(projection["projected_storage_tb"] - expected_bytes / 1e12) < 1e-12

    assert projection["legacy_wall_minutes_median"] is None


def test_calibration_projection_partial_universe_scales_up():
    # A calibration run over only 100 of the 505 symbols: the per-symbol
    # rate is still scaled by the FULL universe_size.
    measured = CalibrationMeasurements(
        calls=100, wall_minutes=10.0, rows=1_000, bytes_written=100_000,
        symbols=100, week=dt.date(2026, 8, 31),
    )
    projection = calibration_projection(
        measured, universe_size=505, weeks_total=522, halving_overhead=1.0,
    )
    expected_calls = round((100 / 100) * 505 * 522 * 1.0)
    assert projection["projected_calls"] == expected_calls
    assert expected_calls == 263_610


def test_calibration_projection_includes_legacy_median_when_frame_provided():
    import pandas as pd

    measured = CalibrationMeasurements(
        calls=300, wall_minutes=40.0, rows=50_000, bytes_written=2_000_000,
        symbols=505, week=dt.date(2026, 8, 31),
    )
    legacy_df = pd.DataFrame([
        {"week": dt.date(2024, 1, 1), "wall_minutes": 30.0, "finished": None, "settled": None},
        {"week": dt.date(2024, 1, 8), "wall_minutes": 50.0, "finished": None, "settled": None},
        {"week": dt.date(2024, 1, 15), "wall_minutes": 40.0, "finished": None, "settled": None},
    ])
    projection = calibration_projection(measured, legacy_df=legacy_df)
    assert projection["legacy_wall_minutes_median"] == 40.0


def test_calibration_projection_rejects_zero_symbols():
    measured = CalibrationMeasurements(
        calls=0, wall_minutes=0.0, rows=0, bytes_written=0, symbols=0, week=dt.date(2026, 1, 1),
    )
    try:
        calibration_projection(measured)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# write_calibration_report
# ---------------------------------------------------------------------------

def test_write_calibration_report_contains_key_figures():
    d = _tmpdir()
    try:
        path = os.path.join(d, "nested", "2026-09-14-ep15-calibration.md")
        measured = CalibrationMeasurements(
            calls=300, wall_minutes=40.0, rows=50_000, bytes_written=2_000_000,
            symbols=505, week=dt.date(2026, 8, 31),
        )
        projection = calibration_projection(measured)
        write_calibration_report(path, projection, measured)

        assert os.path.isfile(path)
        content = open(path).read()
        assert "2026-08-31" in content
        assert str(projection["projected_calls"]) in content
        assert "projected_wall_clock_days" in content
        assert "projected_storage_tb" in content
    finally:
        shutil.rmtree(d)


def test_write_calibration_report_includes_legacy_median_when_present():
    import pandas as pd

    d = _tmpdir()
    try:
        path = os.path.join(d, "report.md")
        measured = CalibrationMeasurements(
            calls=300, wall_minutes=40.0, rows=50_000, bytes_written=2_000_000,
            symbols=505, week=dt.date(2026, 8, 31),
        )
        legacy_df = pd.DataFrame([
            {"week": dt.date(2024, 1, 1), "wall_minutes": 40.0, "finished": None, "settled": None},
        ])
        projection = calibration_projection(measured, legacy_df=legacy_df)
        write_calibration_report(path, projection, measured, legacy_df)
        content = open(path).read()
        assert "legacy_wall_minutes_median" in content
        assert "40.00" in content
    finally:
        shutil.rmtree(d)

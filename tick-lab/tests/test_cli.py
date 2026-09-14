"""CLI argument handling and the filename -> (symbol, kind) convention."""

import argparse
import zipfile

import pandas as pd
import pytest

from tick_lab import cli
from tick_lab.cli import ADAPTERS, _iter_members, date_or_datetime, main, symbol_from_filename
from tick_lab.store import LibraryNotFoundError, StoreWriteError


@pytest.mark.parametrize(
    "name,expected",
    [
        ("MSFT_trades_2023-05-12.txt", ("MSFT", "trade")),
        ("GOOG_quotes_2023-05-12.txt", ("GOOG", "quote")),
        ("MSFT_trade.txt", ("MSFT", "trade")),
        ("msft_quotes.csv", ("MSFT", "quote")),
    ],
)
def test_symbol_and_kind_from_filename(name, expected):
    assert symbol_from_filename(name) == expected


def test_unrecognised_filename_is_rejected():
    with pytest.raises(ValueError, match="cannot tell"):
        symbol_from_filename("random-data.txt")


def test_yfinance_is_registered():
    assert "yfinance" in ADAPTERS


def test_eodhd_api_is_registered():
    assert "eodhd-api" in ADAPTERS


def test_eodhd_api_is_the_default_reference():
    # v11.1.0: the per-minute 2023 comparison yfinance cannot serve becomes
    # possible through the stack's own OpenBB API, so that is now the
    # default -- yfinance remains available via --reference yfinance.
    parser = cli.build_parser()
    args = parser.parse_args(["compare", "--symbol", "MSFT", "--date", "2023-05-12"])
    assert args.reference == "eodhd-api"


def test_no_subcommand_exits_nonzero(capsys):
    assert main([]) == 2


def test_unknown_reference_is_rejected(capsys):
    with pytest.raises(SystemExit):
        main(["compare", "--symbol", "MSFT", "--date", "2023-05-12",
              "--reference", "nope"])


def test_missing_eodhd_api_env_is_reported_cleanly(capsys, monkeypatch):
    # Step 4's requirement: constructing the default adapter with none of
    # OPENBB_URL / OPENBB_API_USERNAME / OPENBB_API_PASSWORD set must not
    # raise out of cmd_compare -- it must be reported like any other
    # configuration problem, with the exit code main() already uses for one.
    # Ticks must actually be found first, or cmd_compare returns 1 for that
    # ("no ticks stored") before ever reaching adapter construction.
    _set_dummy_arctic_env(monkeypatch)
    for key in ("OPENBB_URL", "OPENBB_API_USERNAME", "OPENBB_API_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    _install_fake_store(monkeypatch, read_result=_fake_trades_two_days())

    code = cli.main(["compare", "--symbol", "MSFT", "--date", "2023-05-12"])

    assert code == 1
    err = capsys.readouterr().err
    assert "cannot use --reference eodhd-api" in err
    assert "OPENBB_URL" in err


def test_bad_config_is_reported_without_a_traceback(capsys, monkeypatch):
    for key in ("ARCTICDB_S3_ENDPOINT", "ARCTICDB_S3_BUCKET",
                "ARCTICDB_S3_ACCESS", "ARCTICDB_S3_SECRET"):
        monkeypatch.delenv(key, raising=False)
    code = main(["compare", "--symbol", "MSFT", "--date", "2023-05-12"])
    assert code == 1
    assert "ARCTICDB_S3_ENDPOINT" in capsys.readouterr().err


# --- --date/--end validation -------------------------------------------
#
# argparse gives --date/--end no type= validator in the brief's sample code,
# so a typo like "2023-13-45" or "last-tuesday" would reach pandas/ArcticDB
# unvalidated and surface as a confusing parse error deep in the stack.
# date_or_datetime() is the type= callable that rejects it right at the
# argparse boundary instead, with a message naming the expected format.


@pytest.mark.parametrize(
    "value",
    [
        "2023-05-12",
        "2023-05-12T15:00:00",
        "2023-05-12 15:00:00",
        "2023-05-12T00:00:00",
    ],
)
def test_date_or_datetime_accepts_what_the_store_supports(value):
    assert date_or_datetime(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "2023-13-45",
        "last-tuesday",
        "05/12/2023",
        "2023-02-30",
        "not-a-date",
    ],
)
def test_date_or_datetime_rejects_bad_shapes(value):
    with pytest.raises(argparse.ArgumentTypeError, match="YYYY-MM-DD"):
        date_or_datetime(value)


def test_bad_date_is_rejected_by_argparse_before_reaching_the_store(capsys):
    with pytest.raises(SystemExit):
        main(["compare", "--symbol", "MSFT", "--date", "2023-13-45"])
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_bad_end_is_rejected_by_argparse_before_reaching_the_store(capsys):
    with pytest.raises(SystemExit):
        main(["compare", "--symbol", "MSFT", "--date", "2023-05-12",
              "--end", "last-tuesday"])
    assert "YYYY-MM-DD" in capsys.readouterr().err


# --- _iter_members: `load` takes a zip OR an already-extracted directory --
#
# The real FirstRate zip is third-party licensed and never committed, so a
# reader may well have extracted it already -- both paths matter.


def test_iter_members_reads_an_extracted_directory(tmp_path):
    (tmp_path / "MSFT_trades_2023-05-12.txt").write_text("a")
    (tmp_path / "GOOG_quotes_2023-05-12.txt").write_text("b")
    (tmp_path / "_readme.txt").write_text("skip me")
    (tmp_path / ".DS_Store").write_text("finder noise")

    names = sorted(name for name, _ in _iter_members(tmp_path))
    assert names == ["GOOG_quotes_2023-05-12.txt", "MSFT_trades_2023-05-12.txt"]


def test_iter_members_reads_a_zip(tmp_path):
    zpath = tmp_path / "sample.zip"
    with zipfile.ZipFile(zpath, "w") as archive:
        archive.writestr("MSFT_trades_2023-05-12.txt", "a")
        archive.writestr("GOOG_quotes_2023-05-12.txt", "b")

    names = sorted(name for name, _ in _iter_members(zpath))
    assert names == ["GOOG_quotes_2023-05-12.txt", "MSFT_trades_2023-05-12.txt"]


def test_iter_members_skips_macos_zip_noise(tmp_path):
    # A zip built on macOS commonly carries a "__MACOSX/" resource-fork
    # sibling for every real entry -- checking only the leaf filename (not
    # the full path) lets "__MACOSX/junk" slip through as a bogus member.
    zpath = tmp_path / "sample.zip"
    with zipfile.ZipFile(zpath, "w") as archive:
        archive.writestr("MSFT_trades_2023-05-12.txt", "a")
        archive.writestr("__MACOSX/._MSFT_trades_2023-05-12.txt", "junk")
        archive.writestr("__MACOSX/junk", "junk")

    names = [name for name, _ in _iter_members(zpath)]
    assert names == ["MSFT_trades_2023-05-12.txt"]


def test_iter_members_raises_a_clean_error_for_a_missing_path(tmp_path):
    with pytest.raises(ValueError, match="cannot read"):
        list(_iter_members(tmp_path / "does-not-exist.zip"))


def test_iter_members_raises_a_clean_error_for_a_non_zip_file(tmp_path):
    bad = tmp_path / "not-a-zip.zip"
    bad.write_text("plain text, not a zip")
    with pytest.raises(ValueError, match="cannot read"):
        list(_iter_members(bad))


# --- cmd_load: per-file failures are reported and the run CONTINUES -----
#
# Neither a bad file (fails to parse) nor a bad write (ArcticDB rejects it,
# a network blip, ...) may abort the rest of the batch. Every scenario below
# drives `main(["load", ...])` end to end against a fake TickStore -- no
# network, no MinIO -- so these tests actually exercise cmd_load, which the
# rest of this file never did.

ARCTIC_ENV_KEYS = (
    "ARCTICDB_S3_ENDPOINT",
    "ARCTICDB_S3_BUCKET",
    "ARCTICDB_S3_ACCESS",
    "ARCTICDB_S3_SECRET",
)


def _set_dummy_arctic_env(monkeypatch):
    for key in ARCTIC_ENV_KEYS:
        monkeypatch.setenv(key, f"dummy-{key}")


class _FakeStore:
    """Stands in for TickStore: records calls, never touches ArcticDB."""

    def __init__(self):
        self.written = []
        self.read_calls = []
        self.fail_write_symbols = set()
        self.read_result = pd.DataFrame()

    def write(self, library, symbol, frame, metadata=None):
        if symbol in self.fail_write_symbols:
            raise StoreWriteError(f"simulated write failure for {symbol!r}")
        self.written.append((library, symbol, frame, metadata))

    def read(self, library, symbol, start=None, end=None):
        self.read_calls.append((library, symbol, start, end))
        return self.read_result


def _install_fake_store(monkeypatch, **kwargs):
    store = _FakeStore()
    for key, value in kwargs.items():
        setattr(store, key, value)
    monkeypatch.setattr(cli, "TickStore", lambda cfg: store)
    return store


def _write_trade_file(dir_path, symbol, date="2023-05-12"):
    text = f"{date} 09:30:00,100.00,10,Q,\n{date} 09:30:01,100.05,20,Q,\n"
    (dir_path / f"{symbol}_trades_{date}.txt").write_text(text)


def test_cmd_load_parse_failure_is_skipped_and_good_files_still_written(
    tmp_path, monkeypatch
):
    _set_dummy_arctic_env(monkeypatch)
    store = _install_fake_store(monkeypatch)
    _write_trade_file(tmp_path, "AAA")
    _write_trade_file(tmp_path, "BBB")
    # 2 fields matches neither the 5-field trade nor the 7/8-field quote
    # shape -- detect_kind raises FirstRateFormatError.
    (tmp_path / "CCC_trades_2023-05-12.txt").write_text("bad,line\n")

    code = cli.main(["load", str(tmp_path)])

    written_symbols = {symbol for _, symbol, _, _ in store.written}
    assert written_symbols == {"AAA", "BBB"}
    assert code == 1


def test_cmd_load_write_failure_is_skipped_and_good_files_still_written(
    tmp_path, monkeypatch
):
    # This is Finding 1: store.write() used to sit OUTSIDE the per-file
    # try/except, so a write failure on one file killed the whole load and
    # lost every remaining file. Run against the unfixed code and this test
    # fails (StoreWriteError propagates out of main() uncaught); against the
    # fix, AAA and CCC are still written and the run reports failure via the
    # exit code, not an exception.
    _set_dummy_arctic_env(monkeypatch)
    _write_trade_file(tmp_path, "AAA")
    _write_trade_file(tmp_path, "BBB")
    _write_trade_file(tmp_path, "CCC")
    store = _install_fake_store(monkeypatch, fail_write_symbols={"BBB"})

    code = cli.main(["load", str(tmp_path)])

    written_symbols = {symbol for _, symbol, _, _ in store.written}
    assert written_symbols == {"AAA", "CCC"}
    assert code == 1


def test_cmd_load_all_good_writes_everything_and_exits_zero(tmp_path, monkeypatch):
    _set_dummy_arctic_env(monkeypatch)
    _write_trade_file(tmp_path, "AAA")
    _write_trade_file(tmp_path, "BBB")
    store = _install_fake_store(monkeypatch)

    code = cli.main(["load", str(tmp_path)])

    written_symbols = {symbol for _, symbol, _, _ in store.written}
    assert written_symbols == {"AAA", "BBB"}
    assert code == 0


def test_cmd_load_dry_run_writes_nothing_and_exits_zero(tmp_path, monkeypatch, capsys):
    _set_dummy_arctic_env(monkeypatch)
    _write_trade_file(tmp_path, "AAA")
    store = _install_fake_store(monkeypatch)

    code = cli.main(["load", str(tmp_path), "--dry-run"])

    assert store.written == []
    assert code == 0
    assert "would write" in capsys.readouterr().out


def test_cmd_load_reports_a_bad_top_level_path_cleanly(tmp_path, monkeypatch, capsys):
    _set_dummy_arctic_env(monkeypatch)
    _install_fake_store(monkeypatch)
    bad = tmp_path / "not-a-zip.zip"
    bad.write_text("plain text, not a zip")

    code = cli.main(["load", str(bad)])

    assert code == 1
    assert "cannot read" in capsys.readouterr().err


# --- cmd_compare: same window on both sides, clean errors, symbol case ---


def _fake_trades_two_days():
    idx = pd.DatetimeIndex(
        [
            "2023-05-12T13:30:00Z",
            "2023-05-12T13:31:00Z",
            "2023-05-13T13:30:00Z",
            "2023-05-13T13:31:00Z",
        ]
    )
    return pd.DataFrame(
        {"price": [100.0, 100.5, 101.0, 101.5], "volume": [10, 20, 30, 40]}, index=idx
    )


class _FakeAdapter:
    """A reference adapter that never touches the network."""

    name = "fake"
    supported_intervals = ("1m", "5m", "15m", "30m", "1h", "1d")

    def fetch(self, symbol, start, end, interval):
        idx = pd.date_range("2023-05-12T13:30:00Z", periods=2, freq="1min")
        return pd.DataFrame(
            {
                "open": [1.0, 2.0],
                "high": [1.0, 2.0],
                "low": [1.0, 2.0],
                "close": [1.0, 2.0],
                "volume": [10, 20],
            },
            index=idx,
        )


def test_compare_reads_our_ticks_over_the_full_requested_window(monkeypatch):
    # Finding 3: cmd_compare used to read our own ticks with
    # start=args.date, end=args.date regardless of --end, silently
    # truncating our side to day one while the reference spanned the whole
    # window. Both sides must use the same (args.date, args.end) window.
    _set_dummy_arctic_env(monkeypatch)
    store = _install_fake_store(monkeypatch, read_result=_fake_trades_two_days())
    monkeypatch.setitem(cli.ADAPTERS, "yfinance", _FakeAdapter)

    code = cli.main(
        ["compare", "--symbol", "MSFT", "--date", "2023-05-12", "--end", "2023-05-13",
         "--reference", "yfinance"]
    )

    assert code == 0
    assert store.read_calls == [(cli.TRADE_LIBRARY, "MSFT", "2023-05-12", "2023-05-13")]


def test_compare_reports_a_missing_library_cleanly(monkeypatch, capsys):
    # Finding 4: TickStore.read raises LibraryNotFoundError (a ValueError
    # subclass) with a message naming `tick-lab load`, but cmd_compare did
    # not catch it and main() only catches ConfigError -- so the single most
    # likely first mistake (compare before ever loading) produced a raw
    # traceback instead of that friendly message.
    _set_dummy_arctic_env(monkeypatch)

    def _raise(*_args, **_kwargs):
        raise LibraryNotFoundError(
            "ArcticDB library 'ticks' does not exist. run `tick-lab load` first"
        )

    _install_fake_store(monkeypatch)
    monkeypatch.setattr(cli, "TickStore", lambda cfg: type("S", (), {"read": _raise})())

    code = cli.main(["compare", "--symbol", "MSFT", "--date", "2023-05-12"])

    assert code == 1
    err = capsys.readouterr().err
    assert "does not exist" in err
    assert "tick-lab load" in err


def test_compare_uppercases_the_symbol_before_reading(monkeypatch):
    # Finding 6: the loader upper-cases the symbol at write time, so a
    # lowercase --symbol must be normalised the same way or a real,
    # previously-loaded symbol is reported as "no ticks stored".
    _set_dummy_arctic_env(monkeypatch)
    store = _install_fake_store(monkeypatch, read_result=_fake_trades_two_days())
    monkeypatch.setitem(cli.ADAPTERS, "yfinance", _FakeAdapter)

    code = cli.main(
        ["compare", "--symbol", "msft", "--date", "2023-05-12", "--reference", "yfinance"]
    )

    assert code == 0
    assert store.read_calls[0][1] == "MSFT"

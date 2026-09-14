"""The durable half of the subscription manager."""

import json

import pytest

from app.watchlist import Watchlist


def test_a_missing_file_loads_as_empty_rather_than_raising(tmp_path):
    """live-grid must start even with no watchlist yet -- this is the first-run case."""
    assert Watchlist(tmp_path / "nope.json").symbols() == []


def test_add_then_reload_from_disk_keeps_the_symbol(tmp_path):
    """The whole point: survive a restart."""
    p = tmp_path / "w.json"
    Watchlist(p).add("AAPL")
    assert Watchlist(p).symbols() == ["AAPL"]


def test_symbols_come_back_sorted(tmp_path):
    w = Watchlist(tmp_path / "w.json")
    for s in ("TSLA", "AAPL", "MSFT"):
        w.add(s)
    assert w.symbols() == ["AAPL", "MSFT", "TSLA"]


def test_symbols_normalise_so_case_cannot_duplicate_one(tmp_path):
    """Same normalisation as LeaseRegistry and classify(): strip().upper()."""
    w = Watchlist(tmp_path / "w.json")
    assert w.add(" aapl ") is True
    assert w.add("AAPL") is False
    assert w.symbols() == ["AAPL"]


def test_add_reports_whether_it_changed_anything(tmp_path):
    w = Watchlist(tmp_path / "w.json")
    assert w.add("AAPL") is True
    assert w.add("AAPL") is False


def test_remove_reports_whether_it_changed_anything(tmp_path):
    w = Watchlist(tmp_path / "w.json")
    w.add("AAPL")
    assert w.remove("AAPL") is True
    assert w.remove("AAPL") is False
    assert w.symbols() == []


def test_a_corrupt_file_loads_as_empty_and_does_not_raise(tmp_path):
    """A bad file must not stop live-grid from serving. Losing a watchlist is
    recoverable; a container that will not start is not."""
    p = tmp_path / "w.json"
    p.write_text("{ this is not json")
    assert Watchlist(p).symbols() == []


def test_a_json_file_of_the_wrong_shape_loads_as_empty(tmp_path):
    """Valid JSON that is not a list of strings is just as unusable."""
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"symbols": ["AAPL"]}))
    assert Watchlist(p).symbols() == []


def test_the_write_is_atomic_so_a_reader_never_sees_a_partial_file(tmp_path):
    """Written to a temp file in the same directory, then os.replace'd. Checked by
    proving no stray temp file survives and the content is complete."""
    p = tmp_path / "w.json"
    w = Watchlist(p)
    for s in ("AAPL", "MSFT", "TSLA"):
        w.add(s)
    assert json.loads(p.read_text()) == ["AAPL", "MSFT", "TSLA"]
    leftovers = [f.name for f in tmp_path.iterdir() if f.name != "w.json"]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_the_parent_directory_is_created_if_absent(tmp_path):
    """The mount may be empty on first run."""
    p = tmp_path / "sub" / "dir" / "w.json"
    Watchlist(p).add("AAPL")
    assert p.exists()


def test_an_empty_or_blank_symbol_is_rejected(tmp_path):
    w = Watchlist(tmp_path / "w.json")
    assert w.add("   ") is False
    assert w.symbols() == []


def test_add_rolls_back_memory_when_the_write_fails(tmp_path):
    """A failed write must not leave the symbol pinned in memory-only: it
    would be fed and reported by the API, then vanish silently on restart."""
    # Point the watchlist file AT a directory: opening it for write raises
    # IsADirectoryError, a real failure _save() cannot swallow.
    p = tmp_path / "w.json"
    p.mkdir()
    w = Watchlist(p / "nested.json")
    w._path = p  # a directory, not a file -- os.replace(tmp, p) must fail
    with pytest.raises(OSError):
        w.add("AAPL")
    assert w.symbols() == []


def test_remove_rolls_back_memory_when_the_write_fails(tmp_path):
    p = tmp_path / "w.json"
    w = Watchlist(p)
    w.add("AAPL")
    real_dir = tmp_path / "blocker"
    real_dir.mkdir()
    w._path = real_dir  # same trick: os.replace onto a directory fails
    with pytest.raises(OSError):
        w.remove("AAPL")
    assert w.symbols() == ["AAPL"]

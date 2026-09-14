"""A durable set of subscribed symbols, backed by one JSON file.

Deliberately ignorant of feeds, caps and HTTP. Those live in the route layer,
and keeping them out of here is what lets this be tested without an app.

Stored as a plain JSON array so it can be read, edited or committed by hand.
"""

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PATH = "/data/watchlist.json"


def _normalise(symbol: str) -> str:
    """Same rule as LeaseRegistry.renew and classify(), so one symbol has one form."""
    return str(symbol).strip().upper()


class Watchlist:
    """Symbols pinned by the operator, persisted across restarts."""

    def __init__(self, path):
        self._path = Path(path)
        self._symbols: set[str] = set()
        self.reload()

    def reload(self) -> None:
        """Read the file. A missing, empty, corrupt or wrongly-shaped file is an
        empty watchlist, never an exception: losing the list is recoverable, a
        container that will not start is not."""
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            self._symbols = set()
            return
        except (OSError, ValueError) as exc:
            log.warning("watchlist at %s is unreadable, starting empty: %s", self._path, exc)
            self._symbols = set()
            return
        if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
            log.warning("watchlist at %s is not a list of strings, starting empty", self._path)
            self._symbols = set()
            return
        self._symbols = {_normalise(s) for s in raw if _normalise(s)}

    def symbols(self) -> list[str]:
        return sorted(self._symbols)

    def add(self, symbol: str) -> bool:
        """Add one symbol. False when it was already there or is blank."""
        sym = _normalise(symbol)
        if not sym or sym in self._symbols:
            return False
        previous = set(self._symbols)
        self._symbols.add(sym)
        try:
            self._save()
        except Exception:
            # A failed write must not leave memory ahead of disk: the symbol
            # would be pinned, fed and reported by the API for the rest of
            # this process, then silently vanish on the next restart.
            self._symbols = previous
            raise
        return True

    def remove(self, symbol: str) -> bool:
        """Remove one symbol. False when it was not there."""
        sym = _normalise(symbol)
        if sym not in self._symbols:
            return False
        previous = set(self._symbols)
        self._symbols.discard(sym)
        try:
            self._save()
        except Exception:
            self._symbols = previous
            raise
        return True

    def _save(self) -> None:
        """Write via a temp file in the SAME directory, then os.replace.

        Same directory because os.replace is only atomic within one filesystem,
        and the watchlist lives on a mount that is not the container's root.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(self.symbols(), handle, indent=2)
            os.replace(tmp, self._path)
        except BaseException:
            # Leave no debris if the write or the replace failed.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

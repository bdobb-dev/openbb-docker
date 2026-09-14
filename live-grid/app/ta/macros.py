"""Chart macros: named, reusable pane layouts.

A macro is validated when it loads, so a typo is a startup failure with a
filename in it rather than an empty pane at render time.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.ta.registry import Req, resolve

log = logging.getLogger("live-grid.ta")

BAKED_IN = Path(__file__).resolve().parent.parent.parent / "macros"


class MacroError(ValueError):
    """A macro file that cannot be trusted to render."""


@dataclass(frozen=True)
class PaneSpec:
    id: str
    height: float
    reqs: list[Req]
    guides: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class Macro:
    name: str
    label: str
    description: str
    panes: list[PaneSpec]


def macro_dirs() -> list[Path]:
    """Baked-in macros first, then the mounted override directory if set."""
    dirs = [BAKED_IN]
    override = os.getenv("TA_MACRO_DIR", "").strip()
    if override:
        dirs.append(Path(override))
    return dirs


def load_macro(path: Path) -> Macro:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise MacroError(f"{path.name}: invalid YAML: {exc}") from exc

    panes_raw = raw.get("panes") or []
    if not panes_raw:
        raise MacroError(f"{path.name}: a macro needs at least one pane")

    panes: list[PaneSpec] = []
    for index, pane in enumerate(panes_raw):
        pane_id = str(pane.get("id") or f"pane{index}")
        try:
            height = float(pane.get("height", 1))
        except (TypeError, ValueError):
            # A bare ValueError here would escape the caller's `except
            # MacroError` and crash differently than every other bad-macro path.
            raise MacroError(
                f"{path.name}: pane {pane_id!r} height must be a number, "
                f"got {pane.get('height')!r}"
            ) from None
        if height <= 0:
            raise MacroError(f"{path.name}: pane {pane_id!r} height must be positive")
        reqs = []
        for entry in pane.get("indicators") or []:
            spec = dict(entry)
            name = spec.pop("name", None)
            # `style` stays in spec deliberately: resolve() accepts it as
            # per-series presentation, and panes.py merges it over the
            # registry's default render. Popping it here silently discarded
            # a documented macro feature.
            if name not in _registry():
                raise MacroError(f"{path.name}: unknown indicator {name!r}")
            try:
                reqs.append(resolve(name, **spec))
            except ValueError as exc:
                raise MacroError(f"{path.name}: {exc}") from exc
        panes.append(PaneSpec(pane_id, height, reqs,
                              [float(g) for g in pane.get("guides") or []]))

    if sum(1 for p in panes if p.id == "price") != 1:
        raise MacroError(f"{path.name}: exactly one pane must have id 'price'")

    return Macro(path.stem, str(raw.get("label") or path.stem),
                 str(raw.get("description") or ""), panes)


def load_macros(directory: Path) -> dict[str, Macro]:
    """Every loadable macro in `directory`. A broken one is skipped, not fatal.

    The comprehension this replaces raised on the first bad file, so a single
    typo in a mounted macro blanked EVERY macro -- including the baked-in ones
    -- from the widget's dropdown. A hand-edited directory will contain typos;
    losing one macro is a proportionate consequence, losing all of them is not.
    The warning plus the file's absence from the dropdown is how its author
    finds out.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return {}
    loaded: dict[str, Macro] = {}
    for path in sorted(directory.glob("*.yml")):
        try:
            macro = load_macro(path)
        except MacroError as exc:
            log.warning("skipping macro %s: %s", path.name, exc)
            continue
        loaded[macro.name] = macro
    return loaded


def load_all() -> dict[str, Macro]:
    """Every macro from every directory; later directories win on name clash."""
    loaded: dict[str, Macro] = {}
    for directory in macro_dirs():
        loaded.update(load_macros(directory))
    return loaded


def _registry() -> dict:
    from app.ta.registry import REGISTRY

    return REGISTRY

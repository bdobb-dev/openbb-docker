"""Which series go where, and how tall each pane is.

Deliberately free of Plotly and of data: this is arithmetic and bookkeeping,
so it is testable without building a figure or fetching a bar.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.ta.macros import Macro
from app.ta.registry import Req, col_suffix, get


@dataclass(frozen=True)
class Series:
    column: str
    label: str
    render: dict


@dataclass
class Pane:
    id: str
    height: float
    is_price: bool
    series: list[Series] = field(default_factory=list)
    guides: list[float] = field(default_factory=list)
    reqs: list[Req] = field(default_factory=list)


def _series_for(req: Req) -> list[Series]:
    ind = get(req.name)
    suffix = _suffix(req)
    # A macro's per-series `style` overrides the registry's defaults key by
    # key, so `{color: ...}` recolours a line without discarding its type.
    style = req.params.get("style") or {}
    # `ind.render` is keyed by BASE column names; the computed frame names its
    # columns per (indicator, params), so each one is mapped to its suffix.
    return [
        Series(base + col_suffix(req),
               f"{ind.label}{suffix}" if i == 0 else base,
               {**render, **style})
        for i, (base, render) in enumerate(ind.render.items())
    ]


def _suffix(req: Req) -> str:
    numeric = [f"{v:g}" for k, v in req.params.items()
               if k != "style" and isinstance(v, (int, float))]
    return f"({','.join(numeric)})" if numeric else ""


def _key(req: Req) -> tuple:
    return (req.name, tuple(sorted(
        (k, v) for k, v in req.params.items() if k != "style")))


def assign(macro: Macro | None, picks: list[Req]) -> list[Pane]:
    """Build the pane list. A macro sets the layout; picks append to it."""
    panes: list[Pane] = []
    seen: set[tuple] = set()

    if macro is not None:
        for spec in macro.panes:
            pane = Pane(spec.id, spec.height, spec.id == "price",
                        guides=list(spec.guides))
            for req in spec.reqs:
                seen.add(_key(req))
                pane.series.extend(_series_for(req))
                pane.reqs.append(req)
                if not pane.guides:
                    pane.guides = list(get(req.name).guides)
            panes.append(pane)
    else:
        panes.append(Pane("price", 3.0, True))

    price = next(p for p in panes if p.is_price)
    for req in picks:
        if _key(req) in seen:
            continue
        seen.add(_key(req))
        if get(req.name).pane == "price":
            price.series.extend(_series_for(req))
            price.reqs.append(req)
        else:
            panes.append(Pane(req.name, 1.0, False, _series_for(req),
                              list(get(req.name).guides), [req]))
    return panes


def domains(panes: list[Pane], gap: float = 0.02) -> list[tuple[float, float]]:
    """Vertical (y0, y1) per pane, top-down. Index 0 is the top pane."""
    if not panes:
        return []
    # Enough panes and the gaps alone exceed 1.0, making every span negative
    # (y0 > y1), which Plotly rejects. Cap the total gap at half the height.
    gap = min(gap, 0.5 / max(len(panes) - 1, 1))
    available = 1.0 - gap * (len(panes) - 1)
    total = sum(p.height for p in panes) or 1.0
    out: list[tuple[float, float]] = []
    top = 1.0
    for pane in panes:
        height = pane.height / total * available
        out.append((round(top - height, 6), round(top, 6)))
        top -= height + gap
    return out


def all_reqs(panes: list[Pane]) -> list[Req]:
    """Every request across every pane, deduplicated, order preserved."""
    seen: set[tuple] = set()
    out: list[Req] = []
    for pane in panes:
        for req in pane.reqs:
            if _key(req) in seen:
                continue
            seen.add(_key(req))
            out.append(req)
    return out

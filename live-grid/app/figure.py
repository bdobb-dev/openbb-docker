# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Plotly figure JSON. The client renders it -- this image has no chart backend."""


def build_figure(symbol: str, bars: list[dict]) -> dict:
    """A candlestick figure for the given bars."""
    return {
        "data": [
            {
                "type": "candlestick",
                "name": symbol,
                "x": [b.get("date") for b in bars],
                "open": [b.get("open") for b in bars],
                "high": [b.get("high") for b in bars],
                "low": [b.get("low") for b in bars],
                "close": [b.get("close") for b in bars],
            }
        ],
        "layout": {
            "title": {"text": f"{symbol} — served through the kdb+ cache"},
            "xaxis": {"rangeslider": {"visible": False}},
            "margin": {"l": 40, "r": 20, "t": 40, "b": 40},
            "template": "plotly_dark",
        },
    }

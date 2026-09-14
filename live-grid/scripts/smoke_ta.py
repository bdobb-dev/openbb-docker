"""Smoke-check the TA chart against a running live-grid.

    python scripts/smoke_ta.py http://127.0.0.1:6903
"""

import json
import sys
import urllib.error
import urllib.request


def get(base: str, path: str, **params) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{base}{path}" + (f"?{query}" if query else "")
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read())


def main(base: str) -> int:
    spec = get(base, "/widgets.json")
    if "ta_chart" not in spec:
        print("FAIL: widgets.json has no ta_chart entry")
        return 1
    macro_param = next(p for p in spec["ta_chart"]["params"]
                       if p["paramName"] == "macro")
    macros = [o["value"] for o in macro_param["options"]]
    print(f"macros advertised: {macros}")
    if "classic-momentum" not in macros:
        print("FAIL: classic-momentum macro not discovered")
        return 1

    # A degraded /ta_chart answers 502 with a titled figure -- exactly the case
    # this script exists to catch -- and urlopen raises HTTPError on it, so
    # without this the script prints a traceback instead of its FAIL line.
    try:
        figure = get(base, "/ta_chart", symbol="AAPL", macro="classic-momentum")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        print(f"FAIL: /ta_chart returned {exc.code}: {body}")
        return 1
    traces = figure.get("data", [])
    axes = sorted(k for k in figure.get("layout", {}) if k.startswith("yaxis"))
    print(f"traces: {len(traces)}  panes: {len(axes)}  axes: {axes}")
    if not traces or traces[0].get("type") != "candlestick":
        print("FAIL: first trace is not a candlestick")
        return 1
    if len(axes) != 4:
        print(f"FAIL: expected 4 panes from classic-momentum, got {len(axes)}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6903"))

#!/usr/bin/env python3
"""Smoke-test every widget we can reach with the keys we have.

    OPENBB_URL=https://openbb.<your-tailnet>.ts.net python3 scripts/widget_smoke/run_smoke.py

Fills in AAPL for equity/etf/derivatives/index-style symbols, USDJPY for
currency, BTC for crypto, and a 1-week start_date/end_date range -- then GETs
each testable widget's endpoint and records the outcome to
scripts/widget_smoke/output/results.csv so failures can be triaged into
better default parameters over time.
"""
import csv
import datetime
import json
import os
import shutil
import time

from _common import base_url, fetch_widgets, load_credentials, make_session, testability

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

TODAY = datetime.date.today()
WEEK_AGO = TODAY - datetime.timedelta(days=7)


def symbol_for(widget):
    category = widget.get("category")
    if category == "Crypto":
        return "BTC"
    if category == "Currency":
        return "USDJPY"
    return "AAPL"


def build_params(widget):
    """(params: dict, unresolved: list[str]) for one widget's request."""
    params = {}
    unresolved = []
    for p in widget["params"]:
        name = p["paramName"]
        if name == "symbol":
            params[name] = symbol_for(widget)
        elif name == "start_date":
            params[name] = WEEK_AGO.isoformat()
        elif name == "end_date":
            params[name] = TODAY.isoformat()
        elif p.get("type") == "date":
            params[name] = TODAY.isoformat()
        elif p.get("value") is not None:
            value = p["value"]
            params[name] = str(value).lower() if isinstance(value, bool) else value
        elif not p.get("optional", True):
            unresolved.append(name)
    return params, unresolved


def run_one(session, url, widget_id, widget):
    params, unresolved = build_params(widget)
    if unresolved:
        return {
            "widgetId": widget_id,
            "endpoint": widget.get("endpoint"),
            "params_sent": "",
            "status_code": "",
            "ok": "N",
            "error_message": f"skipped: unhandled required param(s): {', '.join(unresolved)}",
            "elapsed_ms": "",
        }

    started = time.monotonic()
    try:
        resp = session.get(f"{url}{widget['endpoint']}", params=params, timeout=30)
        elapsed_ms = round((time.monotonic() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 - network errors are the data we're collecting
        elapsed_ms = round((time.monotonic() - started) * 1000)
        return {
            "widgetId": widget_id,
            "endpoint": widget.get("endpoint"),
            "params_sent": json.dumps(params),
            "status_code": "",
            "ok": "N",
            "error_message": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": elapsed_ms,
        }

    error_message = ""
    if not resp.ok:
        try:
            body = resp.json()
            error_message = str(body.get("detail", body))
        except ValueError:
            error_message = resp.text
        error_message = error_message[:500]

    return {
        "widgetId": widget_id,
        "endpoint": widget.get("endpoint"),
        "params_sent": json.dumps(params),
        "status_code": resp.status_code,
        "ok": "Y" if resp.ok else "N",
        "error_message": error_message,
        "elapsed_ms": elapsed_ms,
    }


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    url = base_url()
    session = make_session()
    widgets = fetch_widgets(session)
    credentials = load_credentials()

    results = []
    testable_widgets = {
        wid: w for wid, w in widgets.items() if testability(w, credentials)[0]
    }
    for i, (widget_id, widget) in enumerate(testable_widgets.items(), 1):
        print(f"[{i}/{len(testable_widgets)}] {widget_id}")
        results.append(run_one(session, url, widget_id, widget))

    out_path = os.path.join(OUTPUT_DIR, "results.csv")
    if os.path.exists(out_path):
        history_dir = os.path.join(OUTPUT_DIR, "history")
        os.makedirs(history_dir, exist_ok=True)
        stamp = datetime.datetime.fromtimestamp(os.path.getmtime(out_path)).strftime("%Y%m%dT%H%M%S")
        shutil.copy2(out_path, os.path.join(history_dir, f"results-{stamp}.csv"))

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)

    ok_count = sum(1 for r in results if r["ok"] == "Y")
    print(f"\n{ok_count}/{len(results)} OK -> {out_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build the widget inventory spreadsheet + an empty params template.

    OPENBB_URL=https://openbb.<your-tailnet>.ts.net python3 scripts/widget_smoke/generate_inventory.py

Writes scripts/widget_smoke/output/widgets.xlsx and widget_params_template.json.
"""
import json
import os

from openpyxl import Workbook

from _common import REPO_ROOT, fetch_widgets, load_credentials, testability, widget_sources

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def build_inventory(widgets, credentials):
    rows = []
    for widget_id, widget in widgets.items():
        testable, skip_reason = testability(widget, credentials)
        required = [p["paramName"] for p in widget["params"] if not p.get("optional", True)]
        rows.append(
            {
                "widgetId": widget_id,
                "name": widget.get("name"),
                "category": widget.get("category"),
                "subCategory": widget.get("subCategory"),
                "endpoint": widget.get("endpoint"),
                "source": ", ".join(widget_sources(widget)),
                "required_params": ", ".join(required),
                "testable": "Y" if testable else "N",
                "skip_reason": skip_reason or "",
            }
        )
    return rows


def write_xlsx(rows, path):
    wb = Workbook()
    ws = wb.active
    ws.title = "widgets"
    headers = list(rows[0].keys()) if rows else []
    ws.append(headers)
    for row in rows:
        ws.append([row[h] for h in headers])
    for col_cells in ws.columns:
        width = max(len(str(cell.value)) for cell in col_cells if cell.value is not None)
        ws.column_dimensions[col_cells[0].column_letter].width = min(width + 2, 60)
    wb.save(path)


def write_params_template(widgets, path):
    template = {
        widget_id: {p["paramName"]: None for p in widget["params"]}
        for widget_id, widget in widgets.items()
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2, sort_keys=True)
        f.write("\n")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    widgets = fetch_widgets()
    credentials = load_credentials()

    rows = build_inventory(widgets, credentials)
    write_xlsx(rows, os.path.join(OUTPUT_DIR, "widgets.xlsx"))
    write_params_template(widgets, os.path.join(OUTPUT_DIR, "widget_params_template.json"))

    testable_count = sum(1 for r in rows if r["testable"] == "Y")
    print(f"{len(rows)} widgets, {testable_count} testable -> {OUTPUT_DIR}")


if __name__ == "__main__":
    main()

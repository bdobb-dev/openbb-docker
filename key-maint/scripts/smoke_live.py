#!/usr/bin/env python3
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Manual smoke: print the /keys table from a running key-maint.

Usage:
  python scripts/smoke_live.py http://localhost:8446 USER PASS --tests
Not run in CI: hits real provider APIs when --tests is given."""
import base64
import json
import sys
import urllib.request


def main() -> int:
    base, user, pw = sys.argv[1], sys.argv[2], sys.argv[3]
    run_tests = "--tests" in sys.argv[4:]
    url = f"{base}/keys?run_tests={'true' if run_tests else 'false'}"
    tok = base64.b64encode(f"{user}:{pw}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {tok}"})
    body = json.load(urllib.request.urlopen(req, timeout=120))
    print(f"tier={body['tier']}")
    for r in body["rows"]:
        test = r.get("test", {})
        print(
            f"{r['provider']:<20} {r['status']:<8} "
            f"{'DEMO' if r['demo'] else '':<5} "
            f"{r.get('value', ''):<36} "
            f"{test.get('result', '')} {test.get('detail', '')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

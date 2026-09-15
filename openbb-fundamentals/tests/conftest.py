# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""A stand-in for SEC, shared by the lookup and router tests. No test touches
the network: sec.urlopen is replaced, and every request it would have made is
recorded."""
import io
import json
from types import SimpleNamespace

import pytest

from openbb_fundamentals import sec

AGENT = "bdobb-tests admin@example.com"

# The two shapes SEC serves, trimmed to what the lookup reads. Real values as
# of 2026-09-15: Apple ends its year on the last Saturday of September.
TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
}
APPLE = sec.SUBMISSIONS_URL.format(cik=320193)
BERKSHIRE = sec.SUBMISSIONS_URL.format(cik=1067983)


@pytest.fixture
def sec_site(monkeypatch):
    """SEC as a dict of URL -> answer. An answer is a JSON-able document, an
    int HTTP status to fail with, bytes to serve raw, or an exception to raise.
    A URL not in `pages` is a 404, as on SEC. The caches start empty."""
    monkeypatch.setenv("SEC_USER_AGENT", AGENT)
    monkeypatch.setattr(sec, "_ciks", None)
    monkeypatch.setattr(sec, "_answers", {})
    site = SimpleNamespace(
        pages={
            sec.TICKERS_URL: TICKERS,
            APPLE: {"name": "Apple Inc.", "fiscalYearEnd": "0926"},
            BERKSHIRE: {"name": "BERKSHIRE HATHAWAY INC", "fiscalYearEnd": "1231"},
        },
        requests=[],
    )

    def urlopen(request, timeout):
        site.requests.append(request)
        answer = site.pages.get(request.full_url, 404)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, int):
            raise sec.HTTPError(request.full_url, answer, "stand-in", {}, None)
        if isinstance(answer, bytes):
            return io.BytesIO(answer)
        return io.BytesIO(json.dumps(answer).encode())

    monkeypatch.setattr(sec, "urlopen", urlopen)
    return site

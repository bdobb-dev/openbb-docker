# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""A company's fiscal year end, from SEC EDGAR.

Pure lookup: no OpenBB, no FastAPI. router.py is a thin wrapper that turns a
`None` from here into a 404 and an `UpstreamError` into a 502, which is what
lets the tests exercise every rule below without booting the API.

Two keyless SEC documents: company_tickers.json maps every ticker SEC knows to
a CIK, and submissions/CIK##########.json carries that filer's `fiscalYearEnd`
as MMDD. SEC covers companies that file with it: US-listed issuers, ADRs and
foreign filers listed in the US included. Anything else is a miss."""
from __future__ import annotations

import calendar
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# Seconds per SEC request. company_tickers.json is about 800 kB.
TIMEOUT = 10

# In-process for the container's lifetime (spec §2). _ciks is SEC's whole
# ticker map, fetched once; _answers holds every answer and every definitive
# miss, keyed by the normalized ticker. A failed fetch stores nothing, so the
# next request asks SEC again.
# ponytail: no lock -- two first requests racing both fetch the map, and the
# second write wins with identical data. Add a lock if SEC ever complains.
# ponytail: _answers is unbounded; a caller spraying made-up symbols grows it
# by one entry each. Cap it with an LRU if that ever matters.
_ciks: dict[str, int] | None = None
_answers: dict[str, str | None] = {}


class UpstreamError(Exception):
    """SEC could not be reached, or answered with neither data nor a 404."""


def _get(url: str, agent: str) -> dict | None:
    """One SEC JSON document, or None when SEC answers 404."""
    request = Request(url, headers={"User-Agent": agent})
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except HTTPError as exc:
        # An unknown CIK is a 404 with an XML body: SEC's way of saying no.
        if exc.code == 404:
            return None
        # 403 is what SEC answers a request its fair-access policy refuses.
        raise UpstreamError(f"SEC answered {exc.code} for {url}") from exc
    except (OSError, ValueError) as exc:
        # OSError: DNS, refused connection, TLS, timeout (URLError is one).
        # ValueError: a body that is not JSON.
        raise UpstreamError(f"SEC unreachable for {url}: {exc}") from exc


def month_name(mmdd: object) -> str | None:
    """SEC's MMDD fiscal year end -> English month name, or None if malformed.

    A day from 01 to 07 counts as the month before. 52/53-week filers end
    their year on a weekday near a month's end, and SEC records the actual
    date: Deere and Broadcom close their October years in early November and
    report "1101".
    ponytail: a filer whose year genuinely ends in a month's first week is
    reported a month early; none is known."""
    if not (isinstance(mmdd, str) and len(mmdd) == 4 and mmdd.isdigit()):
        return None
    month, day = int(mmdd[:2]), int(mmdd[2:])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if day <= 7:
        month = month - 1 or 12
    return calendar.month_name[month]


def _cik(ticker: str, agent: str) -> int | None:
    global _ciks
    if _ciks is None:
        tickers = _get(TICKERS_URL, agent)
        if tickers is None:
            # The map itself missing is SEC failing, not SEC saying no.
            raise UpstreamError(f"SEC answered 404 for {TICKERS_URL}")
        # SEC writes class shares with a dash (BRK-B) and every ticker in
        # upper case; the keys are upper-cased anyway so the match cannot
        # depend on that.
        _ciks = {row["ticker"].upper(): int(row["cik_str"]) for row in tickers.values()}
    return _ciks.get(ticker)


def fiscal_year_end(symbol: str) -> str | None:
    """The English month name a company's fiscal year ends in, or None.

    None when SEC_USER_AGENT is unset (no request is made), when SEC does not
    know the ticker, or when its fiscalYearEnd is missing or malformed. Raises
    UpstreamError when SEC cannot be asked; that is never cached."""
    # SEC's fair-access policy refuses requests without a contact User-Agent
    # ("name email"). Without one, answering "no data" beats asking SEC and
    # being refused with a 403 every time.
    agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not agent:
        return None
    # AAPL.US -> AAPL: the exchange suffix is not part of SEC's ticker.
    ticker = symbol.strip().upper().split(".")[0]
    if ticker in _answers:
        return _answers[ticker]
    answer = None
    cik = _cik(ticker, agent)
    if cik is not None:
        filer = _get(SUBMISSIONS_URL.format(cik=cik), agent)
        if filer is not None:
            answer = month_name(filer.get("fiscalYearEnd"))
    _answers[ticker] = answer
    return answer

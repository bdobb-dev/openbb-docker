# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The lookup, without the API: a stand-in SEC in, a month name out."""
from urllib.error import URLError

import pytest

from openbb_fundamentals import sec
from tests.conftest import AGENT, APPLE, BERKSHIRE


def test_hit_answers_the_month_name_and_identifies_itself(sec_site):
    assert sec.fiscal_year_end("AAPL") == "September"
    assert [r.full_url for r in sec_site.requests] == [sec.TICKERS_URL, APPLE]
    # SEC's fair-access policy: every request carries the contact.
    assert {r.get_header("User-agent") for r in sec_site.requests} == {AGENT}


def test_an_answer_is_cached_and_the_map_is_fetched_once(sec_site):
    sec.fiscal_year_end("AAPL")
    sec.fiscal_year_end("AAPL")
    sec.fiscal_year_end("BRK-B")
    assert [r.full_url for r in sec_site.requests] == [sec.TICKERS_URL, APPLE, BERKSHIRE]


def test_unknown_ticker_is_a_cached_miss(sec_site):
    assert sec.fiscal_year_end("ZZZZ") is None
    assert sec.fiscal_year_end("ZZZZ") is None
    # The map was asked once; no submissions request, and no second ask.
    assert [r.full_url for r in sec_site.requests] == [sec.TICKERS_URL]


def test_a_cik_sec_has_no_submissions_for_is_a_cached_miss(sec_site):
    del sec_site.pages[APPLE]  # the stand-in answers 404, as SEC does
    assert sec.fiscal_year_end("AAPL") is None
    assert sec.fiscal_year_end("AAPL") is None
    assert [r.full_url for r in sec_site.requests] == [sec.TICKERS_URL, APPLE]


@pytest.mark.parametrize("agent", [None, "", "   "])
def test_no_user_agent_is_a_miss_without_a_request(sec_site, monkeypatch, agent):
    if agent is None:
        monkeypatch.delenv("SEC_USER_AGENT")
    else:
        monkeypatch.setenv("SEC_USER_AGENT", agent)
    assert sec.fiscal_year_end("AAPL") is None
    assert sec_site.requests == []


@pytest.mark.parametrize(
    "url,failure",
    [
        (sec.TICKERS_URL, 503),
        (sec.TICKERS_URL, 404),
        (sec.TICKERS_URL, URLError("connection refused")),
        (APPLE, 403),
        (APPLE, TimeoutError("timed out")),
        (APPLE, b"<html>Service Unavailable</html>"),
    ],
)
def test_upstream_failure_raises_and_is_not_cached(sec_site, url, failure):
    good = sec_site.pages[url]
    sec_site.pages[url] = failure
    with pytest.raises(sec.UpstreamError):
        sec.fiscal_year_end("AAPL")
    # SEC recovers; the next request asks again and gets the answer.
    sec_site.pages[url] = good
    assert sec.fiscal_year_end("AAPL") == "September"


@pytest.mark.parametrize("symbol", ["AAPL", "aapl", "AAPL.US", "aapl.us", " AAPL "])
def test_suffix_and_case_do_not_matter(sec_site, symbol):
    assert sec.fiscal_year_end(symbol) == "September"


def test_class_shares_keep_their_dash_and_lose_the_suffix(sec_site):
    assert sec.fiscal_year_end("brk-b.us") == "December"


def test_spellings_of_one_ticker_share_one_cache_entry(sec_site):
    for symbol in ("AAPL", "aapl.us", "AAPL.US"):
        sec.fiscal_year_end(symbol)
    assert [r.full_url for r in sec_site.requests] == [sec.TICKERS_URL, APPLE]


@pytest.mark.parametrize("filer", [{}, {"fiscalYearEnd": None}, {"fiscalYearEnd": ""}, {"fiscalYearEnd": "13XX"}])
def test_unusable_fiscal_year_end_is_a_cached_miss(sec_site, filer):
    sec_site.pages[APPLE] = filer
    assert sec.fiscal_year_end("AAPL") is None
    assert sec.fiscal_year_end("AAPL") is None
    assert [r.full_url for r in sec_site.requests] == [sec.TICKERS_URL, APPLE]


@pytest.mark.parametrize(
    "mmdd,month",
    [
        ("1231", "December"),
        ("0926", "September"),  # Apple
        ("0131", "January"),    # Walmart
        ("0630", "June"),
        ("0108", "January"),    # day 8 is the month itself
        ("1101", "October"),    # Deere, Broadcom: a 52/53-week October year
        ("0103", "December"),   # rolls back across the year
    ],
)
def test_mmdd_to_month(mmdd, month):
    assert sec.month_name(mmdd) == month


@pytest.mark.parametrize("mmdd", [None, "", "12", "12310", "1331", "0015", "1200", "1232", "12-3", "abcd", 1231])
def test_malformed_mmdd_has_no_month(mmdd):
    assert sec.month_name(mmdd) is None

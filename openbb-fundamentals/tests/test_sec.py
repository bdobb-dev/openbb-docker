# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The lookup, without the API: a stand-in SEC in, a month name out."""
import threading
import time
from http.client import IncompleteRead
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
        # http.client.HTTPException subclasses are not OSError: a broken
        # response mid-read must still become UpstreamError, not an
        # unhandled 500.
        (APPLE, IncompleteRead(b"partial")),
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


def test_malformed_ticker_map_row_raises_and_is_not_cached(sec_site):
    # A row missing cik_str breaks the dict comprehension with a KeyError;
    # that's SEC serving us something broken, not a definitive answer, so
    # the map must not be cached from it.
    good = sec_site.pages[sec.TICKERS_URL]
    sec_site.pages[sec.TICKERS_URL] = {"0": {"ticker": "AAPL", "title": "Apple Inc."}}
    with pytest.raises(sec.UpstreamError):
        sec.fiscal_year_end("AAPL")
    assert sec._ciks is None
    # SEC recovers; the next request fetches the map again and succeeds.
    sec_site.pages[sec.TICKERS_URL] = good
    assert sec.fiscal_year_end("AAPL") == "September"
    assert [r.full_url for r in sec_site.requests] == [sec.TICKERS_URL, sec.TICKERS_URL, APPLE]


@pytest.mark.parametrize("symbol", ["AAPL", "aapl", "AAPL.US", "aapl.us", " AAPL "])
def test_suffix_and_case_do_not_matter(sec_site, symbol):
    assert sec.fiscal_year_end(symbol) == "September"


def test_class_shares_keep_their_dash_and_lose_the_suffix(sec_site):
    assert sec.fiscal_year_end("brk-b.us") == "December"


@pytest.mark.parametrize("symbol", ["TSCO.LSE", "SHEL.L", "BRK.B", "VOW3.XETRA", "7203.T"])
def test_any_suffix_but_dot_us_is_a_miss_with_no_sec_request(sec_site, symbol):
    # TSCO.LSE is Tesco; the stand-in's map lists an unrelated US company,
    # Tractor Supply, under the bare ticker TSCO. Answering from that root
    # would be a confidently wrong month, so the suffix is a miss instead --
    # and it doesn't even cost a request, unlike a miss SEC has to answer.
    assert sec.fiscal_year_end(symbol) is None
    assert sec_site.requests == []


def test_non_us_suffix_misses_are_cached(sec_site):
    assert sec.fiscal_year_end("TSCO.LSE") is None
    assert sec.fiscal_year_end("TSCO.LSE") is None
    assert sec_site.requests == []


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


def test_concurrent_first_requests_fetch_the_map_once(sec_site, monkeypatch):
    # Two callers racing to be first both see `_ciks is None` before either
    # reaches the lock; _lock must let only one of them actually fetch
    # company_tickers.json.
    real_urlopen = sec.urlopen
    map_fetch_started = threading.Event()
    release_map_fetch = threading.Event()

    def slow_urlopen(request, timeout):
        if request.full_url == sec.TICKERS_URL:
            map_fetch_started.set()
            release_map_fetch.wait(timeout=5)
        return real_urlopen(request, timeout)

    monkeypatch.setattr(sec, "urlopen", slow_urlopen)

    results = {}

    def ask(symbol):
        results[symbol] = sec.fiscal_year_end(symbol)

    first = threading.Thread(target=ask, args=("AAPL",))
    second = threading.Thread(target=ask, args=("BRK-B",))
    first.start()
    assert map_fetch_started.wait(timeout=5)  # first thread is mid-fetch, holding the lock
    second.start()
    time.sleep(0.05)  # let the second thread reach and block on the lock
    release_map_fetch.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert results == {"AAPL": "September", "BRK-B": "December"}
    map_requests = [r for r in sec_site.requests if r.full_url == sec.TICKERS_URL]
    assert len(map_requests) == 1


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

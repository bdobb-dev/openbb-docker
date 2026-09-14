"""EODHD macro indicators (/macro-indicator/{ALPHA3}) — the Phase-4 macro
cluster: EconomicIndicators, GdpReal, GdpNominal, ConsumerPriceIndex,
Unemployment, CountryProfile.

Every row is World-Bank-shaped and **annual**: {CountryCode, CountryName,
Indicator, Date, Period, Value}. `frequency` params are accepted for
interface parity but the answer is always the annual series. Countries are
resolved to Alpha-3 ISO (the API's requirement) from OpenBB's snake-case
names ('united_states'), alpha-2, or alpha-3; unknown values pass through
upper-cased so EODHD's own error surfaces.

Semantics worth knowing (recorded live 2026-09-01):
- GdpReal maps to `gdp_growth_annual` — an annual **growth percent**, not a
  level series like oecd's.
- GdpNominal maps to `gdp_current_usd` — level, current US$.
- CPI supports transform="index" (`consumer_price_index`, 2010=100) and
  transform="yoy" (`inflation_consumer_prices_annual`).
"""

from typing import Any

from openbb_core.app.model.abstract.error import OpenBBError
from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.consumer_price_index import (
    ConsumerPriceIndexData, ConsumerPriceIndexQueryParams,
)
from openbb_core.provider.standard_models.country_profile import (
    CountryProfileData, CountryProfileQueryParams,
)
from openbb_core.provider.standard_models.economic_indicators import (
    EconomicIndicatorsData, EconomicIndicatorsQueryParams,
)
from openbb_core.provider.standard_models.gdp_nominal import (
    GdpNominalData, GdpNominalQueryParams,
)
from openbb_core.provider.standard_models.gdp_real import (
    GdpRealData, GdpRealQueryParams,
)
from openbb_core.provider.standard_models.unemployment import (
    UnemploymentData, UnemploymentQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError, UnauthorizedError
from pydantic import Field

from openbb_eodhd.models._client import sdk_call

# OpenBB snake-case names and alpha-2 codes for the majors → Alpha-3.
# Anything not here passes through upper-cased (3-letter codes work as-is).
COUNTRY_MAP = {
    "united_states": "USA", "US": "USA",
    "united_kingdom": "GBR", "GB": "GBR", "UK": "GBR",
    "germany": "DEU", "DE": "DEU",
    "france": "FRA", "FR": "FRA",
    "italy": "ITA", "IT": "ITA",
    "spain": "ESP", "ES": "ESP",
    "japan": "JPN", "JP": "JPN",
    "china": "CHN", "CN": "CHN",
    "india": "IND", "IN": "IND",
    "canada": "CAN", "CA": "CAN",
    "mexico": "MEX", "MX": "MEX",
    "brazil": "BRA", "BR": "BRA",
    "australia": "AUS", "AU": "AUS",
    "south_korea": "KOR", "korea": "KOR", "KR": "KOR",
    "switzerland": "CHE", "CH": "CHE",
    "netherlands": "NLD", "NL": "NLD",
    "sweden": "SWE", "SE": "SWE",
    "norway": "NOR", "NO": "NOR",
    "russia": "RUS", "RU": "RUS",
    "turkey": "TUR", "TR": "TUR",
    "south_africa": "ZAF", "ZA": "ZAF",
    "saudi_arabia": "SAU", "SA": "SAU",
    "indonesia": "IDN", "ID": "IDN",
    "argentina": "ARG", "AR": "ARG",
    "new_zealand": "NZL", "NZ": "NZL",
    "ireland": "IRL", "IE": "IRL",
    "singapore": "SGP", "SG": "SGP",
    "hong_kong": "HKG", "HK": "HKG",
    "taiwan": "TWN", "TW": "TWN",
    "poland": "POL", "PL": "POL",
    "greece": "GRC", "GR": "GRC",
    "portugal": "PRT", "PT": "PRT",
    "austria": "AUT", "AT": "AUT",
    "belgium": "BEL", "BE": "BEL",
    "denmark": "DNK", "DK": "DNK",
    "finland": "FIN", "FI": "FIN",
}


def resolve_country(raw: str) -> str:
    key = raw.strip()
    return COUNTRY_MAP.get(key.lower()) or COUNTRY_MAP.get(key.upper()) or key.upper()


def _date(v):
    from pandas import isna, to_datetime
    if not v:
        return None
    ts = to_datetime(v, errors="coerce")
    return None if isna(ts) else ts.date()


def _series_sync(client, country: str, indicator: str) -> list[dict]:
    """One /macro-indicator call, error-shaped responses surfaced."""
    resp = client.get_macro_indicators_data(country=country, indicator=indicator)
    if isinstance(resp, dict):
        raise UnauthorizedError(
            f"EODHD (macro {indicator}/{country}): "
            f"{resp.get('message') or resp.get('errors') or resp}"
        )
    return [r for r in resp or [] if r.get("Value") is not None and r.get("Date")]


def _windowed(rows: list[dict], start, end) -> list[dict]:
    s = str(start) if start else None
    e = str(end) if end else None
    out = [r for r in rows if (not s or r["Date"] >= s) and (not e or r["Date"] <= e)]
    return sorted(out, key=lambda r: r["Date"])


def _single_series(query, credentials, indicator: str, context: str) -> Any:
    """aextract body shared by the four single-indicator fetchers."""
    from asyncio import to_thread

    country = resolve_country(query.country)

    def _sync():
        rows = sdk_call(
            credentials, lambda c: _series_sync(c, country, indicator), context
        )
        rows = _windowed(rows, query.start_date, query.end_date)
        if not rows:
            raise EmptyDataError(f"EODHD has no {context} data for '{country}'.")
        return rows

    return to_thread(_sync)


# ============================================================
# EconomicIndicators — any indicator slug, any countries
# ============================================================

class EODHDEconomicIndicatorsQueryParams(EconomicIndicatorsQueryParams):
    """EODHD Economic Indicators Query.

    `symbol` is an EODHD indicator slug (e.g. gdp_current_usd,
    inflation_consumer_prices_annual, population_total); `country` one or
    more countries, comma-joined. Both multi — one call per pair. The full
    slug list: https://eodhd.com/financial-apis/macroeconomics-data-and-macro-indicators-api
    """

    __json_schema_extra__ = {
        "symbol": {"multiple_items_allowed": True},
        "country": {"multiple_items_allowed": True},
    }


class EODHDEconomicIndicatorsData(EconomicIndicatorsData):
    """EODHD Economic Indicators Data."""

    indicator_name: str | None = Field(
        default=None, description="Human-readable indicator name from EODHD."
    )


class EODHDEconomicIndicatorsFetcher(
    Fetcher[EODHDEconomicIndicatorsQueryParams, list[EODHDEconomicIndicatorsData]]
):
    """EODHD macro indicators, generic."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEconomicIndicatorsQueryParams:
        return EODHDEconomicIndicatorsQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        indicators = [s.strip() for s in query.symbol.split(",") if s.strip()]
        countries = [
            resolve_country(c) for c in (query.country or "united_states").split(",") if c.strip()
        ]

        def _sync():
            def _all(client):
                out = []
                for country in countries:
                    for ind in indicators:
                        for r in _series_sync(client, country, ind):
                            r["_indicator"] = ind
                            out.append(r)
                return out

            rows = sdk_call(credentials, _all, "economic indicators")
            rows = _windowed(rows, query.start_date, query.end_date)
            if not rows:
                raise EmptyDataError("EODHD returned no indicator data.")
            return rows

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDEconomicIndicatorsData]:  # pylint: disable=unused-argument
        return [
            EODHDEconomicIndicatorsData.model_validate({
                "date": _date(r["Date"]),
                "symbol": r.get("_indicator"),
                "country": r.get("CountryName") or r.get("CountryCode"),
                "value": r.get("Value"),
                "indicator_name": r.get("Indicator"),
            })
            for r in data
        ]


# ============================================================
# GdpReal / GdpNominal
# ============================================================

class EODHDGdpRealQueryParams(GdpRealQueryParams):
    """EODHD Real GDP Query — annual real growth percent."""
    country: str = Field(default="united_states", description="Country name, alpha-2 or alpha-3 code.")


class EODHDGdpRealData(GdpRealData):
    """EODHD Real GDP Data — `value` is annual real GDP growth, percent."""


class EODHDGdpRealFetcher(Fetcher[EODHDGdpRealQueryParams, list[EODHDGdpRealData]]):
    """EODHD real GDP growth (gdp_growth_annual)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDGdpRealQueryParams:
        return EODHDGdpRealQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        return await _single_series(query, credentials, "gdp_growth_annual", "real GDP growth")

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDGdpRealData]:  # pylint: disable=unused-argument
        return [
            EODHDGdpRealData.model_validate({
                "date": _date(r["Date"]),
                "country": r.get("CountryName") or r.get("CountryCode"),
                "value": r["Value"],
            })
            for r in data
        ]


class EODHDGdpNominalQueryParams(GdpNominalQueryParams):
    """EODHD Nominal GDP Query — level, current US$."""
    country: str = Field(default="united_states", description="Country name, alpha-2 or alpha-3 code.")


class EODHDGdpNominalData(GdpNominalData):
    """EODHD Nominal GDP Data — `value` is GDP level in current US$."""


class EODHDGdpNominalFetcher(Fetcher[EODHDGdpNominalQueryParams, list[EODHDGdpNominalData]]):
    """EODHD nominal GDP (gdp_current_usd)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDGdpNominalQueryParams:
        return EODHDGdpNominalQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        return await _single_series(query, credentials, "gdp_current_usd", "nominal GDP")

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDGdpNominalData]:  # pylint: disable=unused-argument
        return [
            EODHDGdpNominalData.model_validate({
                "date": _date(r["Date"]),
                "country": r.get("CountryName") or r.get("CountryCode"),
                "value": r["Value"],
            })
            for r in data
        ]


# ============================================================
# ConsumerPriceIndex
# ============================================================

class EODHDConsumerPriceIndexQueryParams(ConsumerPriceIndexQueryParams):
    """EODHD CPI Query.

    transform="yoy" → annual inflation percent; transform="index" → the CPI
    index level (2010 = 100). Other transforms are not available. The series
    is annual regardless of `frequency`; `harmonized` is ignored.
    """


class EODHDConsumerPriceIndexData(ConsumerPriceIndexData):
    """EODHD CPI Data."""


class EODHDConsumerPriceIndexFetcher(
    Fetcher[EODHDConsumerPriceIndexQueryParams, list[EODHDConsumerPriceIndexData]]
):
    """EODHD consumer prices — annual index level or yoy inflation."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDConsumerPriceIndexQueryParams:
        return EODHDConsumerPriceIndexQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        indicator = {
            "yoy": "inflation_consumer_prices_annual",
            "index": "consumer_price_index",
        }.get(query.transform)
        if indicator is None:
            raise OpenBBError(
                f"EODHD supports transform 'yoy' or 'index', not '{query.transform}'."
            )
        return await _single_series(query, credentials, indicator, "CPI")

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDConsumerPriceIndexData]:  # pylint: disable=unused-argument
        return [
            EODHDConsumerPriceIndexData.model_validate({
                "date": _date(r["Date"]),
                "country": r.get("CountryName") or r.get("CountryCode"),
                "value": r["Value"],
            })
            for r in data
        ]


# ============================================================
# Unemployment
# ============================================================

class EODHDUnemploymentQueryParams(UnemploymentQueryParams):
    """EODHD Unemployment Query — annual national-estimate series;
    `frequency` accepted for parity, the answer is always annual."""


class EODHDUnemploymentData(UnemploymentData):
    """EODHD Unemployment Data — percent of total labor force."""


class EODHDUnemploymentFetcher(
    Fetcher[EODHDUnemploymentQueryParams, list[EODHDUnemploymentData]]
):
    """EODHD unemployment (unemployment_total_percent)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDUnemploymentQueryParams:
        return EODHDUnemploymentQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        return await _single_series(
            query, credentials, "unemployment_total_percent", "unemployment"
        )

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDUnemploymentData]:  # pylint: disable=unused-argument
        return [
            EODHDUnemploymentData.model_validate({
                "date": _date(r["Date"]),
                "country": r.get("CountryName") or r.get("CountryCode"),
                "value": r["Value"],
            })
            for r in data
        ]


# ============================================================
# CountryProfile — assembled, six calls per country
# ============================================================

# CountryProfile field ← EODHD indicator. Six calls per country per request
# (macro moves yearly; the endpoint has no bundle form).
PROFILE_INDICATORS = {
    "population": "population_total",
    "gdp_usd": "gdp_current_usd",
    "gdp_yoy": "gdp_growth_annual",
    "cpi_yoy": "inflation_consumer_prices_annual",
    "jobless_rate": "unemployment_total_percent",
    "govt_debt_gdp": "debt_percent_gdp",
}


class EODHDCountryProfileQueryParams(CountryProfileQueryParams):
    """EODHD Country Profile Query — one or more countries, comma-joined."""

    __json_schema_extra__ = {"country": {"multiple_items_allowed": True}}


class EODHDCountryProfileData(CountryProfileData):
    """EODHD Country Profile Data.

    Latest annual value per field; the World-Bank set has no qoq/core/
    retail/industrial/policy-rate/10y/current-account figures — those stay
    null.
    """


class EODHDCountryProfileFetcher(
    Fetcher[EODHDCountryProfileQueryParams, list[EODHDCountryProfileData]]
):
    """EODHD country profile, assembled from six indicator series."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDCountryProfileQueryParams:
        return EODHDCountryProfileQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        countries = [resolve_country(c) for c in query.country.split(",") if c.strip()]

        def _sync():
            def _all(client):
                out = []
                for country in countries:
                    profile: dict = {"country_code": country}
                    for field, indicator in PROFILE_INDICATORS.items():
                        try:
                            rows = _series_sync(client, country, indicator)
                        except UnauthorizedError:
                            raise
                        except Exception:  # noqa: BLE001 — one gap never kills the profile
                            rows = []
                        if rows:
                            latest = max(rows, key=lambda r: r["Date"])
                            profile[field] = latest["Value"]
                            profile.setdefault("country_name", latest.get("CountryName"))
                    out.append(profile)
                return out

            profiles = [p for p in sdk_call(credentials, _all, "country profile") if len(p) > 1]
            if not profiles:
                raise EmptyDataError("EODHD returned no profile data.")
            return profiles

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDCountryProfileData]:  # pylint: disable=unused-argument
        rows = []
        for p in data:
            population = p.get("population")
            rows.append(EODHDCountryProfileData.model_validate({
                "country": p.get("country_name") or p.get("country_code"),
                "population": int(population) if population is not None else None,
                "gdp_usd": p.get("gdp_usd"),
                "gdp_yoy": p.get("gdp_yoy"),
                "cpi_yoy": p.get("cpi_yoy"),
                "jobless_rate": p.get("jobless_rate"),
                "govt_debt_gdp": p.get("govt_debt_gdp"),
            }))
        return rows

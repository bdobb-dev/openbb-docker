# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""A static three-level index of EODHD's economic-calendar event names.

EODHD's /economic-events endpoint sends only a bare event name in its `type`
field (e.g. "Core PPI", "Philly Fed Prices Paid") -- it never says who
publishes the statistic or which release it belongs to. The OpenBB standard
model's EconomicCalendarData carries both: `source` (who publishes it, e.g.
"BLS", "Federal Reserve", "Treasury") and `category` (which release it comes
from, e.g. "Producer Prices", "Philly Fed", "TIC"). This module supplies the
missing two levels so the calendar fetcher can fill them in.

The three levels, concretely:

    Source (level 1)   -- the publishing body: "BLS", "Federal Reserve", ...
      Release (level 2) -- the named release or survey: "Producer Prices",
                            "Philly Fed", "FOMC", ...
        Statistic (level 3) -- EODHD's own event name, verbatim, the only
                                thing the API actually sends: "Core PPI",
                                "Philly Fed Prices Paid", ...

TAXONOMY holds that tree exactly as curated (source -> release -> [event
names]). Every event name appears exactly once across the whole tree, so a
name determines its (source, release) pair uniquely -- EVENT_INDEX inverts
the tree into that lookup, built once at import. classify() is the only
entry point callers need; unrecognized or missing names resolve to
(None, None) rather than raising, since EODHD adds event names over time and
a new one showing up should not break the fetcher.
"""

TAXONOMY: dict[str, dict[str, list[str]]] = {
    "Federal Reserve": {
        "FOMC": [
            "Fed Interest Rate Decision",
            "FOMC Economic Projections",
            "FOMC Minutes",
            "Fed Press Conference",
            "Interest Rate Projection - Current",
            "Interest Rate Projection - 1st Yr",
            "Interest Rate Projection - 2nd Yr",
            "Interest Rate Projection - 3rd Yr",
            "Interest Rate Projection - Longer",
        ],
        "Beige Book": [
            "Beige Book",
            "Fed Beige Book",
        ],
        "Speeches": [
            "Fed Barkin Speech",
            "Fed Barr Speech",
            "Fed Hammack Speech",
            "Fed Waller Speech",
        ],
        "Industrial Production (G.17)": [
            "Industrial Production",
            "Manufacturing Production",
            "Capacity Utilization",
        ],
        "Consumer Credit (G.19)": [
            "Consumer Credit Change",
        ],
        "Money Stock (H.6)": [
            "Money Supply",
        ],
        "Balance Sheet (H.4.1)": [
            "Central Bank Balance Sheet",
        ],
        "New York Fed": [
            "NY Empire State Manufacturing Index",
            "Consumer Inflation Expectation",
            "Total Household Debt",
        ],
        "Philly Fed": [
            "Philadelphia Fed Manufacturing Index",
            "Philly Fed Employment",
            "Philly Fed Prices Paid",
            "Philly Fed New Orders",
            "Philly Fed Business Conditions",
            "Philly Fed CAPEX Index",
        ],
        "Richmond Fed": [
            "Richmond Fed Manufacturing Index",
            "Richmond Fed Manufacturing Shipments Index",
            "Richmond Fed Services Index",
        ],
        "Kansas City Fed": [
            "Kansas Fed Manufacturing Index",
            "Kansas Fed Composite Index",
        ],
        "Dallas Fed": [
            "Dallas Fed Manufacturing Index",
            "Dallas Fed Services Index",
            "Dallas Fed Services Revenues Index",
        ],
        "Chicago Fed": [
            "Chicago Fed National Activity Index",
        ],
        "Cleveland Fed": [
            "Cleveland CPI",
        ],
        "Atlanta Fed": [
            "Atlanta Fed GDPNow",
        ],
    },
    "BLS": {
        "Consumer Prices": [
            "CPI",
            "CPI s.a",
            "CPI n.s.a",
            "Core CPI",
            "Inflation Rate",
            "Core Inflation Rate",
            "Real Earnings",
        ],
        "Producer Prices": [
            "Producer Price Index",
            "Core PPI",
            "PPI Ex Food, Energy and Trade",
        ],
        "Employment Situation": [
            "Non Farm Payrolls",
            "Nonfarm Payrolls Private",
            "Manufacturing Payrolls",
            "Government Payrolls",
            "Unemployment Rate",
            "U-6 Unemployment Rate",
            "Participation Rate",
            "Average Hourly Earnings",
            "Average Weekly Hours",
        ],
        "JOLTS": [
            "JOLTs Job Openings",
            "JOLTs Job Quits",
        ],
        "Import and Export Prices": [
            "Import Prices",
            "Export Prices",
        ],
        "Productivity and Costs": [
            "Nonfarm Productivity",
            "Unit Labour Costs",
        ],
        "Employment Cost Index": [
            "Employment Cost Index",
            "Employment Cost - Wages",
            "Employment Cost - Benefits",
        ],
    },
    "BEA": {
        "GDP": [
            "GDP Growth Rate",
            "GDP Price Index",
            "GDP Sales",
            "Real Consumer Spending",
            "Corporate Profits",
            "PCE Prices",
            "Core PCE Prices",
        ],
        "Personal Income and Outlays": [
            "Personal Income",
            "Personal Spending",
            "PCE Price Index",
            "Core PCE Price Index",
        ],
        "International Trade": [
            "Balance of Trade",
            "Exports",
            "Imports",
        ],
        "Current Account": [
            "Current Account",
        ],
        "Vehicle Sales": [
            "Total Vehicle Sales",
            "All Car Sales",
            "All Truck Sales",
        ],
    },
    "Census Bureau": {
        "Retail Sales": [
            "Retail Sales",
            "Retail Sales Ex Autos",
            "Retail Sales Ex Gas/Autos",
        ],
        "Durable Goods": [
            "Durable Goods Orders",
            "Durable Goods Orders Ex Defense",
            "Durable Goods Orders Ex Transp",
            "Non Defense Goods Orders Ex Air",
        ],
        "Factory Orders": [
            "Factory Orders",
            "Factory Orders ex Transportation",
        ],
        "New Residential Construction": [
            "Housing Starts",
            "Building Permits",
        ],
        "New Home Sales": [
            "New Home Sales",
        ],
        "Construction Spending": [
            "Construction Spending",
        ],
        "Business Inventories": [
            "Business Inventories",
        ],
        "Advance Indicators": [
            "Goods Trade Balance",
            "Wholesale Inventories",
            "Retail Inventories Ex Autos",
        ],
        "Wholesale Trade": [
            "Wholesale Sales",
        ],
    },
    "Treasury": {
        "Auctions": [
            "4-Week Bill Auction",
            "8-Week Bill Auction",
            "17-Week Bill Auction",
            "6-Month Bill Auction",
            "3-Month Bill Auction",
            "30-Year Bond Auction",
            "10-Year Note Auction",
            "3-Year Note Auction",
            "52-Week Bill Auction",
            "7-Year Note Auction",
            "5-Year Note Auction",
            "2-Year Note Auction",
            "10-Year TIPS Auction",
            "20-Year Bond Auction",
            "5-Year TIPS Auction",
        ],
        "Monthly Treasury Statement": [
            "Budget Balance",
        ],
        "TIC": [
            "Net Long-Term TIC Flows",
            "Overall Net Capital Flows",
            "Foreign Bond Investment",
        ],
        "Refunding": [
            "Treasury Refunding Announcement",
        ],
    },
    "Department of Labor": {
        "Weekly Claims": [
            "Initial Jobless Claims",
            "Continuing Jobless Claims",
            "Jobless Claims 4-Week Average",
        ],
    },
    "EIA": {
        "Weekly Petroleum Status": [
            "EIA Crude Oil Stocks Change",
            "EIA Cushing Crude Oil Stocks Change",
            "EIA Crude Oil Imports Change",
            "EIA Refinery Crude Runs Change",
            "EIA Weekly Refinery Utilization Rates WoW",
            "EIA Gasoline Stocks Change",
            "EIA Gasoline Production Change",
            "EIA Distillate Stocks Change",
            "EIA Distillate Fuel Production Change",
            "EIA Heating Oil Stocks Change",
            "Crude Oil Imports",
        ],
        "Natural Gas Storage": [
            "EIA Natural Gas Stocks Change",
        ],
    },
    "API": {
        "Weekly Statistical Bulletin": [
            "API Crude Oil Stock Change",
        ],
    },
    "Baker Hughes": {
        "Rig Count": [
            "Baker Hughes Oil Rig Count",
        ],
    },
    "CFTC": {
        "Commitments of Traders": [
            "CFTC Silver Speculative net positions",
            "CFTC Natural Gas speculative net positions",
            "CFTC Corn speculative net positions",
            "CFTC Gold Speculative net positions",
            "CFTC Wheat speculative net positions",
            "CFTC Crude Oil speculative net positions",
            "CFTC Soybeans speculative net positions",
            "CFTC Copper Speculative net positions",
            "CFTC Nasdaq 100 speculative net positions",
            "CFTC Aluminium Speculative net positions",
            "CFTC S&P 500 speculative net positions",
        ],
    },
    "USDA": {
        "WASDE": [
            "WASDE Report",
        ],
        "Grain Stocks": [
            "Quarterly Grain Stocks - Corn",
            "Quarterly Grain Stocks - Soy",
            "Quarterly Grain Stocks - Wheat",
        ],
    },
    "ISM": {
        "Manufacturing PMI": [
            "ISM Manufacturing PMI",
            "ISM Manufacturing Employment",
            "ISM Manufacturing New Orders",
            "ISM Manufacturing Prices",
        ],
        "Services PMI": [
            "ISM Services PMI",
            "ISM Services Business Activity",
            "ISM Services Employment",
            "ISM Services New Orders",
            "ISM Services Prices",
        ],
        "Chicago Business Barometer": [
            "Chicago PMI",
        ],
    },
    "S&P Global": {
        "PMI": [
            "S&P Global Manufacturing PMI",
            "S&P Global Services PMI",
            "S&P Global Composite PMI",
        ],
        "Case-Shiller": [
            "S&P/Case-Shiller Home Price",
        ],
    },
    "Conference Board": {
        "Consumer Confidence": [
            "CB Consumer Confidence",
        ],
        "Employment Trends": [
            "CB Employment Trends Index",
        ],
        "Leading Indicators": [
            "Leading Index",
        ],
    },
    "University of Michigan": {
        "Consumer Sentiment": [
            "Michigan Consumer Sentiment",
            "Michigan Current Conditions",
            "Michigan Consumer Expectations",
            "Michigan Inflation Expectations",
            "Michigan 1 Year Inflation Expectations",
            "Michigan 5 Year Inflation Expectations",
        ],
    },
    "NAR": {
        "Existing Home Sales": [
            "Existing Home Sales",
        ],
        "Pending Home Sales": [
            "Pending Home Sales",
        ],
    },
    "NAHB": {
        "Housing Market Index": [
            "NAHB Housing Market Index",
        ],
    },
    "FHFA": {
        "House Price Index": [
            "House Price Index",
        ],
    },
    "MBA": {
        "Weekly Applications": [
            "MBA Mortgage Applications",
            "MBA Mortgage Market Index",
            "MBA Purchase Index",
            "MBA Mortgage Refinance Index",
            "MBA 30-Year Mortgage Rate",
        ],
    },
    "Freddie Mac": {
        "Primary Mortgage Market Survey": [
            "30-Year Mortgage Rate",
            "15-Year Mortgage Rate",
        ],
    },
    "ADP": {
        "National Employment Report": [
            "ADP Employment Change",
        ],
    },
    "Challenger": {
        "Job Cuts": [
            "Challenger Job Cuts",
        ],
    },
    "NFIB": {
        "Small Business Optimism": [
            "NFIB Business Optimism Index",
        ],
    },
    "Redbook": {
        "Weekly Retail Sales": [
            "Redbook",
        ],
    },
    "Manheim": {
        "Used Vehicle Value Index": [
            "Used Car Prices",
        ],
    },
    "LMI": {
        "Logistics Managers Index": [
            "LMI Logistics Managers Index",
        ],
    },
    "TIPP": {
        "Economic Optimism": [
            "Economic Optimism Index",
        ],
    },
    "Thomson Reuters / Ipsos": {
        "PCSI": [
            "Thomson Reuters IPSOS PCSI",
        ],
    },
    "OPEC": {
        "Monthly Oil Market Report": [
            "OPEC Monthly Report",
        ],
        "Meetings": [
            "OPEC Meeting",
        ],
    },
    "Holidays": {
        "Market Holidays": [
            "Labour Day",
        ],
    },
}

# Inverted at import: event name -> (source, release). Built once here so
# classify() below is an O(1) lookup instead of walking TAXONOMY per call.
EVENT_INDEX: dict[str, tuple[str, str]] = {
    event: (source, release)
    for source, releases in TAXONOMY.items()
    for release, events in releases.items()
    for event in events
}

# Case-insensitive fallback index, built once alongside EVENT_INDEX.
_EVENT_INDEX_CASEFOLD: dict[str, tuple[str, str]] = {
    event.casefold(): pair for event, pair in EVENT_INDEX.items()
}


def classify(event: str | None) -> tuple[str | None, str | None]:
    """Resolve an EODHD event name to its (source, release) pair.

    Tries an exact match first, then a case-insensitive match. Returns
    (None, None) for anything unrecognized, including None itself.
    """
    if not event:
        return (None, None)
    if event in EVENT_INDEX:
        return EVENT_INDEX[event]
    return _EVENT_INDEX_CASEFOLD.get(event.casefold(), (None, None))

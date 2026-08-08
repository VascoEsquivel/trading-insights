"""The ticker universe the historical study runs over.

Deliberately mixed: mega caps, cyclicals, biotech, energy, banks, and a large
block of names that blew up or bled out after their run (PTON, ZM, BYND, LCID,
HOOD-era meme stocks, SPAC-era EV names). A universe of only current winners
would report flattering base rates for every setup.

This does NOT solve survivorship bias. yfinance serves the tickers that still
exist, so companies that delisted or went to zero are missing entirely, and
their absence makes every hit rate here optimistic. The UI says so.
"""
from __future__ import annotations

MEGA_TECH = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AVGO", "TSLA", "ORCL",
    "CRM", "ADBE", "AMD", "INTC", "QCOM", "TXN", "MU", "AMAT", "LRCX", "KLAC",
    "NOW", "INTU", "PANW", "SNPS", "CDNS", "ANET", "MRVL", "NXPI", "ON",
]

SOFTWARE_GROWTH = [
    "PLTR", "SNOW", "DDOG", "NET", "CRWD", "ZS", "OKTA", "MDB", "TEAM", "TWLO",
    "SHOP", "SQ", "PYPL", "ROKU", "SPOT", "UBER", "LYFT", "ABNB", "DASH",
    "COIN", "HOOD", "RBLX", "U", "PATH", "AI", "SOUN", "BBAI",
]

BUSTED_GROWTH = [
    # Big runs that ended badly — essential ballast for honest base rates.
    "PTON", "ZM", "DOCU", "CHWY", "CVNA", "WBD", "PARA", "SNAP", "PINS",
    "BYND", "OPEN", "WE", "AFRM", "UPST", "FUBO", "SKLZ", "CLOV", "WISH",
]

EV_AND_ENERGY_TRANSITION = [
    "RIVN", "LCID", "NIO", "XPEV", "LI", "FSR", "NKLA", "CHPT", "BLNK",
    "ENPH", "SEDG", "RUN", "FSLR", "PLUG", "BE", "QS",
]

MEME_AND_RETAIL = ["GME", "AMC", "BB", "NOK", "KOSS", "EXPR", "BBBY", "MULN"]

SEMIS_AND_HARDWARE = [
    "SMCI", "DELL", "HPQ", "HPE", "WDC", "STX", "GLW", "TER", "ENTG",
    "ONTO", "AXTI", "COHR", "LITE", "IIVI", "FORM", "ACLS", "UCTT",
]

BIOTECH_PHARMA = [
    "LLY", "NVO", "MRNA", "BNTX", "PFE", "MRK", "ABBV", "AMGN", "GILD",
    "REGN", "VRTX", "BIIB", "ALNY", "SRPT", "IONS", "EXAS", "CRSP", "NTLA",
    "BEAM", "EDIT", "SAVA", "AXSM", "KRTX", "MDGL", "VKTX",
]

FINANCIALS = [
    "JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW", "BLK", "AXP", "V", "MA",
    "PGR", "ALL", "MET", "PRU", "SOFI", "LC", "ALLY", "SIVBQ", "FRCB",
]

ENERGY_MATERIALS = [
    "XOM", "CVX", "COP", "SLB", "HAL", "OXY", "DVN", "FANG", "MRO", "APA",
    "FCX", "NEM", "GOLD", "AA", "X", "CLF", "NUE", "MP", "ALB", "SQM",
    "UEC", "CCJ", "SMR", "OKLO", "LEU", "IAG", "AG", "HL",
]

HEALTHCARE_INDUSTRIAL = [
    "UNH", "CVS", "CI", "HUM", "ISRG", "SYK", "BSX", "MDT", "ABT", "TMO",
    "DHR", "GE", "HON", "CAT", "DE", "BA", "LMT", "RTX", "NOC", "GD",
    "UNP", "CSX", "UPS", "FDX", "URI", "PWR", "ETN", "PH", "EMR",
]

CONSUMER = [
    "WMT", "COST", "TGT", "HD", "LOW", "NKE", "SBUX", "MCD", "CMG", "YUM",
    "KO", "PEP", "PG", "CL", "KMB", "MO", "PM", "DIS", "NFLX", "CCL",
    "RCL", "NCLH", "AAL", "DAL", "UAL", "LUV", "MAR", "HLT", "LVS", "WYNN",
    "DKNG", "PENN", "FIGS", "DOCS", "ELF", "CELH", "MNST", "DECK", "CROX",
]

ETFS = ["SPY", "QQQ", "IWM", "XLK", "XLE", "XLF", "XLV", "SMH", "ARKK"]


def default_universe() -> list[str]:
    groups = [
        MEGA_TECH, SOFTWARE_GROWTH, BUSTED_GROWTH, EV_AND_ENERGY_TRANSITION,
        MEME_AND_RETAIL, SEMIS_AND_HARDWARE, BIOTECH_PHARMA, FINANCIALS,
        ENERGY_MATERIALS, HEALTHCARE_INDUSTRIAL, CONSUMER, ETFS,
    ]
    seen: dict[str, None] = {}
    for group in groups:
        for ticker in group:
            seen.setdefault(ticker, None)
    return list(seen)


UNIVERSE = default_universe()

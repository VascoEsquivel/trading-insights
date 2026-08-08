"""Candidate discovery — look past the watchlist for things that are moving.

Three keyless-or-cheap sources:
  * stocks — Yahoo's predefined screeners via yfinance, which return market-wide
    movers already carrying volume, 50-day average and 52-week range, so a
    candidate can be ranked without a follow-up request per symbol.
  * crypto — CoinGecko /search/trending (what people are looking up) and
    /coins/markets (the actual 24h leaderboard).
  * meme  — DexScreener boosted tokens, shared with the meme tab.

Finnhub's general news feed is not used for this: its items come back with an
empty `related` field, so headlines can't be mapped to tickers.

Discovery surfaces candidates. It does not claim any of them will go up.
"""
from __future__ import annotations

from typing import Any

import config
from collector import RateLimited, log, request_json
from trading import db

CG_BASE = "https://api.coingecko.com/api/v3"
CG_SOURCE = "coingecko"

# Yahoo screener ids, keyed by the label shown in the UI.
STOCK_SCREENS: dict[str, str] = {
    "Day gainers": "day_gainers",
    "Most active": "most_actives",
    "Small-cap gainers": "small_cap_gainers",
    "Growth tech": "growth_technology_stocks",
    "Undervalued growth": "undervalued_growth_stocks",
    "Aggressive small caps": "aggressive_small_caps",
    "Day losers": "day_losers",
}


def _ratio(numerator: Any, denominator: Any) -> float | None:
    try:
        if numerator is None or not denominator:
            return None
        return float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def screen_stocks(screen: str = "day_gainers", count: int = 25) -> list[dict[str, Any]]:
    """Market-wide stock candidates from one Yahoo screener."""
    try:
        import yfinance as yf

        payload = yf.screen(screen, count=count)
    except Exception as exc:
        log.error("yfinance screen %s failed: %s", screen, exc)
        return []

    quotes = payload.get("quotes", []) if isinstance(payload, dict) else []
    out: list[dict[str, Any]] = []
    for q in quotes:
        symbol = q.get("symbol")
        price = q.get("regularMarketPrice")
        if not symbol or price is None:
            continue
        low = q.get("fiftyTwoWeekLow")
        high = q.get("fiftyTwoWeekHigh")
        range_position = None
        if low is not None and high is not None and high > low:
            range_position = (price - low) / (high - low)
        out.append(
            {
                "symbol": symbol,
                "name": q.get("longName") or q.get("shortName") or symbol,
                "asset_class": "stock",
                "source_id": None,
                "price": price,
                "change_24h": q.get("regularMarketChangePercent"),
                "volume": q.get("regularMarketVolume"),
                "market_cap": q.get("marketCap"),
                "volume_ratio": _ratio(
                    q.get("regularMarketVolume"), q.get("averageDailyVolume3Month")
                ),
                "vs_50d": q.get("fiftyDayAverageChangePercent"),
                "range_position": range_position,
                "exchange": q.get("fullExchangeName"),
            }
        )
    log.info("discovery: %d stock candidates from %s", len(out), screen)
    return out


def _cg_headers() -> dict[str, str]:
    return {"x-cg-demo-api-key": config.COINGECKO_API_KEY} if config.COINGECKO_API_KEY else {}


def trending_crypto() -> list[dict[str, Any]]:
    """CoinGecko's trending list — ranked by what people are searching for."""
    try:
        data = request_json(
            f"{CG_BASE}/search/trending", source=CG_SOURCE, headers=_cg_headers()
        )
    except RateLimited:
        return []
    except Exception as exc:
        log.error("coingecko /search/trending failed: %s", exc)
        return []

    out = []
    for wrapper in (data or {}).get("coins", []):
        item = wrapper.get("item") or {}
        extra = item.get("data") or {}
        change = (extra.get("price_change_percentage_24h") or {}).get("usd")
        out.append(
            {
                "symbol": (item.get("symbol") or "?").upper(),
                "name": item.get("name") or item.get("id"),
                "asset_class": "crypto",
                "source_id": item.get("id"),
                "price": extra.get("price"),
                "change_24h": change,
                "volume": _parse_money(extra.get("total_volume")),
                "market_cap": _parse_money(extra.get("market_cap")),
                "volume_ratio": None,
                "vs_50d": None,
                "range_position": None,
                "rank": item.get("market_cap_rank"),
            }
        )
    log.info("discovery: %d trending coins", len(out))
    return out


def _parse_money(value: Any) -> float | None:
    """CoinGecko's trending payload returns these pre-formatted, e.g. "$1,234"."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def crypto_movers(per_page: int = 250, top: int = 25) -> list[dict[str, Any]]:
    """The actual 24h leaderboard across the largest coins."""
    try:
        data = request_json(
            f"{CG_BASE}/coins/markets",
            source=CG_SOURCE,
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": str(per_page),
                "page": "1",
                "price_change_percentage": "24h,7d",
            },
            headers=_cg_headers(),
        )
    except RateLimited:
        return []
    except Exception as exc:
        log.error("coingecko /coins/markets failed: %s", exc)
        return []

    rows = []
    for c in data or []:
        change = c.get("price_change_percentage_24h")
        if change is None:
            continue
        high = c.get("high_24h")
        low = c.get("low_24h")
        price = c.get("current_price")
        range_position = None
        if None not in (high, low, price) and high > low:
            range_position = (price - low) / (high - low)
        rows.append(
            {
                "symbol": (c.get("symbol") or "?").upper(),
                "name": c.get("name"),
                "asset_class": "crypto",
                "source_id": c.get("id"),
                "price": price,
                "change_24h": change,
                "change_7d": c.get("price_change_percentage_7d_in_currency"),
                "volume": c.get("total_volume"),
                "market_cap": c.get("market_cap"),
                # Turnover stands in for the "unusual volume" check that the
                # stock screener gets from a 3-month average.
                "volume_ratio": _ratio(c.get("total_volume"), c.get("market_cap")),
                "vs_50d": None,
                "range_position": range_position,
                "rank": c.get("market_cap_rank"),
            }
        )
    rows.sort(key=lambda r: abs(r["change_24h"]), reverse=True)
    log.info("discovery: %d crypto movers scanned", len(rows))
    return rows[:top]


def meme_candidates(limit: int = 15) -> list[dict[str, Any]]:
    """DexScreener boosted tokens, normalised into the candidate shape."""
    from collector import memecoins

    rows = []
    for r in memecoins.fetch_trending(limit=limit):
        rows.append(
            {
                "symbol": r["symbol"],
                "name": r["name"],
                "asset_class": "meme",
                "source_id": r["source_id"],
                "price": r["price"],
                "change_24h": r["pct_change_24h"],
                "volume": r["volume_24h"],
                "market_cap": r["market_cap"],
                "liquidity_usd": r["liquidity_usd"],
                "pair_created_at": r["pair_created_at"],
                "volume_ratio": _ratio(r["volume_24h"], r["liquidity_usd"]),
                "vs_50d": None,
                "range_position": None,
                "chain": r["chain"],
            }
        )
    return rows


def prime_news(candidate: dict[str, Any]) -> int:
    """Fetch headlines for one candidate so a deep read has something to weigh.

    Discovery candidates are off-watchlist, so nothing has been collected for
    them yet. Rows land in news_items like any other and get pruned normally.
    """
    entry = {
        "symbol": candidate["symbol"],
        "name": candidate.get("name"),
        "asset_class": candidate["asset_class"],
        "source_id": candidate.get("source_id"),
    }
    try:
        if candidate["asset_class"] == "stock":
            from collector import stocks

            return stocks.fetch_news(symbols=[candidate["symbol"]])
        from collector import crypto

        return crypto.fetch_news(entries=[entry])
    except Exception as exc:
        log.error("priming news for %s failed: %s", candidate["symbol"], exc)
        return 0

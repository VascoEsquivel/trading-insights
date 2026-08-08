"""Stock quotes and company news.

Prices come from Finnhub /quote (60 calls/min on the free tier, one call per
symbol). Finnhub's /stock/candle returns 403 for US equities on free keys, so
charts use yfinance instead — see collector.stocks.fetch_candles.

/quote carries no volume field (c/d/dp/h/l/o/pc/t only), so daily volume is
topped up from a single batched yfinance call on a much slower cadence and
cached in-process between quote cycles.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Sequence

import config
from collector import RateLimited, log, request_json
from trading import db

FINNHUB_BASE = "https://finnhub.io/api/v1"
SOURCE = "finnhub"

# symbol -> last known daily volume, refreshed on STOCK_VOLUME_INTERVAL
_VOLUME_CACHE: dict[str, float] = {}


def _stock_symbols() -> list[str]:
    return [w["symbol"] for w in db.get_watchlist("stock")]


# --------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------


def fetch_quotes() -> int:
    """Poll /quote for each watched stock and write price snapshots."""
    if not config.FINNHUB_API_KEY:
        log.warning("FINNHUB_API_KEY not set - skipping stock quotes")
        return 0

    symbols = _stock_symbols()
    rows: list[dict[str, Any]] = []
    fetched_at = db.iso_now()

    for symbol in symbols:
        try:
            data = request_json(
                f"{FINNHUB_BASE}/quote",
                source=SOURCE,
                params={"symbol": symbol, "token": config.FINNHUB_API_KEY},
            )
        except RateLimited:
            break  # whole source is cooling down; stop the cycle
        except Exception as exc:
            log.error("finnhub quote failed for %s: %s", symbol, exc)
            continue

        if not data:
            continue
        price = data.get("c")
        if not price:  # 0 or None means Finnhub has no quote for this ticker
            log.warning("finnhub returned no price for %s", symbol)
            continue

        rows.append(
            {
                "symbol": symbol,
                "asset_class": "stock",
                "price": float(price),
                "volume": _VOLUME_CACHE.get(symbol),
                "pct_change_24h": data.get("dp"),
                "liquidity_usd": None,
                "market_cap": None,
                "fetched_at": fetched_at,
            }
        )

    written = db.insert_price_snapshots(rows)
    log.info("stocks: wrote %d/%d quotes", written, len(symbols))
    return written


# --------------------------------------------------------------------------
# Volume (yfinance, slow cadence)
# --------------------------------------------------------------------------


def refresh_volumes() -> int:
    """Refresh the daily-volume cache for all watched stocks in one batch call.

    yfinance scrapes Yahoo and can break without warning; a failure here just
    leaves the volume column stale, it never blocks quotes.
    """
    symbols = _stock_symbols()
    if not symbols:
        return 0
    try:
        import yfinance as yf

        data = yf.download(
            tickers=" ".join(symbols),
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        log.error("yfinance volume refresh failed: %s", exc)
        return 0

    updated = 0
    for symbol in symbols:
        try:
            if len(symbols) == 1:
                series = data["Volume"]
            else:
                series = data[symbol]["Volume"]
            series = series.dropna()
            if len(series):
                _VOLUME_CACHE[symbol] = float(series.iloc[-1])
                updated += 1
        except Exception:
            continue
    log.info("stocks: refreshed volume for %d/%d symbols", updated, len(symbols))
    return updated


# --------------------------------------------------------------------------
# Candles (called on demand from the UI, not from the collector loop)
# --------------------------------------------------------------------------


def fetch_candles(symbol: str, period: str = "1mo", interval: str = "1d"):
    """OHLCV DataFrame for one ticker, or None if Yahoo is unavailable.

    Isolated deliberately: a yfinance breakage degrades one chart, not the app.
    """
    try:
        import yfinance as yf

        hist = yf.Ticker(symbol).history(period=period, interval=interval)
        if hist is None or hist.empty:
            return None
        return hist
    except Exception as exc:
        logging.getLogger("collector").error("yfinance candles failed for %s: %s", symbol, exc)
        return None


# --------------------------------------------------------------------------
# Company news
# --------------------------------------------------------------------------


def fetch_news(days_back: int = 3, symbols: list[str] | None = None) -> int:
    """Company news for the watched stocks, or for an explicit list.

    Discovery passes symbols directly, since candidates are off-watchlist.
    """
    if not config.FINNHUB_API_KEY:
        return 0

    symbols = symbols if symbols is not None else _stock_symbols()
    today = db.utcnow().date()
    frm = (db.utcnow() - timedelta(days=days_back)).date()
    rows: list[dict[str, Any]] = []

    for symbol in symbols:
        try:
            items = request_json(
                f"{FINNHUB_BASE}/company-news",
                source=SOURCE,
                params={
                    "symbol": symbol,
                    "from": frm.isoformat(),
                    "to": today.isoformat(),
                    "token": config.FINNHUB_API_KEY,
                },
            )
        except RateLimited:
            break
        except Exception as exc:
            log.error("finnhub news failed for %s: %s", symbol, exc)
            continue

        if not items:
            continue
        for item in items[:25]:
            url = item.get("url")
            headline = item.get("headline")
            if not url or not headline:
                continue
            published = db.from_epoch_s(item.get("datetime"))
            rows.append(
                {
                    "symbol": symbol,
                    "headline": headline,
                    "source": item.get("source") or "Finnhub",
                    "url": url,
                    "published_at": db.iso(published) if published else None,
                }
            )

    db.insert_news(rows)
    log.info("stocks: %d news rows offered", len(rows))
    return len(rows)

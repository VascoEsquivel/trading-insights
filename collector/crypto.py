"""Crypto prices, OHLC, and headlines.

CoinGecko Demo keys allow ~100 calls/min but cap at 10,000 calls/month, and the
month cap is what actually binds. /simple/price accepts comma-separated ids, so
the entire crypto + established-meme watchlist costs ONE call per cycle. OHLC is
pulled only when a chart is drawn, and cached.

Headlines come from keyless publisher RSS: CoinGecko's /news and CryptoCompare's
news API both return 401 without a paid plan.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

import config
from collector import RateLimited, log, request_json, request_text
from trading import db

CG_BASE = "https://api.coingecko.com/api/v3"
SOURCE = "coingecko"
NEWS_SOURCE = "crypto-rss"


def _cg_headers() -> dict[str, str]:
    return {"x-cg-demo-api-key": config.COINGECKO_API_KEY} if config.COINGECKO_API_KEY else {}


def coingecko_watchlist() -> list[dict[str, Any]]:
    """Watchlist rows priced by CoinGecko: all crypto, plus memes with a coin id."""
    rows = db.get_watchlist("crypto") + db.get_watchlist("meme")
    return [r for r in rows if r["source_id"] and not db.is_dex_source(r["source_id"])]


# --------------------------------------------------------------------------
# Prices — one batched call for the whole watchlist
# --------------------------------------------------------------------------


def fetch_prices() -> int:
    entries = coingecko_watchlist()
    if not entries:
        return 0

    ids = sorted({e["source_id"] for e in entries})
    try:
        data = request_json(
            f"{CG_BASE}/simple/price",
            source=SOURCE,
            params={
                "ids": ",".join(ids),
                "vs_currencies": "usd",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
                "include_24hr_change": "true",
            },
            headers=_cg_headers(),
        )
    except RateLimited:
        return 0
    except Exception as exc:
        log.error("coingecko /simple/price failed: %s", exc)
        return 0

    if not data:
        return 0

    fetched_at = db.iso_now()
    rows = []
    for entry in entries:
        quote = data.get(entry["source_id"])
        if not quote or quote.get("usd") is None:
            log.warning("coingecko returned nothing for id %s", entry["source_id"])
            continue
        rows.append(
            {
                "symbol": entry["symbol"],
                "asset_class": entry["asset_class"],
                "price": float(quote["usd"]),
                "volume": quote.get("usd_24h_vol"),
                "pct_change_24h": quote.get("usd_24h_change"),
                "liquidity_usd": None,
                "market_cap": quote.get("usd_market_cap"),
                "fetched_at": fetched_at,
            }
        )

    written = db.insert_price_snapshots(rows)
    log.info("crypto: wrote %d/%d prices (1 API call)", written, len(entries))
    return written


# --------------------------------------------------------------------------
# OHLC — on demand from the UI only
# --------------------------------------------------------------------------


def fetch_ohlc(coin_id: str, days: int = 7) -> list[list[float]] | None:
    """[[ts_ms, open, high, low, close], ...] or None.

    Called from the chart path behind a Streamlit cache, never from the loop —
    every call here eats into the monthly quota.
    """
    try:
        data = request_json(
            f"{CG_BASE}/coins/{coin_id}/ohlc",
            source=SOURCE,
            params={"vs_currency": "usd", "days": str(days)},
            headers=_cg_headers(),
        )
    except RateLimited:
        return None
    except Exception as exc:
        log.error("coingecko ohlc failed for %s: %s", coin_id, exc)
        return None
    return data if isinstance(data, list) and data else None


# --------------------------------------------------------------------------
# News — keyless RSS, matched to watched symbols
# --------------------------------------------------------------------------


def _match_patterns(entry: dict[str, Any]) -> list[re.Pattern]:
    """Regexes that decide whether a headline is about this coin."""
    terms: set[str] = set()

    symbol = db.base_symbol(entry["symbol"])
    if len(symbol) >= 3:
        terms.add(symbol)

    name = entry.get("name") or ""
    name = re.sub(r"\s*\(.*?\)\s*", "", name).strip()  # drop "(Raydium, Solana)"
    if len(name) >= 3:
        terms.add(name)

    return [re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE) for t in terms if t]


def _parse_feed(xml_text: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text.encode("utf-8", "replace"))
    except ET.ParseError as exc:
        log.error("RSS parse error: %s", exc)
        return []

    items = []
    for node in root.findall(".//item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        if not title or not link:
            continue
        published = None
        raw_date = node.findtext("pubDate")
        if raw_date:
            try:
                published = db.iso(parsedate_to_datetime(raw_date))
            except (TypeError, ValueError):
                published = None
        items.append({"title": title, "link": link, "published_at": published})
    return items


def fetch_news() -> int:
    entries = db.get_watchlist("crypto") + db.get_watchlist("meme")
    if not entries:
        return 0

    matchers = [(e, _match_patterns(e)) for e in entries]
    rows: list[dict[str, Any]] = []

    for source_name, url in config.CRYPTO_NEWS_FEEDS:
        try:
            text = request_text(url, source=NEWS_SOURCE)
        except RateLimited:
            break
        except Exception as exc:
            log.error("crypto news feed %s failed: %s", source_name, exc)
            continue
        if not text:
            continue

        for item in _parse_feed(text):
            for entry, patterns in matchers:
                if any(p.search(item["title"]) for p in patterns):
                    rows.append(
                        {
                            "symbol": entry["symbol"],
                            "headline": item["title"],
                            "source": source_name,
                            "url": item["link"],
                            "published_at": item["published_at"],
                        }
                    )

    db.insert_news(rows)
    log.info("crypto: %d news matches offered", len(rows))
    return len(rows)

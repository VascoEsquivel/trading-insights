"""DexScreener pairs and trending discovery.

DexScreener is keyless (~300 req/min on pairs/search, ~60/min on
token-profiles/boosts) and its pair payload carries exactly the risk context the
meme tab pins next to price: priceUsd, liquidity.usd, marketCap/fdv, and
pairCreatedAt.

There is no historical OHLC here — the API exposes current and 24h stats only.
Charts for DexScreener-only tokens are drawn from our own accumulated
price_snapshots, which is why this poll runs on a tight cadence.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import config
from collector import RateLimited, log, request_json
from trading import db

DS_BASE = "https://api.dexscreener.com"
SOURCE = "dexscreener"
BOOST_SOURCE = "dexscreener-boosts"

# The pairs endpoint takes comma-separated addresses on a single chain.
MAX_PAIRS_PER_CALL = 30


def dex_watchlist() -> list[dict[str, Any]]:
    return [
        r
        for r in db.get_watchlist("meme")
        if db.is_dex_source(r["source_id"])
    ]


def _snapshot_from_pair(symbol: str, pair: dict[str, Any], fetched_at: str) -> dict[str, Any]:
    price = pair.get("priceUsd")
    liquidity = (pair.get("liquidity") or {}).get("usd")
    volume = (pair.get("volume") or {}).get("h24")
    change = (pair.get("priceChange") or {}).get("h24")
    return {
        "symbol": symbol,
        "asset_class": "meme",
        "price": float(price) if price is not None else None,
        "volume": float(volume) if volume is not None else None,
        "pct_change_24h": float(change) if change is not None else None,
        "liquidity_usd": float(liquidity) if liquidity is not None else None,
        "market_cap": pair.get("marketCap") or pair.get("fdv"),
        "fetched_at": fetched_at,
    }


def fetch_pairs() -> int:
    """Poll every watched DexScreener pair, batched per chain."""
    entries = dex_watchlist()
    if not entries:
        return 0

    # chain -> {pair_address: symbol}
    by_chain: dict[str, dict[str, str]] = defaultdict(dict)
    for entry in entries:
        chain, pair_address = db.split_dex_source(entry["source_id"])
        if chain and pair_address:
            by_chain[chain][pair_address.lower()] = entry["symbol"]

    fetched_at = db.iso_now()
    rows: list[dict[str, Any]] = []
    ages: list[tuple[str, str]] = []

    for chain, mapping in by_chain.items():
        addresses = list(mapping)
        for i in range(0, len(addresses), MAX_PAIRS_PER_CALL):
            chunk = addresses[i : i + MAX_PAIRS_PER_CALL]
            try:
                data = request_json(
                    f"{DS_BASE}/latest/dex/pairs/{chain}/{','.join(chunk)}",
                    source=SOURCE,
                )
            except RateLimited:
                return db.insert_price_snapshots(rows)
            except Exception as exc:
                log.error("dexscreener pairs failed for %s: %s", chain, exc)
                continue

            for pair in (data or {}).get("pairs") or []:
                addr = (pair.get("pairAddress") or "").lower()
                symbol = mapping.get(addr)
                if not symbol:
                    continue
                rows.append(_snapshot_from_pair(symbol, pair, fetched_at))
                created = db.from_epoch_ms(pair.get("pairCreatedAt"))
                if created:
                    ages.append((symbol, db.iso(created)))

    for symbol, created_iso in ages:
        db.set_pair_created_at(symbol, created_iso)

    written = db.insert_price_snapshots(rows)
    log.info("memecoins: wrote %d/%d pairs", written, len(entries))
    return written


# --------------------------------------------------------------------------
# Trending discovery
# --------------------------------------------------------------------------


def fetch_trending(limit: int = 12) -> list[dict[str, Any]]:
    """Boosted tokens resolved to their deepest-liquidity pair.

    Returned for display and one-click watchlist adds — nothing is added
    automatically. Each row carries the same risk context as a watched pair.
    """
    try:
        boosts = request_json(f"{DS_BASE}/token-boosts/top/v1", source=BOOST_SOURCE)
    except RateLimited:
        return []
    except Exception as exc:
        log.error("dexscreener boosts failed: %s", exc)
        return []

    if not isinstance(boosts, list):
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for boost in boosts[: limit * 2]:
        if len(out) >= limit:
            break
        chain = boost.get("chainId")
        token = boost.get("tokenAddress")
        if not chain or not token or token in seen:
            continue
        seen.add(token)

        try:
            data = request_json(f"{DS_BASE}/latest/dex/tokens/{token}", source=SOURCE)
        except RateLimited:
            break
        except Exception as exc:
            log.error("dexscreener token lookup failed for %s: %s", token, exc)
            continue

        pairs = (data or {}).get("pairs") or []
        if not pairs:
            continue
        # Deepest pool is the one worth tracking.
        pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)

        base = pair.get("baseToken") or {}
        pair_address = pair.get("pairAddress") or ""
        token_symbol = (base.get("symbol") or "?").upper()
        created = db.from_epoch_ms(pair.get("pairCreatedAt"))

        out.append(
            {
                "symbol": db.dex_symbol(token_symbol, pair_address),
                "token_symbol": token_symbol,
                "name": base.get("name") or token_symbol,
                "source_id": f"{pair.get('chainId')}:{pair_address}",
                "chain": pair.get("chainId"),
                "price": float(pair["priceUsd"]) if pair.get("priceUsd") else None,
                "pct_change_24h": (pair.get("priceChange") or {}).get("h24"),
                "volume_24h": (pair.get("volume") or {}).get("h24"),
                "liquidity_usd": (pair.get("liquidity") or {}).get("usd"),
                "market_cap": pair.get("marketCap") or pair.get("fdv"),
                "pair_created_at": db.iso(created) if created else None,
                "url": pair.get("url"),
            }
        )

    log.info("memecoins: resolved %d trending pairs", len(out))
    return out


def lookup_pair(chain: str, pair_address: str) -> dict[str, Any] | None:
    """Resolve one chain+pair for the 'add symbol' form."""
    try:
        data = request_json(
            f"{DS_BASE}/latest/dex/pairs/{chain}/{pair_address}", source=SOURCE
        )
    except Exception as exc:
        log.error("dexscreener lookup failed: %s", exc)
        return None
    pairs = (data or {}).get("pairs") or []
    return pairs[0] if pairs else None

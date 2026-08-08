"""SQLite schema and queries.

The collector process writes; the Streamlit app only reads. WAL journalling is
what makes that safe to do concurrently from two processes.

All timestamps are stored as ISO-8601 UTC strings ("2026-08-07T21:13:21+00:00")
so they sort lexicographically and stay readable in a sqlite3 shell.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import config

# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """Serialise an aware datetime to UTC ISO-8601."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def iso_now() -> str:
    return iso(utcnow())


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def from_epoch_ms(ms: int | float | None) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def from_epoch_s(seconds: int | float | None) -> datetime | None:
    if not seconds:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


# --------------------------------------------------------------------------
# Symbol / source-id conventions
# --------------------------------------------------------------------------


def is_dex_source(source_id: str | None) -> bool:
    """True for DexScreener ids ("solana:FCEn..."), false for CoinGecko ids.

    CoinGecko coin ids are slugs ("shiba-inu") and never contain a colon, so the
    separator is a safe discriminator.
    """
    return bool(source_id) and ":" in source_id


def split_dex_source(source_id: str) -> tuple[str, str]:
    """"solana:FCEn..." -> ("solana", "FCEn...")"""
    chain, _, pair_address = source_id.partition(":")
    return chain, pair_address


def base_symbol(symbol: str) -> str:
    """Strip the "~<last4>" disambiguator from a DexScreener watchlist symbol."""
    return symbol.split("~", 1)[0]


def dex_symbol(token_symbol: str, pair_address: str) -> str:
    """Build a unique watchlist symbol for a DexScreener pair."""
    return f"{token_symbol.upper()}~{pair_address[-4:]}"


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------


@contextmanager
def connect(readonly: bool = False):
    """Yield a row-dict connection, committing on clean exit."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            conn.execute("BEGIN")
        yield conn
        # executescript() commits implicitly, so the transaction may already be
        # closed by the time we get here.
        if not readonly and conn.in_transaction:
            conn.execute("COMMIT")
    except Exception:
        if not readonly and conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL UNIQUE,
    asset_class     TEXT NOT NULL CHECK (asset_class IN ('stock','crypto','meme')),
    name            TEXT,
    source_id       TEXT,          -- CoinGecko id | "chain:pairAddress" | NULL
    pair_created_at TEXT,          -- DexScreener only; token age derives from it
    added_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    asset_class     TEXT NOT NULL,
    price           REAL,
    volume          REAL,
    pct_change_24h  REAL,
    liquidity_usd   REAL,          -- meme coins only
    market_cap      REAL,
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_symbol_time
    ON price_snapshots (symbol, fetched_at DESC);

CREATE TABLE IF NOT EXISTS news_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    headline      TEXT NOT NULL,
    source        TEXT,
    url           TEXT NOT NULL,
    published_at  TEXT,
    UNIQUE (symbol, url)
);
CREATE INDEX IF NOT EXISTS idx_news_symbol_time
    ON news_items (symbol, published_at DESC);

CREATE TABLE IF NOT EXISTS social_mentions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    platform        TEXT NOT NULL,
    mention_count   INTEGER NOT NULL DEFAULT 0,
    sentiment_score REAL,
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_social_symbol_time
    ON social_mentions (symbol, window_end DESC);

CREATE TABLE IF NOT EXISTS portfolio (
    id               INTEGER PRIMARY KEY CHECK (id = 1),  -- single paper account
    cash_balance     REAL NOT NULL,
    starting_balance REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL UNIQUE,
    asset_class     TEXT NOT NULL,
    quantity        REAL NOT NULL,
    avg_cost_basis  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    asset_class   TEXT NOT NULL,
    side          TEXT NOT NULL CHECK (side IN ('buy','sell')),
    quantity      REAL NOT NULL,
    price         REAL NOT NULL,
    executed_at   TEXT NOT NULL,
    realized_pnl  REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_time ON trades (executed_at DESC);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    total_value  REAL NOT NULL,
    cash_balance REAL NOT NULL,
    snapshot_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_psnap_time ON portfolio_snapshots (snapshot_at);
"""


def init_db(seed: bool = True) -> None:
    """Create tables, the single portfolio row, and (optionally) seed symbols."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO portfolio (id, cash_balance, starting_balance) "
            "VALUES (1, ?, ?)",
            (config.STARTING_BALANCE, config.STARTING_BALANCE),
        )
        if seed:
            now = iso_now()
            for symbol, asset_class, name, source_id in config.SEED_WATCHLIST:
                conn.execute(
                    "INSERT OR IGNORE INTO watchlist "
                    "(symbol, asset_class, name, source_id, added_at) "
                    "VALUES (?,?,?,?,?)",
                    (symbol, asset_class, name, source_id, now),
                )


# --------------------------------------------------------------------------
# Watchlist
# --------------------------------------------------------------------------


def get_watchlist(asset_class: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM watchlist"
    params: list[Any] = []
    if asset_class:
        sql += " WHERE asset_class = ?"
        params.append(asset_class)
    sql += " ORDER BY symbol"
    with connect(readonly=True) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def add_watchlist_item(
    symbol: str,
    asset_class: str,
    name: str | None = None,
    source_id: str | None = None,
    pair_created_at: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist "
            "(symbol, asset_class, name, source_id, pair_created_at, added_at) "
            "VALUES (?,?,?,?,?,?)",
            (symbol, asset_class, name, source_id, pair_created_at, iso_now()),
        )


def remove_watchlist_item(symbol: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))


def set_pair_created_at(symbol: str, pair_created_at: str | None) -> None:
    if not pair_created_at:
        return
    with connect() as conn:
        conn.execute(
            "UPDATE watchlist SET pair_created_at = ? "
            "WHERE symbol = ? AND pair_created_at IS NULL",
            (pair_created_at, symbol),
        )


# --------------------------------------------------------------------------
# Price snapshots
# --------------------------------------------------------------------------


def insert_price_snapshots(rows: Iterable[dict[str, Any]]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with connect() as conn:
        conn.executemany(
            "INSERT INTO price_snapshots "
            "(symbol, asset_class, price, volume, pct_change_24h, "
            " liquidity_usd, market_cap, fetched_at) "
            "VALUES (:symbol,:asset_class,:price,:volume,:pct_change_24h,"
            ":liquidity_usd,:market_cap,:fetched_at)",
            rows,
        )
    return len(rows)


def latest_snapshots(asset_class: str | None = None) -> dict[str, dict[str, Any]]:
    """Most recent snapshot per symbol, keyed by symbol."""
    sql = """
        SELECT s.* FROM price_snapshots s
        JOIN (
            SELECT symbol, MAX(fetched_at) AS mx
            FROM price_snapshots GROUP BY symbol
        ) latest ON latest.symbol = s.symbol AND latest.mx = s.fetched_at
    """
    params: list[Any] = []
    if asset_class:
        sql += " WHERE s.asset_class = ?"
        params.append(asset_class)
    with connect(readonly=True) as conn:
        out: dict[str, dict[str, Any]] = {}
        for r in conn.execute(sql, params):
            out[r["symbol"]] = dict(r)  # dedupe ties on identical timestamps
        return out


def latest_prices() -> dict[str, float]:
    """symbol -> last known price. Used for marking the portfolio to market."""
    return {
        sym: snap["price"]
        for sym, snap in latest_snapshots().items()
        if snap.get("price") is not None
    }


def snapshot_history(symbol: str, hours: int = 48) -> list[dict[str, Any]]:
    cutoff = iso(utcnow() - timedelta(hours=hours))
    with connect(readonly=True) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT fetched_at, price, volume, liquidity_usd, market_cap "
                "FROM price_snapshots WHERE symbol = ? AND fetched_at >= ? "
                "ORDER BY fetched_at",
                (symbol, cutoff),
            )
        ]


# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------


def insert_news(rows: Iterable[dict[str, Any]]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with connect() as conn:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO news_items "
            "(symbol, headline, source, url, published_at) "
            "VALUES (:symbol,:headline,:source,:url,:published_at)",
            rows,
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def get_news(symbols: Sequence[str], limit: int = 40) -> list[dict[str, Any]]:
    if not symbols:
        return []
    placeholders = ",".join("?" * len(symbols))
    with connect(readonly=True) as conn:
        return [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM news_items WHERE symbol IN ({placeholders}) "
                "ORDER BY COALESCE(published_at,'') DESC LIMIT ?",
                (*symbols, limit),
            )
        ]


def prune_news(keep_days: int = 14) -> None:
    cutoff = iso(utcnow() - timedelta(days=keep_days))
    with connect() as conn:
        conn.execute(
            "DELETE FROM news_items WHERE COALESCE(published_at,'') < ?", (cutoff,)
        )


# --------------------------------------------------------------------------
# Social mentions
# --------------------------------------------------------------------------


def insert_social_mentions(rows: Iterable[dict[str, Any]]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with connect() as conn:
        conn.executemany(
            "INSERT INTO social_mentions "
            "(symbol, platform, mention_count, sentiment_score, "
            " window_start, window_end) "
            "VALUES (:symbol,:platform,:mention_count,:sentiment_score,"
            ":window_start,:window_end)",
            rows,
        )
    return len(rows)


def latest_social(symbol: str) -> dict[str, Any] | None:
    with connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT * FROM social_mentions WHERE symbol = ? "
            "ORDER BY window_end DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return dict(row) if row else None


def social_baseline(symbol: str, hours: int = 24) -> float | None:
    """Mean mentions per window over the trailing period, excluding the newest.

    This is the number the current window is compared against to get a trend
    direction — not a verdict, just "busier or quieter than usual".
    """
    cutoff = iso(utcnow() - timedelta(hours=hours))
    with connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT mention_count FROM social_mentions "
            "WHERE symbol = ? AND window_end >= ? ORDER BY window_end DESC",
            (symbol, cutoff),
        ).fetchall()
    prior = [r["mention_count"] for r in rows[1:]]
    return sum(prior) / len(prior) if prior else None

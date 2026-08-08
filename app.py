"""Trading Insights — Streamlit entrypoint.

Reads only from SQLite. The collector (`python -m collector.scheduler`) is a
separate process and is the only writer; no background threads are started here,
because Streamlit reruns this script top-to-bottom on every interaction and
would duplicate or orphan them.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import config
from trading import db

st.set_page_config(
    page_title="Trading Insights",
    page_icon="📈",
    layout="wide",
)

ASSET_TABS = [
    ("stock", "Stocks"),
    ("crypto", "Crypto"),
    ("meme", "Meme Coins"),
]


# --------------------------------------------------------------------------
# Cached reads (cleared on any write, and by the Refresh button)
# --------------------------------------------------------------------------

TTL = config.UI_CACHE_TTL_SECONDS


@st.cache_data(ttl=TTL)
def load_watchlist(asset_class: str | None = None):
    return db.get_watchlist(asset_class)


@st.cache_data(ttl=TTL)
def load_snapshots():
    return db.latest_snapshots()


def refresh_data() -> None:
    st.cache_data.clear()


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

DASH = "—"


def fmt_price(value: float | None) -> str:
    """Readable across nine orders of magnitude ($773.26 down to $3.08e-09)."""
    if value is None:
        return DASH
    magnitude = abs(value)
    if magnitude >= 1:
        return f"${value:,.2f}"
    if magnitude >= 0.01:
        return f"${value:.4f}"
    if magnitude >= 1e-8:
        return f"${value:.10f}".rstrip("0")
    return f"${value:.4g}"


def fmt_compact(value: float | None) -> str:
    if value is None:
        return DASH
    magnitude = abs(value)
    for cutoff, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if magnitude >= cutoff:
            return f"${value / cutoff:,.2f}{suffix}"
    return f"${value:,.2f}"


def fmt_pct(value: float | None) -> str:
    return DASH if value is None else f"{value:+.2f}%"


def fmt_age(created_iso: str | None) -> str:
    created = db.from_iso(created_iso)
    if created is None:
        return DASH
    delta = datetime.now(timezone.utc) - created
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)}m"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours // 24)}d"


def age_hours(created_iso: str | None) -> float | None:
    created = db.from_iso(created_iso)
    if created is None:
        return None
    return (datetime.now(timezone.utc) - created).total_seconds() / 3600


def fmt_when(iso_value: str | None) -> str:
    when = db.from_iso(iso_value)
    if when is None:
        return DASH
    seconds = (datetime.now(timezone.utc) - when).total_seconds()
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------


def newest_snapshot_time(snapshots: dict) -> str | None:
    times = [s["fetched_at"] for s in snapshots.values() if s.get("fetched_at")]
    return max(times) if times else None


def render_sidebar(snapshots: dict) -> None:
    with st.sidebar:
        st.markdown("### Trading Insights")
        st.caption("Paper trading only. No brokerage or exchange is ever linked.")

        newest = newest_snapshot_time(snapshots)
        when = db.from_iso(newest)
        if when is None:
            st.error(
                "No price data yet. Start the collector:\n\n"
                "`python -m collector.scheduler`"
            )
        else:
            stale_seconds = (datetime.now(timezone.utc) - when).total_seconds()
            if stale_seconds > 300:
                st.warning(
                    f"Last update {fmt_when(newest)} — the collector may not be running."
                )
            else:
                st.success(f"Data current as of {fmt_when(newest)}")

        st.divider()
        st.checkbox(
            "Auto-refresh",
            value=True,
            key="auto_refresh",
            help=f"Re-reads SQLite every {config.UI_AUTO_REFRESH_SECONDS}s.",
        )
        if st.button("Refresh now", width="stretch"):
            refresh_data()
            st.rerun()

        st.divider()
        st.caption(
            f"Reddit sentiment: {'on' if config.ENABLE_REDDIT else 'off'}  \n"
            f"Snapshot cadences: stocks {config.STOCK_QUOTE_INTERVAL}s · "
            f"crypto {config.CRYPTO_PRICE_INTERVAL}s · "
            f"meme {config.MEME_PAIR_INTERVAL}s"
        )

    _auto_refresh_tick()


@st.fragment(run_every=f"{config.UI_AUTO_REFRESH_SECONDS}s")
def _auto_refresh_tick() -> None:
    """Timer-only fragment; reruns the whole app so every tab re-reads SQLite.

    The fragment also runs on first paint and on every app rerun, not just when
    the timer fires, so it has to check elapsed time itself — rerunning
    unconditionally would loop before the page ever finished rendering.
    """
    if not st.session_state.get("auto_refresh", True):
        return
    now = time.monotonic()
    last = st.session_state.get("_last_auto_refresh")
    st.session_state["_last_auto_refresh"] = now
    if last is None or (now - last) < config.UI_AUTO_REFRESH_SECONDS * 0.9:
        return
    refresh_data()
    st.rerun(scope="app")


# --------------------------------------------------------------------------
# Watchlist table
# --------------------------------------------------------------------------


def build_table(entries: list[dict], snapshots: dict, asset_class: str) -> pd.DataFrame:
    rows = []
    for entry in entries:
        symbol = entry["symbol"]
        snap = snapshots.get(symbol, {})
        row = {
            "Symbol": symbol,
            "Name": entry.get("name") or DASH,
            "Price": fmt_price(snap.get("price")),
            "24h": fmt_pct(snap.get("pct_change_24h")),
            "Volume 24h": fmt_compact(snap.get("volume")),
        }
        if asset_class == "meme":
            # Risk context is pinned next to price, per the ground rules.
            row["Liquidity"] = fmt_compact(snap.get("liquidity_usd"))
            row["Market cap"] = fmt_compact(snap.get("market_cap"))
            row["Age"] = fmt_age(entry.get("pair_created_at"))
        row["Updated"] = fmt_when(snap.get("fetched_at"))
        rows.append(row)
    return pd.DataFrame(rows)


def render_watchlist(entries: list[dict], snapshots: dict, asset_class: str) -> None:
    if not entries:
        st.info("Nothing on this watchlist yet — add a symbol below.")
        return
    table = build_table(entries, snapshots, asset_class)
    st.dataframe(table, width="stretch", hide_index=True)

    missing = [e["symbol"] for e in entries if e["symbol"] not in snapshots]
    if missing:
        st.caption(
            f"No snapshot yet for {', '.join(missing)} — "
            "the collector picks new symbols up on its next cycle."
        )


# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------


def render_asset_tab(asset_class: str, label: str, snapshots: dict) -> None:
    entries = load_watchlist(asset_class)
    st.subheader(f"{label} watchlist")
    render_watchlist(entries, snapshots, asset_class)


def render_portfolio_tab() -> None:
    st.subheader("Paper portfolio")
    st.info("Trading engine lands in the next phase.")


def main() -> None:
    db.init_db()
    snapshots = load_snapshots()
    render_sidebar(snapshots)

    st.title("Trading Insights")
    st.caption(
        "Market data, news, and social signal for research and paper practice. "
        "Numbers are inputs — no recommendations are produced here."
    )

    tabs = st.tabs([label for _, label in ASSET_TABS] + ["Portfolio"])
    for tab, (asset_class, label) in zip(tabs, ASSET_TABS):
        with tab:
            render_asset_tab(asset_class, label, snapshots)
    with tabs[-1]:
        render_portfolio_tab()


main()

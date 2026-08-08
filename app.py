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
from collector import crypto as crypto_source
from collector import memecoins as meme_source
from trading import db, portfolio

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


def md(text: str) -> str:
    """Escape dollar signs for Streamlit markdown.

    st.markdown/caption/success/error treat paired `$` as LaTeX delimiters, which
    turns "$313.33 ... $10,000.00" into rendered math. Dataframe cells are not
    markdown, so they don't need this.
    """
    return text.replace("$", r"\$")


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


def flash(kind: str, message: str) -> None:
    """Queue a message to survive the rerun that follows a write."""
    st.session_state.setdefault("_flash", []).append((kind, md(message)))


def drain_flash() -> None:
    for kind, message in st.session_state.pop("_flash", []):
        getattr(st, kind)(message)


# --------------------------------------------------------------------------
# Watchlist management
# --------------------------------------------------------------------------


def add_stock(ticker: str) -> None:
    ticker = ticker.strip().upper()
    if not ticker:
        return
    db.add_watchlist_item(ticker, "stock", ticker)
    flash("success", f"Added {ticker}. It gets a price on the collector's next cycle.")


def add_coingecko(coin_id: str, asset_class: str) -> None:
    """Validate against CoinGecko before adding — ids are slugs, not tickers."""
    coin_id = coin_id.strip().lower()
    if not coin_id:
        return
    quote = crypto_source.request_json(
        f"{crypto_source.CG_BASE}/simple/price",
        source=crypto_source.SOURCE,
        params={"ids": coin_id, "vs_currencies": "usd"},
        headers=crypto_source._cg_headers(),
    )
    if not quote or coin_id not in quote:
        flash(
            "error",
            f"CoinGecko has no coin with id '{coin_id}'. Ids are slugs "
            "(`shiba-inu`), not tickers (`SHIB`).",
        )
        return
    db.add_watchlist_item(coin_id.upper(), asset_class, coin_id, coin_id)
    flash("success", f"Added {coin_id}.")


def add_dex_pair(chain: str, pair_address: str) -> None:
    chain = chain.strip().lower()
    pair_address = pair_address.strip()
    if not chain or not pair_address:
        return
    pair = meme_source.lookup_pair(chain, pair_address)
    if not pair:
        flash("error", f"DexScreener has no pair {chain}:{pair_address}.")
        return
    base = pair.get("baseToken") or {}
    symbol = db.dex_symbol(base.get("symbol") or "?", pair.get("pairAddress") or pair_address)
    created = db.from_epoch_ms(pair.get("pairCreatedAt"))
    db.add_watchlist_item(
        symbol,
        "meme",
        base.get("name") or symbol,
        f"{pair.get('chainId')}:{pair.get('pairAddress')}",
        db.iso(created) if created else None,
    )
    flash("success", f"Added {symbol} ({base.get('name')}).")


def render_watchlist_manager(asset_class: str, entries: list[dict]) -> None:
    with st.expander("Manage watchlist"):
        add_col, remove_col = st.columns(2)

        with add_col:
            st.markdown("**Add**")
            if asset_class == "stock":
                ticker = st.text_input("Ticker", key=f"add_{asset_class}", placeholder="MSFT")
                if st.button("Add ticker", key=f"addbtn_{asset_class}"):
                    add_stock(ticker)
                    refresh_data()
                    st.rerun()
            elif asset_class == "crypto":
                coin_id = st.text_input(
                    "CoinGecko coin id", key=f"add_{asset_class}", placeholder="cardano",
                    help="CoinGecko keys by coin id (a slug), not ticker.",
                )
                if st.button("Add coin", key=f"addbtn_{asset_class}"):
                    add_coingecko(coin_id, "crypto")
                    refresh_data()
                    st.rerun()
            else:
                mode = st.radio(
                    "Source", ["CoinGecko id", "DexScreener pair"],
                    key=f"mode_{asset_class}", horizontal=True,
                )
                if mode == "CoinGecko id":
                    coin_id = st.text_input(
                        "CoinGecko coin id", key=f"add_{asset_class}", placeholder="bonk"
                    )
                    if st.button("Add coin", key=f"addbtn_{asset_class}"):
                        add_coingecko(coin_id, "meme")
                        refresh_data()
                        st.rerun()
                else:
                    chain = st.text_input("Chain", key=f"chain_{asset_class}", placeholder="solana")
                    pair_address = st.text_input(
                        "Pair address", key=f"pair_{asset_class}", placeholder="FCEnSxy..."
                    )
                    if st.button("Add pair", key=f"addbtn_dex_{asset_class}"):
                        add_dex_pair(chain, pair_address)
                        refresh_data()
                        st.rerun()

        with remove_col:
            st.markdown("**Remove**")
            if entries:
                target = st.selectbox(
                    "Symbol", [e["symbol"] for e in entries], key=f"rm_{asset_class}"
                )
                if st.button("Remove", key=f"rmbtn_{asset_class}"):
                    db.remove_watchlist_item(target)
                    flash("info", f"Removed {target} from the watchlist.")
                    refresh_data()
                    st.rerun()
                st.caption(
                    "Removing stops collection. Price history, trades, and any "
                    "open position are kept."
                )
            else:
                st.caption("Nothing to remove.")


# --------------------------------------------------------------------------
# Trade ticket
# --------------------------------------------------------------------------


def render_trade_form(asset_class: str, entries: list[dict], snapshots: dict) -> None:
    """Market-order ticket. Fills at the latest stored snapshot price."""
    tradable = [e["symbol"] for e in entries if e["symbol"] in snapshots]
    if not tradable:
        st.caption("No priced symbols to trade yet — waiting on the collector.")
        return

    st.markdown("**Place a paper order**")
    cols = st.columns([2, 1, 2, 2])
    symbol = cols[0].selectbox("Symbol", tradable, key=f"trade_sym_{asset_class}")
    side = cols[1].radio("Side", ["Buy", "Sell"], key=f"trade_side_{asset_class}")
    quantity = cols[2].number_input(
        "Quantity", min_value=0.0, value=1.0, step=1.0,
        format="%.8f", key=f"trade_qty_{asset_class}",
    )

    price = snapshots.get(symbol, {}).get("price")
    position = portfolio.get_position(symbol)
    held = position["quantity"] if position else 0.0
    notional = (price or 0) * quantity
    account = portfolio.get_account()

    with cols[3]:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        submitted = st.button(
            f"{side} {symbol}", key=f"trade_go_{asset_class}", width="stretch"
        )

    st.caption(
        md(
            f"Fill price {fmt_price(price)} · order value **${notional:,.2f}** · "
            f"cash ${account['cash_balance']:,.2f} · holding {held:g} {symbol}"
        )
    )

    if submitted:
        try:
            result = portfolio.execute_trade(symbol, asset_class, side.lower(), quantity)
        except portfolio.TradeError as exc:
            flash("error", str(exc))
        else:
            detail = (
                f" · realized PnL ${result['realized_pnl']:,.2f}"
                if result["realized_pnl"] is not None
                else ""
            )
            flash(
                "success",
                f"{side} {result['quantity']:g} {symbol} at "
                f"{fmt_price(result['price'])}{detail}",
            )
        refresh_data()
        st.rerun()


# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------


def render_asset_tab(asset_class: str, label: str, snapshots: dict) -> None:
    entries = load_watchlist(asset_class)
    st.subheader(f"{label} watchlist")
    render_watchlist(entries, snapshots, asset_class)
    render_watchlist_manager(asset_class, entries)
    st.divider()
    render_trade_form(asset_class, entries, snapshots)


# --------------------------------------------------------------------------
# Portfolio tab
# --------------------------------------------------------------------------

FILTER_LABELS = {"All": None, "Stocks": "stock", "Crypto": "crypto", "Meme": "meme"}


def positions_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Symbol": r["symbol"],
                "Class": r["asset_class"],
                "Quantity": f"{r['quantity']:g}",
                "Avg cost": fmt_price(r["avg_cost_basis"]),
                "Price": fmt_price(r["current_price"]),
                "Cost basis": f"${r['cost_basis_total']:,.2f}",
                "Value": f"${r['market_value']:,.2f}" if r["market_value"] is not None else DASH,
                "Unrealized": f"${r['unrealized_pnl']:,.2f}" if r["unrealized_pnl"] is not None else DASH,
                "Unrealized %": fmt_pct(r["unrealized_pct"]),
            }
            for r in rows
        ]
    )


def trades_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "When": fmt_when(t["executed_at"]),
                "Symbol": t["symbol"],
                "Class": t["asset_class"],
                "Side": t["side"].upper(),
                "Quantity": f"{t['quantity']:g}",
                "Price": fmt_price(t["price"]),
                "Value": f"${t['quantity'] * t['price']:,.2f}",
                "Realized PnL": f"${t['realized_pnl']:,.2f}" if t["realized_pnl"] is not None else DASH,
            }
            for t in rows
        ]
    )


def render_portfolio_tab(snapshots: dict) -> None:
    st.subheader("Paper portfolio")

    choice = st.radio(
        "Asset class", list(FILTER_LABELS), horizontal=True, key="pf_filter"
    )
    asset_class = FILTER_LABELS[choice]

    prices = {s: v["price"] for s, v in snapshots.items() if v.get("price") is not None}
    summary = portfolio.portfolio_summary(prices, asset_class)

    cols = st.columns(4)
    if asset_class is None:
        cols[0].metric("Cash", f"${summary['cash_balance']:,.2f}")
        cols[1].metric("Total value", f"${summary['total_value']:,.2f}")
        cols[2].metric(
            "Total PnL",
            f"${summary['total_pnl']:,.2f}",
            f"{summary['total_pnl_pct']:+.2f}%",
        )
        cols[3].metric("Realized", f"${summary['realized_pnl']:,.2f}")
    else:
        cols[0].metric(f"{choice} holdings", f"${summary['holdings_value']:,.2f}")
        cols[1].metric("Unrealized", f"${summary['unrealized_pnl']:,.2f}")
        cols[2].metric("Realized", f"${summary['realized_pnl']:,.2f}")
        cols[3].metric(
            f"{choice} PnL",
            f"${summary['total_pnl']:,.2f}",
            f"{summary['total_pnl_pct']:+.2f}%",
        )
        st.caption(
            md(
                f"Cash (${summary['cash_balance']:,.2f}) is one shared balance "
                "across all classes, so it is not split by this filter."
            )
        )

    if summary["missing_prices"]:
        st.warning(
            "No current price for "
            f"{', '.join(summary['missing_prices'])} — those lines are valued at "
            "cost and excluded from PnL."
        )

    st.markdown("#### Positions")
    if summary["positions"]:
        st.dataframe(positions_frame(summary["positions"]), width="stretch", hide_index=True)
    else:
        st.caption("No open positions.")

    st.markdown("#### Trade history")
    trades = portfolio.get_trades(asset_class)
    if trades:
        st.dataframe(trades_frame(trades), width="stretch", hide_index=True)
    else:
        st.caption("No trades yet.")

    with st.expander("Reset paper account"):
        st.caption(
            md(
                "Clears every position, trade, and equity-curve point, and restores "
                f"the ${summary['starting_balance']:,.2f} starting balance. "
                "Not undoable."
            )
        )
        if st.button("Reset account", type="secondary"):
            portfolio.reset_account()
            flash("info", "Paper account reset to its starting balance.")
            refresh_data()
            st.rerun()


def main() -> None:
    db.init_db()
    snapshots = load_snapshots()
    render_sidebar(snapshots)

    st.title("Trading Insights")
    st.caption(
        "Market data, news, and social signal for research and paper practice. "
        "Numbers are inputs — no recommendations are produced here."
    )

    drain_flash()

    tabs = st.tabs([label for _, label in ASSET_TABS] + ["Portfolio"])
    for tab, (asset_class, label) in zip(tabs, ASSET_TABS):
        with tab:
            render_asset_tab(asset_class, label, snapshots)
    with tabs[-1]:
        render_portfolio_tab(snapshots)


main()

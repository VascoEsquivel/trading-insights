"""Trading Insights — Streamlit entrypoint.

Reads only from SQLite. The collector (`python -m collector.scheduler`) is a
separate process and is the only writer; no background threads are started here,
because Streamlit reruns this script top-to-bottom on every interaction and
would duplicate or orphan them.
"""
from __future__ import annotations

import html as html_lib
import logging
import time
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config
from collector import crypto as crypto_source
from collector import discovery
from collector import memecoins as meme_source
from collector import stocks as stock_source
from quant import live as quant_live
from trading import db, portfolio, signals

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


@st.cache_data(ttl=TTL)
def load_news(symbols: tuple[str, ...], limit: int = 40):
    return db.get_news(list(symbols), limit)


@st.cache_data(ttl=TTL)
def load_social(symbols: tuple[str, ...]):
    """Latest window plus the trailing baseline it should be read against."""
    return {
        symbol: {
            "latest": db.latest_social(symbol),
            "baseline": db.social_baseline(symbol),
        }
        for symbol in symbols
    }


def refresh_data() -> None:
    st.cache_data.clear()


# Chart sources are cached far longer than tables: yfinance is scraped, and
# every CoinGecko OHLC call eats into the 10k/month cap.
@st.cache_data(ttl=300, show_spinner=False)
def load_stock_candles(symbol: str, period: str, interval: str):
    return stock_source.fetch_candles(symbol, period, interval)


@st.cache_data(ttl=900, show_spinner=False)
def load_crypto_ohlc(coin_id: str, days: int):
    return crypto_source.fetch_ohlc(coin_id, days)


@st.cache_data(ttl=TTL)
def load_snapshot_history(symbol: str, hours: int):
    return db.snapshot_history(symbol, hours)


@st.cache_data(ttl=TTL)
def load_equity_curve():
    return portfolio.get_equity_curve()


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


def fmt_compact(value: float | None, prefix: str = "$") -> str:
    if value is None:
        return DASH
    magnitude = abs(value)
    for cutoff, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if magnitude >= cutoff:
            return f"{prefix}{value / cutoff:,.2f}{suffix}"
    return f"{prefix}{value:,.2f}"


def fmt_shares(value: float | None) -> str:
    """Stock volume is a share count, not dollars — no currency prefix."""
    return fmt_compact(value, prefix="")


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
    """Controls only.

    Freshness lives in the hero chip, which is always on screen; repeating it
    here as a coloured banner was the loudest thing in the sidebar and said
    nothing the header did not.
    """
    with st.sidebar:
        st.markdown("### Trading Insights")

        newest = newest_snapshot_time(snapshots)
        if db.from_iso(newest) is None:
            st.error(
                "No price data yet. Start the collector:\n\n"
                "`python -m collector.scheduler`"
            )

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
            "Paper trading only. No brokerage or exchange is ever linked, and "
            "nothing here is advice."
        )
        with st.expander("Collector settings"):
            st.caption(
                f"Stock quotes every {config.STOCK_QUOTE_INTERVAL}s  \n"
                f"Crypto prices every {config.CRYPTO_PRICE_INTERVAL}s  \n"
                f"Meme pairs every {config.MEME_PAIR_INTERVAL}s  \n"
                f"Reddit sentiment: {'on' if config.ENABLE_REDDIT else 'off'}"
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


@st.cache_data(show_spinner=False)
def _read_stylesheet(path: str, mtime: float) -> str:
    """mtime is part of the cache key so editing the CSS shows up on reload."""
    del mtime
    try:
        return f"<style>{open(path, encoding='utf-8').read()}</style>"
    except OSError:
        return ""  # cosmetic only — the app is fully usable unstyled


def load_stylesheet() -> str:
    path = config.BASE_DIR / "assets" / "app.css"
    try:
        return _read_stylesheet(str(path), path.stat().st_mtime)
    except OSError:
        return ""


# Columns whose sign should be coloured, and columns that render as pills.
SIGNED_COLUMNS = {"24h", "Unrealized", "Unrealized %", "Realized PnL"}
NUMERIC_COLUMNS = {
    "Price", "24h", "Volume 24h", "Liquidity", "Market cap", "Age", "Quantity",
    "Avg cost", "Cost basis", "Value", "Unrealized", "Unrealized %",
    "Realized PnL", "Updated", "When",
}


def _signed_cell(text: str) -> str:
    """Colour a signed number without inventing a judgement about it."""
    if text.startswith("+"):
        return f"<span class='ti-up'>{text}</span>"
    if text.startswith("-") or text.startswith("$-"):
        return f"<span class='ti-down'>{text}</span>"
    return text


def _risk_cell(text: str) -> str:
    if text == DASH:
        return text
    return " ".join(
        f"<span class='ti-pill {flag}'>{flag}</span>"
        for flag in (f.strip() for f in text.split("·"))
        if flag in ("thin", "new")
    )


def render_table(frame: pd.DataFrame) -> None:
    """Static HTML table.

    Streamlit's dataframe is a canvas grid that measures zero width inside a
    tab that is hidden on first paint, so tables in every tab but the first
    render collapsed and stay collapsed across tab switches. Every cell here is
    already a formatted string, so a plain table loses nothing — and it avoids
    offering a sort that would order "$1.30T" before "$922.11K" lexicographically.

    Cell values include DexScreener token names, which are attacker-controlled,
    so everything is HTML-escaped before any decoration is applied.
    """
    if frame.empty:
        return
    head = "".join(
        f"<th class='{'num' if c in NUMERIC_COLUMNS else ''}'>{html_lib.escape(str(c))}</th>"
        for c in frame.columns
    )
    body = []
    for _, row in frame.iterrows():
        cells = []
        for column in frame.columns:
            raw = "" if row[column] is None else str(row[column])
            text = html_lib.escape(raw)
            if column in SIGNED_COLUMNS:
                text = _signed_cell(text)
            elif column == "Risk":
                text = _risk_cell(text)
            classes = []
            if column in NUMERIC_COLUMNS:
                classes.append("num")
            if column == "Symbol":
                classes.append("sym")
            cells.append(f"<td class='{' '.join(classes)}'>{text}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")

    st.markdown(
        f"<div class='ti-wrap'><table class='ti-table'>"
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>",
        unsafe_allow_html=True,
    )


def risk_flags(entry: dict, snap: dict) -> str:
    """"thin" under $50k liquidity, "new" under 24h old.

    These are the two numbers that separate a real move from an easily
    manipulated one, so they are computed for every meme row, not on request.
    """
    flags = []
    liquidity = snap.get("liquidity_usd")
    if liquidity is not None and liquidity < config.THIN_LIQUIDITY_USD:
        flags.append("thin")
    age = age_hours(entry.get("pair_created_at"))
    if age is not None and age < config.NEW_TOKEN_AGE_HOURS:
        flags.append("new")
    return " · ".join(flags) if flags else DASH


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
            "Volume 24h": (
                fmt_shares(snap.get("volume")) if asset_class == "stock"
                else fmt_compact(snap.get("volume"))
            ),
        }
        if asset_class == "meme":
            # Risk context is pinned next to price, per the ground rules.
            row["Liquidity"] = fmt_compact(snap.get("liquidity_usd"))
            row["Market cap"] = fmt_compact(snap.get("market_cap"))
            row["Age"] = fmt_age(entry.get("pair_created_at"))
            row["Risk"] = risk_flags(entry, snap)
        row["Updated"] = fmt_when(snap.get("fetched_at"))
        rows.append(row)
    return pd.DataFrame(rows)


def render_watchlist(entries: list[dict], snapshots: dict, asset_class: str) -> None:
    if not entries:
        st.info("Nothing on this watchlist yet — add a symbol below.")
        return
    table = build_table(entries, snapshots, asset_class)
    render_table(table)

    if asset_class == "meme":
        st.caption(
            md(
                f"**Risk** flags a pool under ${config.THIN_LIQUIDITY_USD:,.0f} "
                f"liquidity as `thin` and a pair under "
                f"{config.NEW_TOKEN_AGE_HOURS}h old as `new`. Liquidity and age "
                "come from the DEX pair, so they are blank for coins priced via "
                "CoinGecko (DOGE, SHIB, PEPE), which trade mainly on exchanges."
            )
        )

    missing = [e["symbol"] for e in entries if e["symbol"] not in snapshots]
    if missing:
        st.caption(
            f"No snapshot yet for {', '.join(missing)} — "
            "the collector picks new symbols up on its next cycle."
        )


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------

CANDLE_UP = "#34d399"
CANDLE_DOWN = "#fb7185"
ACCENT = "#22d3ee"
ACCENT_ALT = "#a78bfa"
GRID = "rgba(148,163,184,0.10)"
MUTED = "#8b98ab"


def dark_layout(fig: go.Figure, height: int) -> go.Figure:
    """Match the charts to the page instead of Plotly's white default card."""
    fig.update_layout(
        template="plotly_dark",
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=MUTED, size=11),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#161f2e", bordercolor="rgba(148,163,184,0.28)"),
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False, title=None, showspikes=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, title=None)
    return fig


def style_chart(fig: go.Figure, title: str) -> go.Figure:
    dark_layout(fig, 420)
    fig.update_layout(
        title=dict(text=title, font=dict(size=13, color="#e6edf3"), x=0.01, y=0.97),
        margin=dict(l=10, r=10, t=44, b=10),
        xaxis_rangeslider_visible=False,
        showlegend=False,
    )
    fig.update_yaxes(tickformat=".8~g")
    return fig


def candlestick(frame: pd.DataFrame, title: str) -> go.Figure:
    fig = go.Figure(
        go.Candlestick(
            x=frame["t"],
            open=frame["open"],
            high=frame["high"],
            low=frame["low"],
            close=frame["close"],
            increasing_line_color=CANDLE_UP,
            decreasing_line_color=CANDLE_DOWN,
        )
    )
    return style_chart(fig, title)


def render_stock_chart(symbol: str) -> None:
    period, interval = st.session_state.get(
        f"range_{symbol}", ("1mo", "1d")
    )
    hist = load_stock_candles(symbol, period, interval)
    if hist is None or len(hist) == 0:
        st.warning(
            f"No candles for {symbol} right now. yfinance scrapes Yahoo and "
            "occasionally breaks — the rest of the tab is unaffected."
        )
        return
    frame = pd.DataFrame(
        {
            "t": hist.index,
            "open": hist["Open"].to_numpy(),
            "high": hist["High"].to_numpy(),
            "low": hist["Low"].to_numpy(),
            "close": hist["Close"].to_numpy(),
        }
    )
    st.plotly_chart(candlestick(frame, f"{symbol} · {period} · yfinance"), width="stretch")


def render_coingecko_chart(symbol: str, coin_id: str, days: int) -> None:
    raw = load_crypto_ohlc(coin_id, days)
    if not raw:
        st.warning(f"CoinGecko returned no OHLC for {coin_id}.")
        return
    frame = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close"])
    frame["t"] = pd.to_datetime(frame["ts"], unit="ms", utc=True)
    st.plotly_chart(
        candlestick(frame, f"{symbol} · {days}d · CoinGecko OHLC"), width="stretch"
    )


def render_snapshot_chart(symbol: str, hours: int) -> None:
    """DexScreener exposes no history, so this is drawn from what we collected."""
    history = load_snapshot_history(symbol, hours)
    points = [h for h in history if h.get("price") is not None]
    if len(points) < 2:
        st.info(
            f"Only {len(points)} price point stored for {symbol}. DexScreener "
            "publishes no historical OHLC, so this chart fills in as the "
            f"collector runs (every {config.MEME_PAIR_INTERVAL}s)."
        )
        return
    frame = pd.DataFrame(
        {
            "t": pd.to_datetime([p["fetched_at"] for p in points], utc=True, format="ISO8601"),
            "price": [p["price"] for p in points],
        }
    )
    fig = go.Figure(
        go.Scatter(x=frame["t"], y=frame["price"], mode="lines",
                   line=dict(width=2, color=ACCENT),
                   fill="tozeroy", fillcolor="rgba(34,211,238,0.07)")
    )
    st.plotly_chart(
        style_chart(fig, f"{symbol} · last {hours}h · collected snapshots"),
        width="stretch",
    )


STOCK_RANGES = {
    "5d (15m)": ("5d", "15m"),
    "1mo (1d)": ("1mo", "1d"),
    "3mo (1d)": ("3mo", "1d"),
    "1y (1d)": ("1y", "1d"),
}
CG_RANGES = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}
SNAPSHOT_RANGES = {"6h": 6, "24h": 24, "48h": 48}


def render_chart_section(asset_class: str, entries: list[dict]) -> None:
    if not entries:
        return
    st.markdown("#### Chart")
    pick_col, range_col = st.columns([2, 1])
    symbol = pick_col.selectbox(
        "Symbol", [e["symbol"] for e in entries],
        key=f"chart_sym_{asset_class}", label_visibility="collapsed",
    )
    entry = next(e for e in entries if e["symbol"] == symbol)

    if asset_class == "stock":
        label = range_col.selectbox(
            "Range", list(STOCK_RANGES), index=1,
            key=f"chart_rng_{asset_class}", label_visibility="collapsed",
        )
        period, interval = STOCK_RANGES[label]
        st.session_state[f"range_{symbol}"] = (period, interval)
        render_stock_chart(symbol)
    elif db.is_dex_source(entry.get("source_id")):
        label = range_col.selectbox(
            "Range", list(SNAPSHOT_RANGES), index=1,
            key=f"chart_rng_{asset_class}", label_visibility="collapsed",
        )
        render_snapshot_chart(symbol, SNAPSHOT_RANGES[label])
    elif entry.get("source_id"):
        label = range_col.selectbox(
            "Range", list(CG_RANGES), index=1,
            key=f"chart_rng_{asset_class}", label_visibility="collapsed",
        )
        render_coingecko_chart(symbol, entry["source_id"], CG_RANGES[label])
    else:
        st.caption("No chart source for this symbol.")


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

    st.markdown("#### Buy / sell")
    # Side needs real width or the radio labels wrap one letter per line.
    cols = st.columns([2.2, 1.5, 2, 1.8])
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


def render_quick_buy(
    symbol: str,
    asset_class: str,
    price: float | None,
    key: str,
    name: str | None = None,
    source_id: str | None = None,
    pair_created_at: str | None = None,
) -> None:
    """Buy a name from wherever you happen to be reading about it.

    Discover and Recommended surface tickers that are not on the watchlist and
    so have no stored snapshot, which is what execute_trade normally prices
    against. The live price from the scan is passed explicitly instead, and the
    symbol is added to the watchlist on the way through — owning something the
    collector isn't tracking would leave the position unpriceable afterwards.
    """
    if price is None or price <= 0:
        st.caption("No live price for this symbol, so it can't be traded here.")
        return

    account = portfolio.get_account()
    position = portfolio.get_position(symbol)
    held = position["quantity"] if position else 0.0

    qty_col, buy_col, info_col = st.columns([1.2, 1, 3])
    quantity = qty_col.number_input(
        "Quantity", min_value=0.0, value=1.0, step=1.0,
        format="%.4f", key=f"qb_qty_{key}", label_visibility="collapsed",
    )
    with buy_col:
        clicked = st.button(
            f"Buy {symbol}", key=f"qb_go_{key}", width="stretch", type="primary"
        )
    info_col.caption(
        md(
            f"at {fmt_price(price)} · costs **${price * quantity:,.2f}** · "
            f"cash ${account['cash_balance']:,.2f}"
            + (f" · already holding {held:g}" if held else "")
        )
    )

    if not clicked:
        return
    try:
        db.add_watchlist_item(symbol, asset_class, name or symbol, source_id, pair_created_at)
        # Seed a snapshot at the fill price so the position is marked to market
        # straight away, instead of waiting for the collector's next cycle.
        db.insert_price_snapshots([{
            "symbol": symbol, "asset_class": asset_class, "price": price,
            "volume": None, "pct_change_24h": None, "liquidity_usd": None,
            "market_cap": None, "fetched_at": db.iso_now(),
        }])
        result = portfolio.execute_trade(
            symbol, asset_class, "buy", quantity, price=price
        )
    except portfolio.TradeError as exc:
        flash("error", str(exc))
    else:
        flash(
            "success",
            f"Bought {result['quantity']:g} {symbol} at {fmt_price(result['price'])}. "
            "Added to your watchlist so it stays priced — see the Portfolio tab.",
        )
    refresh_data()
    st.rerun()


# --------------------------------------------------------------------------
# Signal desk
# --------------------------------------------------------------------------


@st.cache_data(ttl=180, show_spinner=False)
def load_readout(symbol: str, _entry: dict, _snapshot: dict | None):
    """Cached because it pulls 6mo of history per symbol.

    The underscore-prefixed args are excluded from the cache key by Streamlit;
    `symbol` plus the 180s TTL is the key.
    """
    return signals.analyze(_entry, _snapshot)


STANCE_ICON = {"supportive": "▲", "cautionary": "▼", "neutral": "●"}
STANCE_CLASS = {"supportive": "ti-up", "cautionary": "ti-down", "neutral": ""}


def render_factor_grid(factors) -> None:
    """Supporting / Against / Context, each factor carrying its own number."""
    buckets = [
        ("Supporting", [f for f in factors if f.stance == "supportive"], "ti-up"),
        ("Against", [f for f in factors if f.stance == "cautionary"], "ti-down"),
        ("Context", [f for f in factors if f.stance == "neutral"], ""),
    ]
    blocks = []
    for title, items, css in buckets:
        if items:
            rows = "".join(
                f"<div class='ti-factor'><span class='ti-fmark {STANCE_CLASS[i.stance]}'>"
                f"{STANCE_ICON[i.stance]}</span><div><b>{html_lib.escape(i.label)}</b>"
                f"<div class='ti-fdetail'>{html_lib.escape(i.detail)}</div></div></div>"
                for i in items
            )
        else:
            rows = "<div class='ti-fempty'>Nothing in this column.</div>"
        blocks.append(
            f"<div class='ti-fcol'><div class='ti-fhead {css}'>{title} · {len(items)}</div>"
            f"{rows}</div>"
        )
    st.markdown(f"<div class='ti-fgrid'>{''.join(blocks)}</div>", unsafe_allow_html=True)


def render_signal_desk(asset_class: str, entries: list[dict], snapshots: dict) -> None:
    priced = [e for e in entries if e["symbol"] in snapshots]
    if not priced:
        return

    st.markdown("#### Signal desk")

    movers = [m for m in signals.scan(asset_class)]
    if movers:
        chips = "".join(
            f"<span class='ti-chip'><b>{html_lib.escape(m['symbol'])}</b> "
            f"<span class='{'ti-up' if m['change_24h'] >= 0 else 'ti-down'}'>"
            f"{m['change_24h']:+.2f}%</span></span>"
            for m in movers[:6]
        )
        st.markdown(
            f"<div class='ti-chips' style='margin-bottom:.7rem'>{chips}</div>",
            unsafe_allow_html=True,
        )

    default = movers[0]["symbol"] if movers else priced[0]["symbol"]
    options = [e["symbol"] for e in priced]
    symbol = st.selectbox(
        "Analyse",
        options,
        index=options.index(default) if default in options else 0,
        key=f"signal_sym_{asset_class}",
        label_visibility="collapsed",
    )
    entry = next(e for e in priced if e["symbol"] == symbol)

    with st.spinner("Reading the tape…"):
        read = load_readout(symbol, entry, snapshots.get(symbol))

    stats = [
        ("Price", fmt_price(read.price)),
        ("24h", fmt_pct(read.change_24h)),
        ("Move vs. typical", f"{abs(read.sigma_move):.1f}x" if read.sigma_move is not None else DASH),
        ("Volume vs. avg", f"{read.volume_ratio:.2f}x" if read.volume_ratio is not None else DASH),
        ("vs. 20-period avg", f"{read.ma20_gap:+.1f}%" if read.ma20_gap is not None else DASH),
        ("RSI(14)", f"{read.rsi14:.0f}" if read.rsi14 is not None else DASH),
    ]
    st.markdown(
        "<div class='ti-statgrid'>"
        + "".join(
            f"<div class='ti-stat'><span class='k'>{html_lib.escape(k)}</span>"
            f"<span class='v'>{html_lib.escape(v)}</span></div>"
            for k, v in stats
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    tone = "ti-up" if (read.change_24h or 0) >= 0 else "ti-down"
    st.markdown(
        f"<div class='ti-read-headline {tone}'>{html_lib.escape(read.headline)}</div>"
        f"<div class='ti-read-note'>{html_lib.escape(read.history_note)}</div>",
        unsafe_allow_html=True,
    )

    render_factor_grid(read.factors)

    if read.news:
        with st.expander(f"The {len(read.news)} headlines this read used"):
            st.markdown(news_html(read.news[:12]), unsafe_allow_html=True)

    st.caption(
        "This weighs up evidence — it is not a recommendation, and none of these "
        "factors predicts what happens next. A well-explained move is still just "
        "a move that has been explained."
    )


# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------


def tone_label(score: float | None) -> tuple[str, str]:
    """(text, css class) for a VADER compound score. Descriptive, not a call."""
    if score is None:
        return ("", "")
    if score >= 0.35:
        return (f"{score:+.2f} positive", "ti-up")
    if score >= 0.05:
        return (f"{score:+.2f} mildly positive", "ti-up")
    if score <= -0.35:
        return (f"{score:+.2f} negative", "ti-down")
    if score <= -0.05:
        return (f"{score:+.2f} mildly negative", "ti-down")
    return (f"{score:+.2f} neutral", "")


def news_html(items: list[dict], show_tone: bool = False) -> str:
    """Headline list. Values are third-party text, so everything is escaped."""
    rows = []
    for item in items:
        url = html_lib.escape(item.get("url") or "", quote=True)
        extra = ""
        if show_tone:
            text, css = tone_label(item.get("tone"))
            if text:
                extra = f"<span class='{css}'>{html_lib.escape(text)}</span>"
        rows.append(
            "<div class='ti-news-item'>"
            f"<a href='{url}' target='_blank' rel='noopener noreferrer'>"
            f"{html_lib.escape(item['headline'])}</a>"
            "<div class='ti-news-meta'>"
            f"<span class='tag'>{html_lib.escape(item['symbol'])}</span>"
            f"<span>{html_lib.escape(item.get('source') or '?')}</span>"
            f"<span>{html_lib.escape(fmt_when(item.get('published_at')))}</span>"
            f"{extra}"
            "</div></div>"
        )
    return f"<div class='ti-news'>{''.join(rows)}</div>"


def render_news(entries: list[dict]) -> None:
    symbols = [e["symbol"] for e in entries]
    if not symbols:
        return

    st.markdown("#### News")
    chosen = st.multiselect(
        "Filter by symbol",
        symbols,
        default=symbols,
        key=f"news_filter_{entries[0]['asset_class']}",
        label_visibility="collapsed",
    )
    if not chosen:
        st.caption("Select at least one symbol.")
        return

    items = load_news(tuple(chosen))
    if not items:
        st.caption(
            "No headlines stored yet for these symbols. The collector pulls "
            "company news and crypto RSS every 15 minutes."
        )
        return

    st.markdown(news_html(items[:20]), unsafe_allow_html=True)


# --------------------------------------------------------------------------
# Sentiment
# --------------------------------------------------------------------------


def polarity_label(score: float | None) -> str:
    """Describe the tone of matched titles. Descriptive only — not a call."""
    if score is None:
        return "no score"
    if score >= 0.05:
        return f"positive tone {score:+.2f}"
    if score <= -0.05:
        return f"negative tone {score:+.2f}"
    return f"neutral tone {score:+.2f}"


def trend_marker(count: int, baseline: float | None) -> str:
    if baseline is None or baseline <= 0:
        return "no baseline yet"
    delta = (count - baseline) / baseline * 100
    arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "▬")
    return f"{arrow} {delta:+.0f}% vs. trailing 24h average of {baseline:.1f}"


def render_sentiment(entries: list[dict]) -> None:
    """Mention counts and polarity. Inputs, not verdicts — no buy/sell language."""
    if not config.ENABLE_REDDIT:
        return

    st.markdown("#### Reddit mentions")
    data = load_social(tuple(e["symbol"] for e in entries))
    shown = 0
    for entry in entries:
        record = data.get(entry["symbol"]) or {}
        latest = record.get("latest")
        if not latest:
            continue
        shown += 1
        st.markdown(
            "<div class='ti-senti'><div class='ti-senti-top'>"
            f"<span class='ti-senti-sym'>{html_lib.escape(entry['symbol'])}</span>"
            f"<span class='ti-senti-count'>{latest['mention_count']}</span></div>"
            "<div class='ti-senti-meta'>"
            f"{html_lib.escape(trend_marker(latest['mention_count'], record.get('baseline')))}"
            "</div>"
            "<div class='ti-senti-meta'>"
            f"{html_lib.escape(polarity_label(latest.get('sentiment_score')))} · "
            f"window ended {html_lib.escape(fmt_when(latest['window_end']))}</div></div>",
            unsafe_allow_html=True,
        )
    if not shown:
        st.caption("No mentions recorded yet in the scanned subreddits.")
    else:
        st.caption(
            "Counts match on ticker, cashtag, and coin name, so tokens sharing a "
            "ticker share a count. Raw signal for context only."
        )


# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------


def render_asset_tab(asset_class: str, label: str, snapshots: dict) -> None:
    entries = load_watchlist(asset_class)
    st.markdown("#### Watchlist")
    render_watchlist(entries, snapshots, asset_class)
    st.divider()
    # Directly under the watchlist: it was below the chart and signal desk,
    # which put the one action on the page three scrolls from the prices.
    render_trade_form(asset_class, entries, snapshots)
    st.divider()
    render_signal_desk(asset_class, entries, snapshots)
    st.divider()
    render_chart_section(asset_class, entries)
    st.divider()
    news_col, social_col = st.columns([3, 2])
    with news_col:
        render_news(entries)
    with social_col:
        render_sentiment(entries)
    st.divider()
    # Editing the watchlist is an occasional action, not something you read —
    # it sat between the table and the analysis and broke the flow.
    render_watchlist_manager(asset_class, entries)


# --------------------------------------------------------------------------
# Discover tab — candidates from outside the watchlist
# --------------------------------------------------------------------------


@st.cache_data(ttl=300, show_spinner=False)
def load_stock_screen(screen: str):
    return signals.rank_candidates(discovery.screen_stocks(screen, count=30))


@st.cache_data(ttl=300, show_spinner=False)
def load_crypto_candidates(mode: str):
    rows = (
        discovery.trending_crypto()
        if mode == "Trending searches"
        else discovery.crypto_movers(top=25)
    )
    return signals.rank_candidates(rows)


@st.cache_data(ttl=300, show_spinner=False)
def load_meme_candidates():
    return signals.rank_candidates(discovery.meme_candidates(limit=15))


@st.cache_data(ttl=300, show_spinner=False)
def deep_read(symbol: str, _candidate: dict):
    """Full analysis of an off-watchlist candidate: fetch its news, then read it."""
    discovery.prime_news(_candidate)
    entry = {
        "symbol": _candidate["symbol"],
        "name": _candidate.get("name"),
        "asset_class": _candidate["asset_class"],
        "source_id": _candidate.get("source_id"),
        "pair_created_at": _candidate.get("pair_created_at"),
    }
    snapshot = {
        "price": _candidate.get("price"),
        "pct_change_24h": _candidate.get("change_24h"),
        "volume": _candidate.get("volume"),
        "liquidity_usd": _candidate.get("liquidity_usd"),
    }
    return signals.analyze(entry, snapshot)


def candidate_table(rows: list[dict], asset_class: str) -> pd.DataFrame:
    table = []
    for r in rows:
        row = {
            "Symbol": r["symbol"],
            "Name": (r.get("name") or "")[:40],
            "Price": fmt_price(r.get("price")),
            "24h": fmt_pct(r.get("change_24h")),
            "Volume 24h": (
                fmt_shares(r.get("volume")) if asset_class == "stock"
                else fmt_compact(r.get("volume"))
            ),
            "Market cap": fmt_compact(r.get("market_cap")),
        }
        if asset_class == "stock":
            ratio = r.get("volume_ratio")
            row["Vol vs avg"] = f"{ratio:.2f}x" if ratio else DASH
        if asset_class == "meme":
            row["Liquidity"] = fmt_compact(r.get("liquidity_usd"))
            row["Risk"] = risk_flags(
                {"pair_created_at": r.get("pair_created_at")},
                {"liquidity_usd": r.get("liquidity_usd")},
            )
        row["Evidence"] = f"+{r['supportive']} / -{r['cautionary']}"
        table.append(row)
    return pd.DataFrame(table)


MAX_CANDIDATE_ROWS = 20


def render_candidate_rows(rows: list[dict], asset_class: str, watched: set[str]) -> None:
    """Clickable candidate rows.

    Hand-built from st.columns rather than st.dataframe(on_select=...): the
    native grid is the canvas widget that measures zero width inside a tab
    hidden on first paint, and Discover is never the first tab.
    """
    widths = [1.15, 2.5, 1.15, 0.95, 1.15, 1.0, 0.5]
    head = st.columns(widths)
    for col, label in zip(
        head, ["Symbol", "Name", "Price", "24h", "Volume", "Evidence", ""]
    ):
        col.markdown(f"<div class='ti-rowhead'>{label}</div>", unsafe_allow_html=True)

    for index, row in enumerate(rows):
        cols = st.columns(widths)
        symbol = row["symbol"]

        if cols[0].button(symbol, key=f"disc_pick_{index}", width="stretch",
                          help="Show why this is here"):
            st.session_state["disc_sel"] = symbol
            st.rerun()

        cols[1].markdown(
            f"<div class='ti-rowcell ti-dim'>{html_lib.escape((row.get('name') or '')[:38])}</div>",
            unsafe_allow_html=True,
        )
        cols[2].markdown(
            f"<div class='ti-rowcell ti-num'>{fmt_price(row.get('price'))}</div>",
            unsafe_allow_html=True,
        )
        change = row.get("change_24h")
        css = "ti-up" if (change or 0) >= 0 else "ti-down"
        cols[3].markdown(
            f"<div class='ti-rowcell ti-num {css}'>{fmt_pct(change)}</div>",
            unsafe_allow_html=True,
        )
        volume = (
            fmt_shares(row.get("volume")) if asset_class == "stock"
            else fmt_compact(row.get("volume"))
        )
        cols[4].markdown(
            f"<div class='ti-rowcell ti-num'>{volume}</div>", unsafe_allow_html=True
        )
        cols[5].markdown(
            f"<div class='ti-rowcell ti-num'>"
            f"<span class='ti-up'>+{row['supportive']}</span> / "
            f"<span class='ti-down'>-{row['cautionary']}</span></div>",
            unsafe_allow_html=True,
        )

        if symbol in watched:
            cols[6].markdown(
                "<div class='ti-rowcell ti-dim' title='Already on the watchlist'>✓</div>",
                unsafe_allow_html=True,
            )
        elif cols[6].button("＋", key=f"disc_add_{index}", help="Add to watchlist"):
            db.add_watchlist_item(
                symbol, asset_class, row.get("name"),
                row.get("source_id"), row.get("pair_created_at"),
            )
            st.session_state["disc_sel"] = symbol
            flash("success", f"Added {symbol} — the collector picks it up next cycle.")
            refresh_data()
            st.rerun()


def render_discover_tab() -> None:
    st.caption(
        "Scans the whole market rather than your watchlist, then ranks what it "
        "finds on the same evidence the signal desk uses — so the top row is not "
        "simply the biggest gainer. Nothing here is a prediction, and a stock "
        "that has already run is often the worst entry, not the best."
    )

    source = st.segmented_control(
        "Source", ["Stocks", "Crypto", "Meme coins"], default="Stocks",
        key="disc_source", label_visibility="collapsed",
    ) or "Stocks"

    control, action = st.columns([3, 1])
    if source == "Stocks":
        screen_label = control.selectbox(
            "Screen", list(discovery.STOCK_SCREENS), key="disc_screen",
            label_visibility="collapsed",
        )
    elif source == "Crypto":
        mode = control.selectbox(
            "Mode", ["24h movers", "Trending searches"], key="disc_cmode",
            label_visibility="collapsed",
        )
    else:
        control.caption("Boosted tokens on DexScreener, with risk context.")

    with action:
        scan_clicked = st.button("Scan market", key="disc_scan", width="stretch",
                                 type="primary")
    if scan_clicked:
        st.session_state["disc_ran"] = True
        st.cache_data.clear()

    if not st.session_state.get("disc_ran"):
        st.info("Hit **Scan market** to pull candidates. Each scan is a live API call.")
        return

    with st.spinner("Scanning…"):
        if source == "Stocks":
            rows = load_stock_screen(discovery.STOCK_SCREENS[screen_label])
            asset_class = "stock"
        elif source == "Crypto":
            rows = load_crypto_candidates(mode)
            asset_class = "crypto"
        else:
            rows = load_meme_candidates()
            asset_class = "meme"

    if not rows:
        st.warning(
            "That scan came back empty — the source may be rate-limited or down. "
            "Try again shortly, or pick another source."
        )
        return

    shown = rows[:MAX_CANDIDATE_ROWS]
    watched = {w["symbol"] for w in load_watchlist(asset_class)}
    render_candidate_rows(shown, asset_class, watched)
    if len(rows) > len(shown):
        st.caption(f"Showing the top {len(shown)} of {len(rows)} candidates by evidence.")

    selected = st.session_state.get("disc_sel")
    candidate = next((r for r in shown if r["symbol"] == selected), None)
    if candidate is None:
        st.info("Click a **symbol** for the full read, or **+** to add it straight to the watchlist.")
        return

    st.divider()
    st.markdown(f"#### {candidate['symbol']} · why it's here")

    screen_key = discovery.STOCK_SCREENS[screen_label] if source == "Stocks" else None
    if screen_key:
        info = discovery.SCREEN_INFO.get(screen_key, {})
        if info:
            st.markdown(
                f"<div class='ti-thesis'><b>{html_lib.escape(screen_label)}</b> — "
                f"{html_lib.escape(info['thesis'])}"
                f"<div class='ti-thesis-caveat'>{html_lib.escape(info['caveat'])}</div>"
                "</div>",
                unsafe_allow_html=True,
            )
        render_factor_grid(signals.category_factors(candidate, screen_key))

    with st.spinner(f"Pulling history and headlines for {candidate['symbol']}…"):
        read = deep_read(candidate["symbol"], candidate)

    st.markdown("#### How it's trading right now")
    tone = "ti-up" if (read.change_24h or 0) >= 0 else "ti-down"
    st.markdown(
        f"<div class='ti-read-headline {tone}'>{html_lib.escape(read.headline)}</div>"
        f"<div class='ti-read-note'>{html_lib.escape(read.history_note)}</div>",
        unsafe_allow_html=True,
    )
    render_factor_grid(read.factors)
    render_quick_buy(
        candidate["symbol"], asset_class, candidate.get("price"),
        key=f"disc_{candidate['symbol']}", name=candidate.get("name"),
        source_id=candidate.get("source_id"),
        pair_created_at=candidate.get("pair_created_at"),
    )
    if read.news:
        with st.expander(f"The {len(read.news)} headlines this read used"):
            st.markdown(news_html(read.news[:12]), unsafe_allow_html=True)
    else:
        st.caption(
            "No headlines found for this symbol in the last 24h — the move, if "
            "there is one, is unexplained by news this tool can see."
        )


# --------------------------------------------------------------------------
# Recommended tab — live setups against measured historical base rates
# --------------------------------------------------------------------------


@st.cache_data(ttl=600, show_spinner=False)
def load_pattern_stats():
    return db.get_pattern_stats()


@st.cache_data(ttl=1800, show_spinner=False)
def load_regime():
    return quant_live.current_regime()


@st.cache_data(ttl=1800, show_spinner=False)
def load_live_setups(bull: bool | None):
    """Heavy: downloads 2y of history for the whole study universe."""
    return quant_live.rank(quant_live.scan(), db.get_pattern_stats(), bull=bull)


def pct(value: float | None, digits: int = 1) -> str:
    return DASH if value is None else f"{value * 100:.{digits}f}%"


def signed_pct(value: float | None, digits: int = 1) -> str:
    return DASH if value is None else f"{value * 100:+.{digits}f}%"


def is_credible(s: dict) -> bool:
    """Survives the volatility adjustment and holds up out of sample."""
    return (
        (s.get("adjusted_lift") or 0) >= 1.10
        and (s.get("oos_lift") or 0) >= 1.10
    )


def stats_table(stats: dict) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Setup": s["label"],
                "Occurrences": f"{s['n']:,}",
                "Big up-move": pct(s["hit_rate"]),
                "Raw lift": f"{s['lift']:.2f}x",
                "Vol-adj": f"{s['adjusted_lift']:.2f}x" if s.get("adjusted_lift") else DASH,
                "Out-of-sample": f"{s['oos_lift']:.2f}x" if s.get("oos_lift") else DASH,
                "Bull": f"{s['bull_lift']:.2f}x" if s.get("bull_lift") else DASH,
                "Bear": f"{s['bear_lift']:.2f}x" if s.get("bear_lift") else DASH,
                "Big drop": pct(s["bust_rate"]),
                "Typical": signed_pct(s["median_fwd_return"]),
                "Holds up": "yes" if is_credible(s) else "no",
            }
            for s in sorted(
                stats.values(), key=lambda r: -(r.get("adjusted_lift") or 0)
            )
        ]
    )


def render_setup_cards(setups: list[dict]) -> None:
    for s in setups:
        adjusted = s.get("adjusted_lift")
        oos = s.get("oos_lift")
        adj_class = "ti-up" if (adjusted or 0) >= 1.10 else ("ti-down" if (adjusted or 1) < 0.95 else "")
        oos_class = "ti-up" if (oos or 0) >= 1.10 else ("ti-down" if (oos or 1) < 0.95 else "")
        risk_class = "ti-down" if s["bust_rate"] > s["base_bust_rate"] * 1.3 else ""
        badge = (
            "<span class='ti-pill new'>holds up</span>" if is_credible(s)
            else "<span class='ti-pill thin'>doesn't hold</span>"
        )
        st.markdown(
            "<div class='ti-setup'>"
            "<div class='ti-setup-head'>"
            f"<span class='ti-setup-name'>{html_lib.escape(s['label'])} {badge}</span>"
            f"<span class='ti-setup-n'>n={s['n']:,}</span></div>"
            f"<div class='ti-setup-desc'>{html_lib.escape(s['description'])}</div>"
            "<div class='ti-setup-stats'>"
            f"<span>vs similar-risk stocks <b class='{adj_class}'>"
            f"{f'{adjusted:.2f}x' if adjusted else DASH}</b></span>"
            f"<span>out-of-sample <b class='{oos_class}'>"
            f"{f'{oos:.2f}x' if oos else DASH}</b></span>"
            f"<span>big up-move <b>{pct(s['hit_rate'])}</b>"
            f" <i>vs {pct(s['base_rate'])} base</i></span>"
            f"<span>big drop <b class='{risk_class}'>{pct(s['bust_rate'])}</b>"
            f" <i>vs {pct(s['base_bust_rate'])} base</i></span>"
            f"<span>typical <b>{signed_pct(s['median_fwd_return'])}</b></span>"
            "</div></div>",
            unsafe_allow_html=True,
        )


HOT_NEWS_TICKERS = 8


@st.cache_data(ttl=900, show_spinner=False)
def load_hot_news(tickers: tuple[str, ...]) -> dict:
    """Headlines for off-watchlist movers, fetched on demand and tone-scored.

    These tickers are not on the watchlist, so the collector has never pulled
    anything for them — the fetch happens here. Rows land in news_items like any
    other and get pruned on the normal schedule. One Finnhub call per ticker,
    cached for 15 minutes.
    """
    if not tickers:
        return {"items": [], "summary": []}
    try:
        stock_source.fetch_news(days_back=3, symbols=list(tickers))
    except Exception as exc:  # a news outage must not take the tab down
        logging.getLogger("app").error("hot news fetch failed: %s", exc)

    items = db.get_news(list(tickers), limit=len(tickers) * 10)

    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

        analyzer = SentimentIntensityAnalyzer()
        for item in items:
            item["tone"] = analyzer.polarity_scores(item["headline"])["compound"]
    except Exception:
        for item in items:
            item["tone"] = None

    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item["symbol"], []).append(item)

    summary = []
    for ticker in tickers:
        bucket = grouped.get(ticker, [])
        scores = [b["tone"] for b in bucket if b.get("tone") is not None]
        summary.append(
            {
                "ticker": ticker,
                "count": len(bucket),
                "tone": sum(scores) / len(scores) if scores else None,
            }
        )
    return {"items": items, "summary": summary}


def render_hot_news(rows: list[dict]) -> None:
    """Trending headlines for the top-ranked matches.

    Separate from the per-tab watchlist feed, which stays scoped to symbols you
    actually track. This one covers names you have no context on yet, which is
    the whole reason they need reading rather than just scoring.
    """
    tickers = tuple(r["ticker"] for r in rows[:HOT_NEWS_TICKERS])
    if not tickers:
        return

    st.divider()
    st.markdown("#### Trending news on these names")

    load_col, note_col = st.columns([1, 3])
    if load_col.button("Load headlines", key="hot_news_load", width="stretch"):
        st.session_state["hot_news_on"] = True
        load_hot_news.clear()
    note_col.caption(
        f"Pulls company news for the top {len(tickers)} matches above — "
        f"{', '.join(tickers)}. One request per ticker, cached 15 minutes."
    )
    if not st.session_state.get("hot_news_on"):
        return

    with st.spinner("Fetching headlines…"):
        data = load_hot_news(tickers)

    if not data["items"]:
        st.caption(
            "No headlines came back for these tickers. Finnhub's company-news "
            "coverage thins out for smaller names."
        )
        return

    chips = []
    for entry in sorted(data["summary"], key=lambda s: -s["count"]):
        if not entry["count"]:
            continue
        text, css = tone_label(entry["tone"])
        chips.append(
            f"<span class='ti-chip'><b>{html_lib.escape(entry['ticker'])}</b> "
            f"{entry['count']} <span class='{css}'>{html_lib.escape(text)}</span></span>"
        )
    if chips:
        st.markdown(
            f"<div class='ti-chips' style='margin-bottom:.7rem'>{''.join(chips)}</div>",
            unsafe_allow_html=True,
        )

    picked = st.multiselect(
        "Filter",
        list(tickers),
        default=list(tickers),
        key="hot_news_filter",
        label_visibility="collapsed",
    )
    items = [i for i in data["items"] if i["symbol"] in picked]

    # news_items is unique on (symbol, url), so a market-wide story legitimately
    # exists once per ticker it mentions. Correct in the table, but in a merged
    # feed it reads as a duplicate — collapse to one row carrying every tag.
    merged: dict[str, dict] = {}
    for item in items:
        existing = merged.get(item["url"])
        if existing:
            if item["symbol"] not in existing["symbols"]:
                existing["symbols"].append(item["symbol"])
        else:
            merged[item["url"]] = {**item, "symbols": [item["symbol"]]}

    feed = sorted(
        merged.values(), key=lambda i: i.get("published_at") or "", reverse=True
    )
    for item in feed:
        item["symbol"] = " · ".join(sorted(item["symbols"]))

    if not feed:
        st.caption("Nothing selected.")
        return
    st.markdown(news_html(feed[:30], show_tone=True), unsafe_allow_html=True)
    st.caption(
        "Tone is a VADER score of the headline text — a description of wording, "
        "not a judgement about the company."
    )


def render_recommended_tab() -> None:
    stats = load_pattern_stats()

    if not stats:
        st.warning(
            "No historical study on record yet. Build it with:\n\n"
            "```\npython -m quant.study\n```\n\n"
            "It downloads roughly a decade of daily history for ~250 tickers and "
            "takes a couple of minutes. Everything on this tab derives from it."
        )
        return

    sample = next(iter(stats.values()))
    st.caption(
        f"Every setup below was measured over {sample['universe_size']} tickers: "
        f"how often it was actually followed by a "
        f"{sample['threshold'] * 100:.0f}% move within "
        f"{sample['horizon_days']} trading days, against the "
        f"{pct(sample['base_rate'])} rate for a randomly chosen day. These are "
        "historical frequencies, not forecasts."
    )

    with st.expander("How to read this — the three lift columns differ, and that matters"):
        st.markdown(
            f"""
**Measured, not asserted.** {sample['universe_size']} tickers, ~12 years of
daily bars, every trading day scored.

**Raw lift is misleading, and the table proves it.** A fixed
+{sample['threshold'] * 100:.0f}% threshold is partly a volatility bet: cheap,
violent stocks clear any fixed percentage more often whichever way they are
heading. A control condition — below the 200-day and falling — was included
expecting it to underperform, and on raw lift it *beat* the baseline.

**Vol-adjusted** fixes that. Each setup is compared against stocks in the same
trailing-volatility decile instead of against the whole universe. The control
drops to 0.88x, where it belongs. Two reversals came out of it:

- *Recovering from a collapse* falls from 2.19x to **0.74x** — its entire
  apparent edge was volatility. Its median outcome is negative and its drop
  rate is triple the baseline.
- *Stage-2 breakout* and *New 52-week high* rise from 0.99x and 0.79x to
  **1.29x** and **1.21x**. Raw lift made two of the better setups look useless,
  because they select calm stocks that clear a fixed percentage less often.

**Out-of-sample** is the honesty check. Base rates are computed on the first 60%
of the date range and re-measured on the last 40%, with a 90-day purge between
so no outcome straddles the split. A setup that only works in-sample was fitted,
not found.

**Holds up = yes** means both above 1.10x. Three conditions clear it.

**Biases that remain, uncorrected:**

- *Survivorship.* Yahoo only serves tickers that still trade, so companies that
  went to zero are missing and every rate reads high.
- *Overlapping windows.* Consecutive days are near-duplicates, so the true
  independent sample is far smaller than n and the intervals are too tight.
- *Multiple testing.* Ten conditions were tried; some spread is chance.
- *Regime.* The window is dominated by a long bull market.

**No position sizing, costs, slippage, or exit rule is modelled.** This is not a
backtest of a strategy, and none of it is advice.
"""
        )

    regime = load_regime()
    if regime.get("known"):
        bull = regime["bull"]
        st.markdown(
            f"<div class='ti-regime {'bull' if bull else 'bear'}'>"
            f"<b>Market regime: {'uptrend' if bull else 'downtrend'}</b> — SPY is "
            f"{regime['gap_pct']:+.1f}% versus its 200-day average "
            f"(as of {html_lib.escape(regime['as_of'])}). The "
            f"<b>{'bull' if bull else 'bear'}</b> column is the one that applies "
            "today, and several setups behave very differently across the two."
            "</div>",
            unsafe_allow_html=True,
        )

    st.markdown("#### The historical record")
    render_table(stats_table(stats))
    st.caption(
        md(
            "Sorted by **vol-adj**, the only column that compares a setup against "
            "stocks of similar risk. Raw lift ranks a volatility artefact "
            "(Recovering from a collapse, 2.19x raw) above two setups that "
            "actually work (Stage-2 breakout, 0.99x raw)."
        )
    )

    st.divider()
    st.markdown("#### What matches right now")
    scan_col, note_col = st.columns([1, 3])
    if scan_col.button("Scan universe", key="rec_scan", type="primary", width="stretch"):
        st.session_state["rec_ran"] = True
        load_live_setups.clear()
    note_col.caption(
        "Runs the same conditions against the latest bar for every ticker in the "
        "study universe. One batched download, cached for 30 minutes."
    )

    if not st.session_state.get("rec_ran"):
        return

    with st.spinner("Downloading history and matching setups…"):
        rows = load_live_setups(regime.get("bull") if regime.get("known") else None)
    if not rows:
        st.warning("Nothing matched, or the download failed. Try again shortly.")
        return

    st.caption(
        md(f"{len(rows)} tickers match at least one setup. **Holds up** counts how "
        "many of those survived both the volatility adjustment and the "
        "out-of-sample check — that is what the ordering leads on, because "
        "matching three setups that are all volatility artefacts is worth less "
        "than matching one that holds.")
    )

    watched = {w["symbol"] for w in load_watchlist("stock")}
    widths = [1.1, 0.6, 1.0, 1.0, 1.0, 0.95, 2.3, 0.5]
    head = st.columns(widths)
    for col, label in zip(
        head,
        ["Ticker", "Holds up", "Typical", "Big up", "Big drop", "6m", "Matched", ""],
    ):
        col.markdown(f"<div class='ti-rowhead'>{label}</div>", unsafe_allow_html=True)

    for index, row in enumerate(rows[:MAX_CANDIDATE_ROWS]):
        cols = st.columns(widths)
        ticker = row["ticker"]
        if cols[0].button(ticker, key=f"rec_pick_{index}", width="stretch",
                          help="Show the historical record behind these setups"):
            st.session_state["rec_sel"] = ticker
            st.rerun()

        cells = [
            (f"{row.get('n_credible', 0)}/{row['n_setups']}",
             "ti-up" if row.get("n_credible") else "ti-dim"),
            (signed_pct(row["typical"]), "ti-up" if row["typical"] > 0 else "ti-down"),
            (pct(row["best_upside"]), ""),
            (pct(row["worst_downside"]),
             "ti-down" if row["worst_downside"] > row["setups"][0]["base_bust_rate"] * 1.3 else ""),
            (signed_pct(row.get("ret_6m")),
             "ti-up" if (row.get("ret_6m") or 0) >= 0 else "ti-down"),
        ]
        for col, (text, css) in zip(cols[1:6], cells):
            col.markdown(
                f"<div class='ti-rowcell ti-num {css}'>{text}</div>",
                unsafe_allow_html=True,
            )
        cols[6].markdown(
            "<div class='ti-rowcell ti-dim'>"
            + html_lib.escape(", ".join(s["label"] for s in row["setups"]))
            + "</div>",
            unsafe_allow_html=True,
        )
        if ticker in watched:
            cols[7].markdown(
                "<div class='ti-rowcell ti-dim' title='Already watched'>✓</div>",
                unsafe_allow_html=True,
            )
        elif cols[7].button("＋", key=f"rec_add_{index}", help="Add to watchlist"):
            db.add_watchlist_item(ticker, "stock", ticker)
            st.session_state["rec_sel"] = ticker
            flash("success", f"Added {ticker} to the stock watchlist.")
            refresh_data()
            st.rerun()

    render_hot_news(rows)

    selected = st.session_state.get("rec_sel")
    chosen = next((r for r in rows if r["ticker"] == selected), None)
    if chosen is None:
        st.info("Click a **ticker** for the record behind its setups, or **+** to watch it.")
        return

    st.divider()
    st.markdown(f"#### {chosen['ticker']} · the record behind these setups")
    st.markdown(
        md(
            f"Price {fmt_price(chosen['price'])} · six-month "
            f"{signed_pct(chosen.get('ret_6m'))} · "
            f"{signed_pct(chosen.get('pct_from_52w_high'))} from its 52-week high · "
            f"RSI {chosen.get('rsi14') and round(chosen['rsi14'])} · "
            f"as of {chosen['as_of']}"
        )
    )
    render_setup_cards(chosen["setups"])
    render_quick_buy(
        chosen["ticker"], "stock", chosen.get("price"),
        key=f"rec_{chosen['ticker']}", name=chosen["ticker"],
    )
    st.caption(
        f"Matching a setup means {chosen['ticker']} currently looks like the "
        "historical cases — not that it will do the same thing. On the best of "
        f"these, the big move failed to appear "
        f"{pct(1 - chosen['best_upside'])} of the time."
    )


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


def render_equity_curve(starting_balance: float) -> None:
    """Drawn from portfolio_snapshots, which the collector appends each cycle.

    Always whole-account: a per-class equity curve would need per-class
    snapshots, and only the total is recorded.
    """
    curve = load_equity_curve()
    if len(curve) < 2:
        st.caption(
            "The equity curve needs at least two snapshots. The collector "
            f"records one every {config.PORTFOLIO_SNAPSHOT_INTERVAL}s while it runs."
        )
        return

    frame = pd.DataFrame(
        {
            "t": pd.to_datetime([c["snapshot_at"] for c in curve], utc=True, format="ISO8601"),
            "total": [c["total_value"] for c in curve],
            "cash": [c["cash_balance"] for c in curve],
        }
    )
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(x=frame["t"], y=frame["total"], mode="lines", name="Total value",
                   line=dict(width=2.2, color=ACCENT))
    )
    fig.add_trace(
        go.Scatter(x=frame["t"], y=frame["cash"], mode="lines", name="Cash",
                   line=dict(width=1.4, dash="dot", color=ACCENT_ALT))
    )
    fig.add_hline(
        y=starting_balance, line_dash="dash", line_color="#888",
        annotation_text="starting balance", annotation_position="bottom right",
    )
    dark_layout(fig, 360)
    fig.update_layout(
        margin=dict(l=10, r=10, t=26, b=10),
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    fig.update_yaxes(tickprefix="$")
    st.plotly_chart(fig, width="stretch")
    st.caption("Whole-account value; not split by the asset-class filter.")


def render_portfolio_tab(snapshots: dict) -> None:

    choice = st.segmented_control(
        "Asset class", list(FILTER_LABELS), default="All",
        key="pf_filter", label_visibility="collapsed",
    ) or "All"
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
        render_table(positions_frame(summary["positions"]))
    else:
        st.caption("No open positions.")

    st.markdown("#### Equity curve")
    render_equity_curve(summary["starting_balance"])

    st.markdown("#### Trade history")
    trades = portfolio.get_trades(asset_class)
    if trades:
        render_table(trades_frame(trades))
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


def render_hero(snapshots: dict) -> None:
    newest = newest_snapshot_time(snapshots)
    when = db.from_iso(newest)
    if when is None:
        state, label = "dead", "collector offline"
    else:
        age = (datetime.now(timezone.utc) - when).total_seconds()
        state, label = ("live", f"live · {fmt_when(newest)}") if age <= 300 else (
            "stale", f"stale · {fmt_when(newest)}"
        )

    account = portfolio.get_account()
    prices = {s: v["price"] for s, v in snapshots.items() if v.get("price") is not None}
    total = account["cash_balance"] + sum(
        p["quantity"] * prices[p["symbol"]]
        for p in portfolio.get_positions()
        if p["symbol"] in prices
    )
    pnl = total - account["starting_balance"]
    pnl_class = "ti-up" if pnl >= 0 else "ti-down"

    st.markdown(
        "<div class='ti-hero'><div>"
        "<h1>Trading Insights</h1>"
        "<div class='ti-sub'>Movement, the news behind it, and what argues both ways — "
        "assembled for you to judge. Paper trading only; no brokerage is ever linked.</div>"
        "</div><div class='ti-chips'>"
        f"<span class='ti-chip'><span class='ti-dot {state}'></span>{label}</span>"
        f"<span class='ti-chip'>paper value <b>${total:,.2f}</b></span>"
        f"<span class='ti-chip'>P&amp;L <b class='{pnl_class}'>{'+' if pnl >= 0 else ''}"
        f"${pnl:,.2f}</b></span>"
        "</div></div>",
        unsafe_allow_html=True,
    )


def main() -> None:
    db.init_db()
    st.markdown(load_stylesheet(), unsafe_allow_html=True)
    snapshots = load_snapshots()
    render_sidebar(snapshots)
    render_hero(snapshots)

    drain_flash()

    # Three destinations, not six. The asset tabs were the same page with a
    # different filter, and Discover/Recommended are both "find something new" —
    # so those become sub-navigation instead of competing for the top bar.
    # It is also cheaper: st.tabs renders every panel on every rerun, whereas
    # only the selected branch below executes.
    markets, ideas, portfolio_tab = st.tabs(["Markets", "Ideas", "Portfolio"])

    with markets:
        labels = [label for _, label in ASSET_TABS]
        picked = st.segmented_control(
            "Asset class", labels, default=labels[0],
            key="market_class", label_visibility="collapsed",
        ) or labels[0]
        asset_class = next(a for a, label in ASSET_TABS if label == picked)
        render_asset_tab(asset_class, picked, snapshots)

    with ideas:
        mode = st.segmented_control(
            "Mode", ["Discover", "Recommended"], default="Discover",
            key="ideas_mode", label_visibility="collapsed",
        ) or "Discover"
        if mode == "Discover":
            render_discover_tab()
        else:
            render_recommended_tab()

    with portfolio_tab:
        render_portfolio_tab(snapshots)


main()

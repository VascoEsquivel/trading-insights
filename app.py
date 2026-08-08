"""Trading Insights — Streamlit entrypoint.

Reads only from SQLite. The collector (`python -m collector.scheduler`) is a
separate process and is the only writer; no background threads are started here,
because Streamlit reruns this script top-to-bottom on every interaction and
would duplicate or orphan them.
"""
from __future__ import annotations

import html as html_lib
import time
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config
from collector import crypto as crypto_source
from collector import memecoins as meme_source
from collector import stocks as stock_source
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


@st.cache_data(ttl=config.MEME_TRENDING_INTERVAL, show_spinner="Asking DexScreener…")
def load_trending():
    """Boosted tokens, resolved to their deepest pool.

    Button-gated rather than automatic: resolving each token is its own request,
    and this is discovery, not something the dashboard needs on every rerun.
    """
    return meme_source.fetch_trending()


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
            "Volume 24h": fmt_compact(snap.get("volume")),
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

    st.markdown("**Place a paper order**")
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

    supportive = [f for f in read.factors if f.stance == "supportive"]
    cautionary = [f for f in read.factors if f.stance == "cautionary"]
    neutral = [f for f in read.factors if f.stance == "neutral"]

    def factor_block(title: str, items, css: str) -> str:
        if not items:
            return (
                f"<div class='ti-fcol'><div class='ti-fhead {css}'>{title} · 0</div>"
                "<div class='ti-fempty'>Nothing in this column.</div></div>"
            )
        rows = "".join(
            f"<div class='ti-factor'><span class='ti-fmark {STANCE_CLASS[i.stance]}'>"
            f"{STANCE_ICON[i.stance]}</span><div><b>{html_lib.escape(i.label)}</b>"
            f"<div class='ti-fdetail'>{html_lib.escape(i.detail)}</div></div></div>"
            for i in items
        )
        return (
            f"<div class='ti-fcol'><div class='ti-fhead {css}'>{title} · {len(items)}</div>"
            f"{rows}</div>"
        )

    st.markdown(
        "<div class='ti-fgrid'>"
        + factor_block("Supporting", supportive, "ti-up")
        + factor_block("Against", cautionary, "ti-down")
        + factor_block("Context", neutral, "")
        + "</div>",
        unsafe_allow_html=True,
    )

    if read.news:
        with st.expander(f"The {len(read.news)} headlines this read used"):
            st.markdown(news_html(read.news[:12]), unsafe_allow_html=True)

    st.caption(
        "This weighs up evidence — it is not a recommendation, and none of these "
        "factors predicts what happens next. A well-explained move is still just "
        "a move that has been explained."
    )


# --------------------------------------------------------------------------
# Trending discovery (meme tab only)
# --------------------------------------------------------------------------


def render_trending() -> None:
    st.markdown("#### Trending on DexScreener")
    left, right = st.columns([1, 3])
    if left.button("Load trending", key="load_trending"):
        st.session_state["show_trending"] = True
        load_trending.clear()
    if not st.session_state.get("show_trending"):
        right.caption(
            "Currently-boosted tokens, with the same risk context as the "
            "watchlist. Nothing is added automatically."
        )
        return

    rows = load_trending()
    if not rows:
        st.caption("DexScreener returned no trending pairs just now.")
        return

    watched = {e["source_id"] for e in load_watchlist("meme")}
    table = pd.DataFrame(
        [
            {
                "Symbol": r["symbol"],
                "Name": r["name"],
                "Chain": r["chain"],
                "Price": fmt_price(r["price"]),
                "24h": fmt_pct(r["pct_change_24h"]),
                "Volume 24h": fmt_compact(r["volume_24h"]),
                "Liquidity": fmt_compact(r["liquidity_usd"]),
                "Market cap": fmt_compact(r["market_cap"]),
                "Age": fmt_age(r["pair_created_at"]),
                "Risk": risk_flags(
                    {"pair_created_at": r["pair_created_at"]},
                    {"liquidity_usd": r["liquidity_usd"]},
                ),
            }
            for r in rows
        ]
    )
    render_table(table)

    addable = [r for r in rows if r["source_id"] not in watched]
    if not addable:
        st.caption("All of these are already on the watchlist.")
        return
    pick_col, btn_col = st.columns([3, 1])
    choice = pick_col.selectbox(
        "Add to watchlist",
        [f"{r['symbol']} — {r['name']}" for r in addable],
        key="trending_pick",
        label_visibility="collapsed",
    )
    with btn_col:
        if st.button("Add", key="trending_add", width="stretch"):
            target = addable[
                [f"{r['symbol']} — {r['name']}" for r in addable].index(choice)
            ]
            db.add_watchlist_item(
                target["symbol"], "meme", target["name"],
                target["source_id"], target["pair_created_at"],
            )
            flash("success", f"Added {target['symbol']} to the meme watchlist.")
            refresh_data()
            st.rerun()


# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------


def news_html(items: list[dict]) -> str:
    """Headline list. Values are third-party text, so everything is escaped."""
    rows = []
    for item in items:
        url = html_lib.escape(item.get("url") or "", quote=True)
        rows.append(
            "<div class='ti-news-item'>"
            f"<a href='{url}' target='_blank' rel='noopener noreferrer'>"
            f"{html_lib.escape(item['headline'])}</a>"
            "<div class='ti-news-meta'>"
            f"<span class='tag'>{html_lib.escape(item['symbol'])}</span>"
            f"<span>{html_lib.escape(item.get('source') or '?')}</span>"
            f"<span>{html_lib.escape(fmt_when(item.get('published_at')))}</span>"
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
    st.subheader(f"{label} watchlist")
    render_watchlist(entries, snapshots, asset_class)
    render_watchlist_manager(asset_class, entries)
    st.divider()
    render_signal_desk(asset_class, entries, snapshots)
    if asset_class == "meme":
        st.divider()
        render_trending()
    st.divider()
    render_chart_section(asset_class, entries)
    st.divider()
    render_trade_form(asset_class, entries, snapshots)
    st.divider()
    news_col, social_col = st.columns([3, 2])
    with news_col:
        render_news(entries)
    with social_col:
        render_sentiment(entries)


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

    tabs = st.tabs([label for _, label in ASSET_TABS] + ["Portfolio"])
    for tab, (asset_class, label) in zip(tabs, ASSET_TABS):
        with tab:
            render_asset_tab(asset_class, label, snapshots)
    with tabs[-1]:
        render_portfolio_tab(snapshots)


main()

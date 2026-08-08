"""Signal engine: size a move, then look for what corroborates or undercuts it.

This assembles evidence; it does not issue a call. Every factor carries the
number it was derived from so a read can be checked rather than trusted, and
the output deliberately has no buy/sell label — the question it answers is
"what is actually going on with this symbol right now", which is the part that
takes work.

Nothing here is a forecast. A 3-sigma move on heavy volume with corroborating
headlines is a well-explained move, not a prediction that it continues.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal

import config
from trading import db

Stance = Literal["supportive", "cautionary", "neutral"]

# Thresholds, gathered here so a read can be retuned in one place.
UNUSUAL_SIGMA = 1.5          # |z| above this is "unusual" vs. recent behaviour
BIG_SIGMA = 2.5
VOLUME_CONFIRM = 1.25        # today's volume vs. its 20-period average
VOLUME_WEAK = 0.80
EXTENDED_MA_PCT = 12.0       # % above the 20-period average before "extended"
RSI_HOT, RSI_COLD = 70.0, 30.0
NEWS_WINDOW_HOURS = 24
MIN_HISTORY = 12             # fewer points than this and the stats aren't worth it


@dataclass
class Factor:
    label: str
    detail: str
    stance: Stance


@dataclass
class Readout:
    symbol: str
    name: str
    asset_class: str
    price: float | None = None
    change_24h: float | None = None
    sigma_move: float | None = None
    ma20_gap: float | None = None
    rsi14: float | None = None
    volume_ratio: float | None = None
    range_position: float | None = None
    liquidity_usd: float | None = None
    news_count: int = 0
    news_tone: float | None = None
    news: list[dict] = field(default_factory=list)
    factors: list[Factor] = field(default_factory=list)
    history_note: str = ""
    history_points: int = 0

    @property
    def supportive(self) -> int:
        return sum(1 for f in self.factors if f.stance == "supportive")

    @property
    def cautionary(self) -> int:
        return sum(1 for f in self.factors if f.stance == "cautionary")

    @property
    def is_unusual(self) -> bool:
        return self.sigma_move is not None and abs(self.sigma_move) >= UNUSUAL_SIGMA

    @property
    def headline(self) -> str:
        """One line describing the move, with no judgement attached."""
        if self.change_24h is None:
            return "No 24h change on record."
        direction = "up" if self.change_24h >= 0 else "down"
        if self.sigma_move is None:
            return f"{direction.capitalize()} {abs(self.change_24h):.2f}% over 24h."
        return (
            f"{direction.capitalize()} {abs(self.change_24h):.2f}% over 24h — "
            f"{abs(self.sigma_move):.1f}x its typical daily swing."
        )


# --------------------------------------------------------------------------
# Indicators
# --------------------------------------------------------------------------


def sma(values: list[float], window: int) -> float | None:
    return sum(values[-window:]) / window if len(values) >= window else None


def rsi(values: list[float], period: int = 14) -> float | None:
    """Wilder's RSI. Returns None when there isn't enough history."""
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for previous, current in zip(values[-(period + 1):-1], values[-period:]):
        delta = current - previous
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def daily_return_sigma(closes: list[float]) -> float | None:
    """Standard deviation of period-over-period % returns, as a percentage."""
    returns = [
        (b - a) / a * 100.0
        for a, b in zip(closes, closes[1:])
        if a
    ]
    if len(returns) < 5:
        return None
    try:
        return statistics.stdev(returns)
    except statistics.StatisticsError:
        return None


# --------------------------------------------------------------------------
# History providers
# --------------------------------------------------------------------------


def _history_for(entry: dict) -> tuple[list[float], list[float], str]:
    """(closes, volumes, note) for one watchlist entry, newest last."""
    asset_class = entry["asset_class"]
    source_id = entry.get("source_id")

    if asset_class == "stock":
        from collector import stocks

        hist = stocks.fetch_candles(entry["symbol"], period="6mo", interval="1d")
        if hist is None or len(hist) < 2:
            return [], [], "No daily history available (yfinance returned nothing)."
        closes = [float(x) for x in hist["Close"].to_numpy()]
        volumes = [float(x) for x in hist["Volume"].to_numpy()]
        return closes, volumes, f"{len(closes)} daily closes from yfinance."

    if source_id and db.is_dex_source(source_id):
        rows = db.snapshot_history(entry["symbol"], hours=72)
        closes = [r["price"] for r in rows if r.get("price") is not None]
        volumes = [r["volume"] for r in rows if r.get("volume") is not None]
        return (
            closes,
            volumes,
            f"{len(closes)} collected snapshots — DexScreener publishes no history, "
            "so this fills in as the collector runs.",
        )

    if source_id:
        from collector import crypto

        chart = crypto.fetch_market_chart(source_id, days=90)
        if not chart:
            return [], [], "CoinGecko returned no price history."
        # Hourly series resampled to one close per calendar day.
        by_day: dict[str, tuple[float, float]] = {}
        volumes_by_day: dict[str, float] = {}
        for (ts, price), (_, volume) in zip(chart["prices"], chart.get("total_volumes", chart["prices"])):
            day = db.from_epoch_ms(ts).date().isoformat()
            by_day[day] = (ts, price)
            volumes_by_day[day] = volume
        days = sorted(by_day)
        closes = [by_day[d][1] for d in days]
        volumes = [volumes_by_day[d] for d in days]
        return closes, volumes, f"{len(closes)} daily closes from CoinGecko."

    return [], [], "No history source for this symbol."


# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------


def _news_for(symbol: str, hours: int = NEWS_WINDOW_HOURS) -> tuple[list[dict], float | None]:
    cutoff = db.utcnow() - timedelta(hours=hours)
    recent = [
        item
        for item in db.get_news([symbol], limit=60)
        if (published := db.from_iso(item.get("published_at"))) and published >= cutoff
    ]
    if not recent:
        return [], None
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

        analyzer = SentimentIntensityAnalyzer()
        scores = [analyzer.polarity_scores(i["headline"])["compound"] for i in recent]
        tone = sum(scores) / len(scores)
    except Exception:
        tone = None
    return recent, tone


# --------------------------------------------------------------------------
# The read
# --------------------------------------------------------------------------


def analyze(entry: dict, snapshot: dict | None) -> Readout:
    read = Readout(
        symbol=entry["symbol"],
        name=entry.get("name") or entry["symbol"],
        asset_class=entry["asset_class"],
    )
    snapshot = snapshot or {}
    read.price = snapshot.get("price")
    read.change_24h = snapshot.get("pct_change_24h")
    read.liquidity_usd = snapshot.get("liquidity_usd")

    closes, volumes, note = _history_for(entry)
    read.history_note = note
    read.history_points = len(closes)

    if len(closes) >= MIN_HISTORY:
        sigma = daily_return_sigma(closes)
        if sigma and read.change_24h is not None:
            read.sigma_move = read.change_24h / sigma
        ma20 = sma(closes, 20) or sma(closes, max(5, len(closes) // 2))
        reference = read.price or closes[-1]
        if ma20:
            read.ma20_gap = (reference - ma20) / ma20 * 100.0
        read.rsi14 = rsi(closes)
        low, high = min(closes), max(closes)
        if high > low:
            read.range_position = (reference - low) / (high - low)
    if len(volumes) >= MIN_HISTORY:
        baseline = sum(volumes[-21:-1]) / len(volumes[-21:-1]) if len(volumes) > 1 else None
        if baseline:
            latest_volume = snapshot.get("volume") or volumes[-1]
            read.volume_ratio = latest_volume / baseline

    read.news, read.news_tone = _news_for(entry["symbol"])
    read.news_count = len(read.news)

    read.factors = _build_factors(read)
    return read


def _build_factors(read: Readout) -> list[Factor]:
    factors: list[Factor] = []
    move_up = (read.change_24h or 0) >= 0

    # 1. How unusual is the move at all?
    if read.sigma_move is not None:
        magnitude = abs(read.sigma_move)
        if magnitude >= BIG_SIGMA:
            factors.append(Factor(
                "Move size",
                f"{magnitude:.1f}x the symbol's typical daily swing — a genuine outlier, "
                "not routine noise.",
                "neutral",
            ))
        elif magnitude >= UNUSUAL_SIGMA:
            factors.append(Factor(
                "Move size",
                f"{magnitude:.1f}x its typical daily swing — larger than usual.",
                "neutral",
            ))
        else:
            factors.append(Factor(
                "Move size",
                f"{magnitude:.1f}x its typical daily swing — within normal range, "
                "so there may be nothing here to explain.",
                "cautionary",
            ))

    # 2. Does volume back the move up?
    if read.volume_ratio is not None:
        if read.volume_ratio >= VOLUME_CONFIRM:
            factors.append(Factor(
                "Volume",
                f"{read.volume_ratio:.2f}x its 20-period average — the move carries "
                "real participation behind it.",
                "supportive",
            ))
        elif read.volume_ratio <= VOLUME_WEAK:
            factors.append(Factor(
                "Volume",
                f"{read.volume_ratio:.2f}x its 20-period average — thin participation, "
                "so the move is easier to fade or reverse.",
                "cautionary",
            ))
        else:
            factors.append(Factor(
                "Volume",
                f"{read.volume_ratio:.2f}x its 20-period average — unremarkable.",
                "neutral",
            ))

    # 3. Is there news that explains it, and does the tone match the direction?
    if read.news_count == 0:
        factors.append(Factor(
            "News",
            f"Nothing stored in the last {NEWS_WINDOW_HOURS}h. "
            + ("An unusual move with no headline behind it is unexplained — worth "
               "finding out why before acting on it."
               if read.is_unusual else
               "Consistent with a quiet tape."),
            "cautionary" if read.is_unusual else "neutral",
        ))
    else:
        tone = read.news_tone
        if tone is None:
            factors.append(Factor(
                "News", f"{read.news_count} headlines in {NEWS_WINDOW_HOURS}h.", "neutral"
            ))
        else:
            aligned = (tone >= 0.05 and move_up) or (tone <= -0.05 and not move_up)
            conflicting = (tone >= 0.05 and not move_up) or (tone <= -0.05 and move_up)
            descriptor = "positive" if tone >= 0.05 else ("negative" if tone <= -0.05 else "neutral")
            if aligned:
                factors.append(Factor(
                    "News",
                    f"{read.news_count} headlines, average tone {tone:+.2f} ({descriptor}) "
                    "— coverage points the same way the price moved.",
                    "supportive",
                ))
            elif conflicting:
                factors.append(Factor(
                    "News",
                    f"{read.news_count} headlines, average tone {tone:+.2f} ({descriptor}) "
                    "— coverage runs against the price move, which is a disagreement "
                    "worth understanding.",
                    "cautionary",
                ))
            else:
                factors.append(Factor(
                    "News",
                    f"{read.news_count} headlines, average tone {tone:+.2f} (neutral) "
                    "— nothing decisive either way.",
                    "neutral",
                ))

    # 4. Where is price relative to its own trend?
    if read.ma20_gap is not None:
        if read.ma20_gap > EXTENDED_MA_PCT:
            factors.append(Factor(
                "Trend",
                f"{read.ma20_gap:+.1f}% vs. its 20-period average — extended, so "
                "entering here pays up relative to the recent base.",
                "cautionary",
            ))
        elif read.ma20_gap < -EXTENDED_MA_PCT:
            factors.append(Factor(
                "Trend",
                f"{read.ma20_gap:+.1f}% vs. its 20-period average — well below its "
                "recent base.",
                "cautionary",
            ))
        elif abs(read.ma20_gap) <= 4.0:
            factors.append(Factor(
                "Trend",
                f"{read.ma20_gap:+.1f}% vs. its 20-period average — sitting right on "
                "its recent base.",
                "supportive",
            ))
        else:
            side = "above" if read.ma20_gap > 0 else "below"
            factors.append(Factor(
                "Trend",
                f"{read.ma20_gap:+.1f}% vs. its 20-period average — clearly {side} "
                "the base, but not yet stretched.",
                "neutral",
            ))

    # 5. Momentum extremes.
    if read.rsi14 is not None:
        if read.rsi14 >= RSI_HOT:
            factors.append(Factor(
                "RSI(14)",
                f"{read.rsi14:.0f} — in the range usually described as overbought.",
                "cautionary",
            ))
        elif read.rsi14 <= RSI_COLD:
            factors.append(Factor(
                "RSI(14)",
                f"{read.rsi14:.0f} — in the range usually described as oversold.",
                "cautionary",
            ))
        else:
            factors.append(Factor(
                "RSI(14)", f"{read.rsi14:.0f} — mid-range, no momentum extreme.", "supportive"
            ))

    # 6. Position in the observed range.
    if read.range_position is not None:
        pct = read.range_position * 100
        if pct >= 92:
            factors.append(Factor(
                "Range", f"At {pct:.0f}% of its observed range — near the highs.", "cautionary"
            ))
        elif pct <= 8:
            factors.append(Factor(
                "Range", f"At {pct:.0f}% of its observed range — near the lows.", "cautionary"
            ))
        else:
            factors.append(Factor(
                "Range", f"At {pct:.0f}% of its observed range.", "neutral"
            ))

    # 7. Meme-specific: a thin pool makes every other number less trustworthy.
    if read.liquidity_usd is not None and read.liquidity_usd < config.THIN_LIQUIDITY_USD:
        factors.append(Factor(
            "Liquidity",
            f"${read.liquidity_usd:,.0f} in the pool — thin enough that the price "
            "is cheap to push, and that exiting a position may move it against you.",
            "cautionary",
        ))

    if read.history_points < MIN_HISTORY:
        factors.append(Factor(
            "History",
            f"Only {read.history_points} data points — too little to size the move "
            "against, so treat the stats above as provisional.",
            "cautionary",
        ))

    return factors


def screen_factors(candidate: dict[str, Any]) -> list[Factor]:
    """A cheap read over a discovery candidate, from screener fields alone.

    Same vocabulary as analyze(), but no extra requests — so a whole screen can
    be ranked at once. The deep read is what pulls history and headlines.
    """
    factors: list[Factor] = []
    change = candidate.get("change_24h")
    move_up = (change or 0) >= 0

    ratio = candidate.get("volume_ratio")
    if ratio is not None:
        if candidate["asset_class"] == "stock":
            if ratio >= 2.0:
                factors.append(Factor("Volume", f"{ratio:.1f}x its 3-month average — heavy participation.", "supportive"))
            elif ratio >= 1.25:
                factors.append(Factor("Volume", f"{ratio:.1f}x its 3-month average — above normal.", "supportive"))
            elif ratio <= 0.8:
                factors.append(Factor("Volume", f"{ratio:.1f}x its 3-month average — the move is thinly traded.", "cautionary"))
            else:
                factors.append(Factor("Volume", f"{ratio:.1f}x its 3-month average.", "neutral"))
        else:
            # Turnover: daily volume against market cap.
            pct = ratio * 100
            if pct >= 15:
                factors.append(Factor("Turnover", f"24h volume is {pct:.0f}% of market cap — very actively traded.", "supportive"))
            elif pct >= 3:
                factors.append(Factor("Turnover", f"24h volume is {pct:.0f}% of market cap — healthy activity.", "supportive"))
            else:
                factors.append(Factor("Turnover", f"24h volume is only {pct:.1f}% of market cap — quiet.", "cautionary"))

    position = candidate.get("range_position")
    if position is not None:
        pct = position * 100
        if pct >= 95:
            factors.append(Factor("Range", f"At {pct:.0f}% of its range — buying the very top of it.", "cautionary"))
        elif pct >= 60:
            factors.append(Factor("Range", f"At {pct:.0f}% of its range — strength, not yet the extreme.", "supportive"))
        elif pct <= 10:
            factors.append(Factor("Range", f"At {pct:.0f}% of its range — near the bottom.", "cautionary"))
        else:
            factors.append(Factor("Range", f"At {pct:.0f}% of its range.", "neutral"))

    vs_50d = candidate.get("vs_50d")
    if vs_50d is not None:
        pct = vs_50d * 100 if abs(vs_50d) < 5 else vs_50d  # yfinance returns a fraction
        if pct > 60:
            factors.append(Factor("Trend", f"{pct:+.0f}% vs. its 50-day average — a long way extended.", "cautionary"))
        elif pct > 0:
            factors.append(Factor("Trend", f"{pct:+.0f}% vs. its 50-day average — above trend.", "supportive"))
        else:
            factors.append(Factor("Trend", f"{pct:+.0f}% vs. its 50-day average — below trend.", "cautionary"))

    cap = candidate.get("market_cap")
    if cap is not None:
        floor = 3e8 if candidate["asset_class"] == "stock" else 5e7
        if cap < floor:
            factors.append(Factor("Size", f"${cap:,.0f} market cap — small enough to be pushed around.", "cautionary"))
        else:
            factors.append(Factor("Size", f"${cap:,.0f} market cap.", "neutral"))

    liquidity = candidate.get("liquidity_usd")
    if liquidity is not None and liquidity < config.THIN_LIQUIDITY_USD:
        factors.append(Factor("Liquidity", f"${liquidity:,.0f} pool — thin and cheap to move.", "cautionary"))

    if change is not None and abs(change) >= 20 and move_up:
        factors.append(Factor(
            "Move size",
            f"Already {change:+.1f}% on the day — most of the move may be behind it.",
            "cautionary",
        ))

    return factors


# Below this trailing EPS, a growth percentage is denominator noise rather than
# information: $0.04 -> $2.23 prints as "+5465% growth", which is true and
# useless. Cases under the floor are described in dollars instead.
MIN_EPS_BASE = 0.10


def implied_eps_growth(candidate: dict[str, Any]) -> float | None:
    """Forward EPS against trailing, as a percentage.

    Yahoo's screener payload carries no revenueGrowth or earningsGrowth field,
    so this stands in for "are earnings still growing". Returns None when
    trailing EPS is negative (the sign flips and the ratio stops meaning
    growth) or too near zero to divide by — see MIN_EPS_BASE.
    """
    forward = candidate.get("eps_forward")
    trailing = candidate.get("eps_ttm")
    if forward is None or trailing is None or trailing < MIN_EPS_BASE:
        return None
    return (forward - trailing) / trailing * 100


def _eps_base_factor(candidate: dict[str, Any]) -> Factor | None:
    """Describe an unusable growth base in dollars rather than a huge percent."""
    trailing = candidate.get("eps_ttm")
    forward = candidate.get("eps_forward")
    if trailing is None or trailing >= MIN_EPS_BASE:
        return None
    if trailing <= 0:
        return Factor(
            "Earnings base",
            f"Trailing EPS is ${trailing:.2f} — the company is not profitable, so "
            "any growth rate is a forecast rather than a trend."
            + (f" Forward estimate is ${forward:.2f}." if forward is not None else ""),
            "cautionary",
        )
    return Factor(
        "Earnings base",
        f"Trailing EPS is only ${trailing:.2f}"
        + (f", against a ${forward:.2f} forward estimate" if forward is not None else "")
        + " — a percentage growth rate off a base this small is arithmetic, not a "
        "trend, so it is left out.",
        "cautionary",
    )


def category_factors(candidate: dict[str, Any], screen_key: str) -> list[Factor]:
    """Why this stock sits in this screen, in the screen's own terms.

    A momentum screen is explained with move and participation; a value screen
    with multiples; a growth screen with the earnings trajectory. Every factor
    carries the figure it came from so the classification can be argued with.
    """
    from collector.discovery import SCREEN_INFO

    info = SCREEN_INFO.get(screen_key, {})
    kind = info.get("kind", "momentum")
    factors: list[Factor] = []

    trailing_pe = candidate.get("trailing_pe")
    forward_pe = candidate.get("forward_pe")
    book = candidate.get("price_to_book")
    growth = implied_eps_growth(candidate)
    change_52w = candidate.get("change_52w")

    if kind == "value":
        if trailing_pe is not None:
            if trailing_pe <= 20:
                factors.append(Factor(
                    "Earnings multiple",
                    f"Trailing P/E of {trailing_pe:.1f} — this is the number that put "
                    "it on a value screen; under ~20 is the usual cutoff.",
                    "supportive",
                ))
            else:
                factors.append(Factor(
                    "Earnings multiple",
                    f"Trailing P/E of {trailing_pe:.1f} — higher than a value screen "
                    "normally admits, so it likely qualified on the forward figure.",
                    "neutral",
                ))
        else:
            factors.append(Factor(
                "Earnings multiple",
                "No trailing P/E, which means trailing earnings are negative — "
                "'cheap' here rests entirely on forecasts.",
                "cautionary",
            ))
        if forward_pe is not None and trailing_pe is not None:
            if forward_pe < trailing_pe:
                factors.append(Factor(
                    "Forward multiple",
                    f"Forward P/E {forward_pe:.1f} vs. trailing {trailing_pe:.1f} — "
                    "it gets cheaper on next year's earnings, which is the whole "
                    "premise of the screen.",
                    "supportive",
                ))
            else:
                factors.append(Factor(
                    "Forward multiple",
                    f"Forward P/E {forward_pe:.1f} is above trailing "
                    f"{trailing_pe:.1f} — earnings are expected to fall, so the low "
                    "multiple is not the bargain it looks like.",
                    "cautionary",
                ))
        if book is not None:
            stance = "supportive" if book <= 3 else ("neutral" if book <= 8 else "cautionary")
            factors.append(Factor(
                "Price to book",
                f"{book:.2f}x book value"
                + (" — backed by real assets." if book <= 3 else
                   " — priced well above its balance sheet." if book > 8 else "."),
                stance,
            ))

    elif kind == "growth":
        if growth is not None:
            if growth >= 25:
                factors.append(Factor(
                    "Earnings growth",
                    f"Forward EPS is {growth:+.0f}% above trailing — the trajectory "
                    "that earns it a place on a growth screen.",
                    "supportive",
                ))
            elif growth > 0:
                factors.append(Factor(
                    "Earnings growth",
                    f"Forward EPS is {growth:+.0f}% above trailing — growing, but "
                    "modestly for this category.",
                    "neutral",
                ))
            else:
                factors.append(Factor(
                    "Earnings growth",
                    f"Forward EPS is {growth:+.0f}% versus trailing — earnings are "
                    "expected to shrink, which sits oddly on a growth screen.",
                    "cautionary",
                ))
        else:
            base = _eps_base_factor(candidate)
            if base:
                factors.append(base)
        if forward_pe is not None:
            stance = "cautionary" if forward_pe > 40 else "neutral"
            factors.append(Factor(
                "What you pay for it",
                f"Forward P/E of {forward_pe:.1f}"
                + (" — the growth is already priced in aggressively."
                   if forward_pe > 40 else "."),
                stance,
            ))

    else:  # momentum screens
        change = candidate.get("change_24h")
        if change is not None:
            factors.append(Factor(
                "Why it's listed",
                f"{change:+.2f}% on the session — the move itself is the only "
                "reason this screen picked it up.",
                "neutral",
            ))
        ratio = candidate.get("volume_ratio")
        if ratio is not None and ratio >= 1.25:
            factors.append(Factor(
                "Participation",
                f"{ratio:.2f}x its 3-month average volume — the move has real "
                "money behind it, not a thin print.",
                "supportive",
            ))
        if forward_pe is not None:
            factors.append(Factor(
                "What you pay for it",
                f"Forward P/E of {forward_pe:.1f} — the screen ignored valuation "
                "entirely, so this is worth knowing.",
                "cautionary" if forward_pe > 40 else "neutral",
            ))
        if growth is not None:
            factors.append(Factor(
                "Earnings trajectory",
                f"Forward EPS {growth:+.0f}% vs. trailing — again, not something "
                "a momentum screen checks.",
                "supportive" if growth >= 15 else "neutral",
            ))
        else:
            base = _eps_base_factor(candidate)
            if base:
                factors.append(base)

    if change_52w is not None:
        if change_52w > 100:
            factors.append(Factor(
                "Past year",
                f"{change_52w:+.0f}% over 12 months — a large amount of the story "
                "may already be in the price.",
                "cautionary",
            ))
        elif change_52w < -30:
            factors.append(Factor(
                "Past year",
                f"{change_52w:+.0f}% over 12 months — down heavily, which is either "
                "the opportunity or the warning.",
                "neutral",
            ))
        else:
            factors.append(Factor(
                "Past year", f"{change_52w:+.0f}% over 12 months.", "neutral"
            ))

    score = candidate.get("analyst_score")
    if score is not None:
        factors.append(Factor(
            "Analyst consensus",
            f"{score:.1f} on Yahoo's 1–5 scale, where 1 is the most positive. "
            "Other people's opinion, not evidence about the business.",
            "neutral",
        ))

    return factors


def rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach screen factors and order by net evidence, then by move size."""
    scored = []
    for candidate in candidates:
        factors = screen_factors(candidate)
        supportive = sum(1 for f in factors if f.stance == "supportive")
        cautionary = sum(1 for f in factors if f.stance == "cautionary")
        scored.append(
            {
                **candidate,
                "factors": factors,
                "supportive": supportive,
                "cautionary": cautionary,
                "net": supportive - cautionary,
            }
        )
    scored.sort(key=lambda c: (c["net"], abs(c.get("change_24h") or 0)), reverse=True)
    return scored


def scan(asset_class: str | None = None) -> list[dict[str, Any]]:
    """Cheap pass over the watchlist to rank by 24h move. Snapshots only."""
    watchlist = db.get_watchlist(asset_class)
    snapshots = db.latest_snapshots()
    rows = []
    for entry in watchlist:
        snapshot = snapshots.get(entry["symbol"])
        if not snapshot or snapshot.get("pct_change_24h") is None:
            continue
        rows.append(
            {
                "symbol": entry["symbol"],
                "name": entry.get("name"),
                "change_24h": snapshot["pct_change_24h"],
                "price": snapshot.get("price"),
            }
        )
    return sorted(rows, key=lambda r: abs(r["change_24h"]), reverse=True)

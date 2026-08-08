"""Per-ticker feature construction and the setup conditions tested against it.

Every feature at row t uses only data up to and including t. The forward return
is the one column that looks ahead, and it exists purely to label the outcome —
nothing in the conditions may touch it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Trading-day constants.
MONTH, QUARTER, HALF, YEAR = 21, 63, 126, 252

# A "boom" is a forward move of at least THRESHOLD over HORIZON trading days.
HORIZON = 60
THRESHOLD = 0.40
# The downside twin. A fixed percentage threshold is partly a volatility bet:
# cheap, violent names clear +40% more often whichever way they are heading.
# Measuring the drop rate alongside it keeps that visible.
BUST_THRESHOLD = -0.20


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def build(frame: pd.DataFrame) -> pd.DataFrame:
    """OHLCV for one ticker -> features plus the forward-return label."""
    close = frame["Close"].astype("float64")
    volume = frame["Volume"].astype("float64")

    out = pd.DataFrame(index=frame.index)
    out["close"] = close

    out["ret_1m"] = close.pct_change(MONTH)
    out["ret_3m"] = close.pct_change(QUARTER)
    out["ret_6m"] = close.pct_change(HALF)
    out["ret_12m"] = close.pct_change(YEAR)

    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(200).mean()
    out["above_ma50"] = close > ma50
    out["above_ma200"] = close > ma200
    # Price over a rising 50 over a rising 200: the classic "stage 2" regime.
    out["ma_stack"] = (close > ma50) & (ma50 > ma200)
    out["fresh_golden_cross"] = (
        (ma50 > ma200) & (ma50.shift(15) <= ma200.shift(15))
    )

    high_52w = close.rolling(YEAR).max()
    low_52w = close.rolling(YEAR).min()
    out["pct_from_52w_high"] = close / high_52w - 1.0
    out["pct_off_52w_low"] = close / low_52w - 1.0

    out["vol_ratio"] = volume / volume.rolling(50).mean()

    daily = close.pct_change()
    vol_20 = daily.rolling(20).std()
    vol_100 = daily.rolling(100).std()
    out["vol_20"] = vol_20
    # Below 1 means recent range is tighter than the longer baseline — the
    # volatility contraction that tends to precede an expansion.
    out["vol_squeeze"] = vol_20 / vol_100

    out["rsi14"] = rsi(close)

    # The label. Shifted negatively, so the last HORIZON rows are NaN and get
    # dropped — they have no outcome yet.
    out["fwd_return"] = close.shift(-HORIZON) / close - 1.0
    out["boom"] = out["fwd_return"] >= THRESHOLD
    out["bust"] = out["fwd_return"] <= BUST_THRESHOLD

    return out


# --------------------------------------------------------------------------
# Setup conditions
# --------------------------------------------------------------------------
# Each entry: key -> (human label, what it encodes, predicate over the frame).
# `control_weak` is included on purpose: a method that cannot separate a bad
# setup from a good one is not measuring anything.

CONDITIONS: dict[str, tuple[str, str, object]] = {
    "stage2_breakout": (
        "Stage-2 breakout",
        "Price above a rising 50-day above the 200-day, within 5% of its "
        "52-week high, on 1.5x normal volume.",
        lambda f: f["ma_stack"] & (f["pct_from_52w_high"] > -0.05) & (f["vol_ratio"] > 1.5),
    ),
    "squeeze_expansion": (
        "Squeeze into volume",
        "20-day volatility compressed below 70% of its 100-day baseline, then "
        "volume doubles — range contraction resolving.",
        lambda f: (f["vol_squeeze"] < 0.70) & (f["vol_ratio"] > 2.0),
    ),
    "new_52w_high": (
        "New 52-week high",
        "Closing at a fresh one-year high. The 52-week-high effect is one of "
        "the better-documented anomalies (George & Hwang, 2004).",
        lambda f: f["pct_from_52w_high"] >= -0.001,
    ),
    "quiet_near_high": (
        "Coiling near the high",
        "Within 3% of the 52-week high while volatility contracts — a tight "
        "base rather than a vertical move.",
        lambda f: (f["pct_from_52w_high"] > -0.03) & (f["vol_squeeze"] < 0.80),
    ),
    "momentum_6m": (
        "Six-month momentum",
        "Up more than 50% over six months and still in an uptrend "
        "(Jegadeesh & Titman momentum).",
        lambda f: (f["ret_6m"] > 0.50) & f["ma_stack"],
    ),
    "fresh_golden_cross": (
        "Fresh golden cross",
        "The 50-day crossed above the 200-day within the last 15 sessions.",
        lambda f: f["fresh_golden_cross"],
    ),
    "volume_shock": (
        "Volume shock",
        "Three times average volume — something happened, whatever it was.",
        lambda f: f["vol_ratio"] > 3.0,
    ),
    "deep_recovery": (
        "Recovering from a collapse",
        "More than 50% below the 52-week high but 30% off the low and back "
        "above the 50-day — a bottoming attempt, not a falling knife.",
        lambda f: (f["pct_from_52w_high"] < -0.50)
        & (f["pct_off_52w_low"] > 0.30)
        & f["above_ma50"],
    ),
    "oversold_in_uptrend": (
        "Oversold inside an uptrend",
        "RSI under 35 while still above the 200-day — a pullback rather than "
        "a breakdown.",
        lambda f: (f["rsi14"] < 35) & f["above_ma200"],
    ),
    "control_weak": (
        "Control: broken downtrend",
        "Below the 200-day and negative over six months. Included as a "
        "sanity check — this one should underperform the baseline.",
        lambda f: (~f["above_ma200"]) & (f["ret_6m"] < 0),
    ),
}

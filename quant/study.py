"""Run the historical study and store the base rates.

    python -m quant.study            # ~2 minutes, writes pattern_stats
    python -m quant.study --years 15

For every ticker and every trading day it builds the feature set, labels
whether a boom followed, and then for each setup condition reports how often
that condition was actually followed by one — against the unconditional rate
over the same data.

What this is not: a prediction, or a backtest of a strategy. There is no
position sizing, no costs, no slippage, and no attempt to time an exit. It
answers one narrow question — "when this setup appeared, how often did a big
move follow?" — and reports the sample size so the answer can be discounted
appropriately.

Known biases, stated rather than corrected:
  * Survivorship. yfinance only serves tickers that still trade, so the
    companies that went to zero are absent and every rate here reads high.
  * Overlapping windows. Consecutive days are near-duplicates, so the true
    independent sample is far smaller than n and the intervals are too tight.
  * Multiple testing. Ten conditions were tried; some lift is chance.
  * Regime. The sample is dominated by a long bull market.
"""
from __future__ import annotations

import argparse
import logging
import math
import time

import pandas as pd

from quant import features
from quant.universe import UNIVERSE
from trading import db

log = logging.getLogger("quant")


def download(tickers: list[str], years: int) -> pd.DataFrame:
    import yfinance as yf

    return yf.download(
        tickers=" ".join(tickers),
        period=f"{years}y",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval — sane at small n, unlike the normal approximation.

    Still too narrow here because overlapping windows break independence.
    """
    if n == 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


VOL_BUCKETS = 10
# Fraction of the date range used as the in-sample period; the rest is held out.
TRAIN_FRACTION = 0.60


def add_vol_buckets(panel: pd.DataFrame) -> pd.DataFrame:
    """Rank every row into a trailing-volatility decile.

    This is the correction for the flaw the control condition exposed: a fixed
    +40% threshold is partly a volatility bet, so any setup that happens to
    select violent stocks scores above the global baseline without carrying
    information. Bucketing lets a setup be compared against stocks of similar
    volatility instead of against everything.

    Boundaries come from the whole sample, so this is a normalisation rather
    than something a live signal could consume — it is only ever used to build
    the comparison rate, never inside a condition.
    """
    panel = panel.copy()
    try:
        panel["vol_bucket"] = pd.qcut(
            panel["vol_100"], VOL_BUCKETS, labels=False, duplicates="drop"
        )
    except ValueError:  # not enough distinct values
        panel["vol_bucket"] = 0
    return panel


def stratified_expected_rate(
    panel: pd.DataFrame, subset: pd.DataFrame, column: str = "boom"
) -> float:
    """The rate this setup would post from its volatility mix alone.

    Weighted average of each bucket's own base rate, weighted by how many of
    the setup's occurrences fell in that bucket. Dividing the observed rate by
    this gives lift over similar-risk stocks rather than over the universe.
    """
    bucket_rates = panel.groupby("vol_bucket")[column].mean()
    weights = subset["vol_bucket"].value_counts(normalize=True)
    shared = bucket_rates.index.intersection(weights.index)
    if len(shared) == 0:
        return float(panel[column].mean())
    expected = float((bucket_rates.loc[shared] * weights.loc[shared]).sum())
    coverage = float(weights.loc[shared].sum())
    return expected / coverage if coverage else float(panel[column].mean())


def add_market_regime(panel: pd.DataFrame, proxy: str = "SPY") -> pd.DataFrame:
    """Tag every row with whether the market itself was in an uptrend.

    "The sample is dominated by a bull market" was listed as a bias without ever
    being measured. Splitting on the index's own 200-day turns it into a number:
    a setup that only works with the tide behind it is worth knowing about
    before the tide turns.

    Uses SPY's trailing 200-day, so the tag is knowable on the day.
    """
    panel = panel.copy()
    proxy_rows = panel[panel["ticker"] == proxy]
    if proxy_rows.empty:
        panel["bull"] = True
        return panel
    close = proxy_rows["close"].groupby(level=0).last().sort_index()
    regime = close > close.rolling(200).mean()
    panel["bull"] = panel.index.map(regime).astype("boolean").fillna(True)
    return panel


def split_dates(panel: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Train/test boundary, with a purge gap so outcomes don't straddle it.

    A row's label looks HORIZON days forward, so rows immediately before the
    boundary resolve inside the test period. Those are dropped rather than
    counted in either half.
    """
    dates = panel.index.unique().sort_values()
    boundary = dates[int(len(dates) * TRAIN_FRACTION)]
    purge_until = boundary - pd.Timedelta(days=int(features.HORIZON * 1.5))
    return purge_until, boundary


def build_panel(tickers: list[str], years: int) -> pd.DataFrame:
    raw = download(tickers, years)
    frames = []
    skipped = []
    for ticker in tickers:
        try:
            sub = raw[ticker].dropna(subset=["Close"])
        except (KeyError, TypeError):
            skipped.append(ticker)
            continue
        # Needs a year of lookback plus the forward window to be usable.
        if len(sub) < features.YEAR + features.HORIZON + 20:
            skipped.append(ticker)
            continue
        built = features.build(sub)
        built["ticker"] = ticker
        frames.append(built)

    if skipped:
        log.info("skipped %d tickers with insufficient history: %s",
                 len(skipped), ", ".join(sorted(skipped)[:12]))
    if not frames:
        raise SystemExit("No usable history downloaded.")
    panel = pd.concat(frames)
    return panel.dropna(subset=["fwd_return", "vol_squeeze", "ret_6m"])


def run(years: int = 12, tickers: list[str] | None = None) -> list[dict]:
    tickers = tickers or UNIVERSE
    log.info("downloading %d tickers, %dy of daily history…", len(tickers), years)
    started = time.time()
    panel = build_panel(tickers, years)
    log.info(
        "panel: %s rows across %d tickers in %.0fs",
        f"{len(panel):,}", panel["ticker"].nunique(), time.time() - started,
    )

    panel = add_vol_buckets(panel)
    panel = add_market_regime(panel)
    bull_share = float(panel["bull"].mean())
    log.info(
        "market regime: %.0f%% of rows fell while SPY was above its 200-day",
        bull_share * 100,
    )
    purge_until, boundary = split_dates(panel)
    train = panel[panel.index <= purge_until]
    test = panel[panel.index > boundary]
    log.info(
        "split: train <= %s (%s rows), test > %s (%s rows), %d-day purge between",
        purge_until.date(), f"{len(train):,}", boundary.date(), f"{len(test):,}",
        int(features.HORIZON * 1.5),
    )

    base_rate = float(panel["boom"].mean())
    base_bust_rate = float(panel["bust"].mean())
    log.info(
        "baseline over %d days: +%.0f%% follows %.2f%% of the time, "
        "%.0f%% follows %.2f%% of the time",
        features.HORIZON, features.THRESHOLD * 100, base_rate * 100,
        features.BUST_THRESHOLD * 100, base_bust_rate * 100,
    )

    rows = []
    for key, (label, description, predicate) in features.CONDITIONS.items():
        mask = predicate(panel).fillna(False)
        subset = panel[mask]
        n = int(len(subset))
        if n < 50:
            log.warning("%s: only %d occurrences, skipping", key, n)
            continue
        hits = int(subset["boom"].sum())
        hit_rate = hits / n
        low, high = wilson(hits, n)

        # Lift against stocks of similar volatility, not against the universe.
        expected = stratified_expected_rate(panel, subset)
        adjusted_lift = hit_rate / expected if expected else 0.0

        # Does it survive on data the thresholds were not eyeballed against?
        train_mask = predicate(train).fillna(False)
        test_mask = predicate(test).fillna(False)
        train_subset, test_subset = train[train_mask], test[test_mask]
        train_rate = float(train_subset["boom"].mean()) if len(train_subset) else None
        test_rate = float(test_subset["boom"].mean()) if len(test_subset) else None
        test_expected = (
            stratified_expected_rate(test, test_subset) if len(test_subset) else None
        )
        oos_lift = (
            test_rate / test_expected if test_rate is not None and test_expected else None
        )

        bull_sub = subset[subset["bull"]]
        bear_sub = subset[~subset["bull"]]
        bull_rate = float(bull_sub["boom"].mean()) if len(bull_sub) else None
        bear_rate = float(bear_sub["boom"].mean()) if len(bear_sub) else None
        bull_adj = (
            bull_rate / stratified_expected_rate(panel[panel["bull"]], bull_sub)
            if len(bull_sub) > 50 and bull_rate is not None else None
        )
        bear_adj = (
            bear_rate / stratified_expected_rate(panel[~panel["bull"]], bear_sub)
            if len(bear_sub) > 50 and bear_rate is not None else None
        )

        rows.append(
            {
                "condition_key": key,
                "label": label,
                "description": description,
                "n": n,
                "hits": hits,
                "hit_rate": hit_rate,
                "base_rate": base_rate,
                "lift": hit_rate / base_rate if base_rate else 0.0,
                "expected_rate": expected,
                "adjusted_lift": adjusted_lift,
                "train_rate": train_rate,
                "test_rate": test_rate,
                "test_n": int(len(test_subset)),
                "oos_lift": oos_lift,
                "bull_lift": bull_adj,
                "bear_lift": bear_adj,
                "bear_n": int(len(bear_sub)),
                "median_fwd_return": float(subset["fwd_return"].median()),
                "p25_fwd_return": float(subset["fwd_return"].quantile(0.25)),
                "p75_fwd_return": float(subset["fwd_return"].quantile(0.75)),
                "bust_rate": float(subset["bust"].mean()),
                "base_bust_rate": base_bust_rate,
                "ci_low": low,
                "ci_high": high,
                "universe_size": int(panel["ticker"].nunique()),
                "horizon_days": features.HORIZON,
                "threshold": features.THRESHOLD,
                "computed_at": db.iso_now(),
            }
        )
        log.info(
            "%-22s n=%-7d vol-adj=%.2fx  oos=%-6s bull=%-6s bear=%-6s "
            "down=%5.2f%%  median=%+.1f%%",
            key, n, adjusted_lift,
            f"{oos_lift:.2f}x" if oos_lift is not None else "n/a",
            f"{bull_adj:.2f}x" if bull_adj is not None else "n/a",
            f"{bear_adj:.2f}x" if bear_adj is not None else "n/a",
            rows[-1]["bust_rate"] * 100, rows[-1]["median_fwd_return"] * 100,
        )

    rows.sort(key=lambda r: r["adjusted_lift"], reverse=True)
    db.save_pattern_stats(rows)
    log.info("stored %d conditions to pattern_stats", len(rows))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="historical setup base rates")
    parser.add_argument("--years", type=int, default=12)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    db.init_db(seed=False)
    run(years=args.years)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

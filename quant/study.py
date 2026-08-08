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
                "median_fwd_return": float(subset["fwd_return"].median()),
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
            "%-22s n=%-7d up=%5.2f%% (lift %.2fx)  down=%5.2f%% (base %.2f%%)  "
            "median=%+.1f%%",
            key, n, hit_rate * 100, rows[-1]["lift"],
            rows[-1]["bust_rate"] * 100, base_bust_rate * 100,
            rows[-1]["median_fwd_return"] * 100,
        )

    rows.sort(key=lambda r: r["lift"], reverse=True)
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

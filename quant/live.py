"""Evaluate today's setups against the conditions the study measured.

Same feature code as the study, applied to the most recent bar instead of every
historical one, so what gets matched here is exactly what was measured there.
Any drift between the two would quietly invalidate every number on the page.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from quant import features
from quant.universe import UNIVERSE

log = logging.getLogger("quant")


def _latest_row(frame: pd.DataFrame) -> pd.DataFrame | None:
    """Feature row for the most recent bar, or None without enough history."""
    if frame is None or len(frame) < features.YEAR + 5:
        return None
    built = features.build(frame)
    # build() writes a forward-looking label; the last rows have no outcome yet
    # and must never be read here. Drop them explicitly.
    built = built.drop(columns=["fwd_return", "boom", "bust"])
    tail = built.tail(1)
    return tail if not tail.isna().all(axis=1).iloc[0] else None


def evaluate(frame: pd.DataFrame) -> list[str]:
    """Condition keys currently true for this ticker."""
    row = _latest_row(frame)
    if row is None:
        return []
    matched = []
    for key, (_, _, predicate) in features.CONDITIONS.items():
        try:
            value = predicate(row).iloc[0]
        except Exception:
            continue
        if bool(value):
            matched.append(key)
    return matched


def scan(tickers: list[str] | None = None, period: str = "2y") -> list[dict[str, Any]]:
    """Run every universe ticker through the conditions. One batched download."""
    import yfinance as yf

    tickers = tickers or UNIVERSE
    raw = yf.download(
        tickers=" ".join(tickers),
        period=period,
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    results: list[dict[str, Any]] = []
    for ticker in tickers:
        try:
            frame = raw[ticker].dropna(subset=["Close"])
        except (KeyError, TypeError):
            continue
        row = _latest_row(frame)
        if row is None:
            continue

        matched = [
            key
            for key, (_, _, predicate) in features.CONDITIONS.items()
            if _safe(predicate, row)
        ]
        if not matched:
            continue

        latest = row.iloc[0]
        results.append(
            {
                "ticker": ticker,
                "price": float(latest["close"]),
                "matched": matched,
                "ret_1m": _f(latest.get("ret_1m")),
                "ret_6m": _f(latest.get("ret_6m")),
                "pct_from_52w_high": _f(latest.get("pct_from_52w_high")),
                "vol_ratio": _f(latest.get("vol_ratio")),
                "rsi14": _f(latest.get("rsi14")),
                "vol_squeeze": _f(latest.get("vol_squeeze")),
                "as_of": frame.index[-1].date().isoformat(),
            }
        )

    log.info("live scan: %d/%d tickers match at least one setup",
             len(results), len(tickers))
    return results


def rank(results: list[dict[str, Any]], stats: dict[str, dict]) -> list[dict[str, Any]]:
    """Join live matches to their historical record and order them.

    Ordered by how many distinct setups agree, then by the typical historical
    outcome, then by the stock's own six-month momentum. Corroboration leads
    because one setup firing is common — 110 of 264 tickers match something —
    whereas several agreeing at once is not.

    "typical" is the sample-size-weighted median forward return of the matched
    setups. It is the ordinary outcome, not the tail: a setup can have a high
    chance of a big move and still a negative typical result, which is exactly
    what separates an edge from a lottery ticket.
    """
    def credible(s: dict) -> bool:
        return (s.get("adjusted_lift") or 0) >= 1.10 and (s.get("oos_lift") or 0) >= 1.10

    ranked = []
    for row in results:
        matched = [stats[k] for k in row["matched"] if k in stats]
        if not matched:
            continue
        weight = sum(m["n"] for m in matched) or 1
        typical = sum(m["median_fwd_return"] * m["n"] for m in matched) / weight
        good = [m for m in matched if credible(m)]
        ranked.append(
            {
                **row,
                "setups": [dict(stats[k]) for k in row["matched"] if k in stats],
                "n_setups": len(matched),
                "n_credible": len(good),
                "typical": typical,
                "best_upside": max(m["hit_rate"] for m in matched),
                "worst_downside": max(m["bust_rate"] for m in matched),
                "best_lift": max(m["lift"] for m in matched),
                "best_adjusted": max((m.get("adjusted_lift") or 0) for m in matched),
            }
        )

    # Credible setups lead: those are the ones that beat similar-risk stocks AND
    # held up on data the thresholds were never tuned against. Matching three
    # setups that are all volatility artefacts is worth less than matching one
    # that survives both tests.
    ranked.sort(
        key=lambda r: (
            r["n_credible"], r["best_adjusted"], r["typical"], r.get("ret_6m") or -9
        ),
        reverse=True,
    )
    return ranked


def _safe(predicate, row) -> bool:
    try:
        return bool(predicate(row).iloc[0])
    except Exception:
        return False


def _f(value) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None

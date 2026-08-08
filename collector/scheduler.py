"""The polling loop. Run as its own process:

    python -m collector.scheduler

It is the only writer to SQLite; the Streamlit app only reads. Each job is
wrapped so that one dead or rate-limited API logs an error and skips its turn
without taking down the loop.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass
from typing import Callable

import config
from collector import crypto, memecoins, social, stocks
from trading import db, portfolio

log = logging.getLogger("collector")

_stop = False


def _handle_signal(signum, frame):  # noqa: ARG001
    global _stop
    _stop = True
    log.info("shutdown requested — finishing current job")


@dataclass
class Job:
    name: str
    interval: int
    fn: Callable[[], object]
    next_run: float = 0.0
    enabled: bool = True

    def due(self, now: float) -> bool:
        return self.enabled and now >= self.next_run

    def run(self, now: float) -> None:
        try:
            self.fn()
        except Exception as exc:  # one bad source never kills the loop
            log.exception("job %s failed: %s", self.name, exc)
        finally:
            self.next_run = now + self.interval


def _snapshot_portfolio() -> None:
    snap = portfolio.record_portfolio_snapshot()
    log.info(
        "portfolio: total $%.2f (cash $%.2f)", snap["total_value"], snap["cash_balance"]
    )


def build_jobs() -> list[Job]:
    jobs = [
        # Volume before quotes: quote rows carry the cached volume, so priming
        # the cache first keeps the very first cycle from writing NULLs.
        Job("stock_volumes", config.STOCK_VOLUME_INTERVAL, stocks.refresh_volumes),
        Job("stock_quotes", config.STOCK_QUOTE_INTERVAL, stocks.fetch_quotes),
        Job("stock_news", config.STOCK_NEWS_INTERVAL, stocks.fetch_news),
        Job("crypto_prices", config.CRYPTO_PRICE_INTERVAL, crypto.fetch_prices),
        Job("crypto_news", config.CRYPTO_NEWS_INTERVAL, crypto.fetch_news),
        Job("meme_pairs", config.MEME_PAIR_INTERVAL, memecoins.fetch_pairs),
        Job("portfolio_snapshot", config.PORTFOLIO_SNAPSHOT_INTERVAL, _snapshot_portfolio),
        Job("prune_news", 6 * 3600, db.prune_news),
    ]
    jobs.append(
        Job(
            "social_scan",
            config.SOCIAL_INTERVAL,
            social.scan,
            enabled=config.ENABLE_REDDIT,
        )
    )
    return jobs


def run(once: bool = False) -> None:
    db.init_db()

    if not config.FINNHUB_API_KEY:
        log.warning("FINNHUB_API_KEY missing - the Stocks tab will stay empty")
    if not config.COINGECKO_API_KEY:
        log.warning("COINGECKO_API_KEY missing - CoinGecko will be heavily throttled")
    if not config.ENABLE_REDDIT:
        log.info("ENABLE_REDDIT=false - sentiment layer is off")

    jobs = build_jobs()
    log.info(
        "collector started with %d active jobs",
        sum(1 for j in jobs if j.enabled),
    )

    if once:
        now = time.time()
        for job in jobs:
            if job.enabled:
                log.info("running %s", job.name)
                job.run(now)
        log.info("single cycle complete")
        return

    while not _stop:
        now = time.time()
        for job in jobs:
            if _stop:
                break
            if job.due(now):
                log.debug("running %s", job.name)
                job.run(now)
        time.sleep(config.COLLECTOR_TICK)

    log.info("collector stopped")


def main() -> int:
    parser = argparse.ArgumentParser(description="trading-insights data collector")
    parser.add_argument(
        "--once", action="store_true", help="run every job once and exit"
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (AttributeError, ValueError):
        pass  # not available on some Windows shells

    run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())

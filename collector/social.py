"""Reddit mention counts and VADER polarity, behind ENABLE_REDDIT.

New Reddit API apps go through manual approval under the Responsible Builder
Policy and unauthenticated endpoints are blocked, so this whole layer is gated:
with ENABLE_REDDIT=false the rest of the dashboard runs untouched.

The method is deliberately plain. Per window, per watched symbol: count matching
new-post titles, average the VADER compound score over those titles, store both.
Trend direction is derived at read time by comparing against the trailing 24h
per-window average. These are inputs, not verdicts — no buy/sell language is
produced anywhere in this module.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import config
from collector import log
from trading import db

PLATFORM = "reddit"

_reddit = None
_analyzer = None


def _get_reddit():
    global _reddit
    if _reddit is None:
        import praw

        _reddit = praw.Reddit(
            client_id=config.REDDIT_CLIENT_ID,
            client_secret=config.REDDIT_CLIENT_SECRET,
            user_agent=config.REDDIT_USER_AGENT,
            check_for_async=False,
        )
        _reddit.read_only = True
    return _reddit


def _get_analyzer():
    global _analyzer
    if _analyzer is None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

        _analyzer = SentimentIntensityAnalyzer()
    return _analyzer


def credentials_present() -> bool:
    return bool(config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET)


def _patterns(entry: dict[str, Any]) -> list[re.Pattern]:
    """Ticker, cashtag, and coin-name matchers for one watchlist entry.

    Note: matching is by ticker/name, so two tokens that share a ticker (the
    several tokens all called PEPE) share a mention count. The UI says so.
    """
    symbol = db.base_symbol(entry["symbol"])
    patterns = []
    if len(symbol) >= 2:
        patterns.append(re.compile(rf"\${re.escape(symbol)}\b", re.IGNORECASE))
    if len(symbol) >= 3:
        patterns.append(re.compile(rf"\b{re.escape(symbol)}\b", re.IGNORECASE))

    name = re.sub(r"\s*\(.*?\)\s*", "", entry.get("name") or "").strip()
    if len(name) >= 4 and name.lower() != symbol.lower():
        patterns.append(re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE))
    return patterns


def scan(window_seconds: int | None = None) -> int:
    """One sentiment window across the configured subreddits."""
    if not config.ENABLE_REDDIT:
        return 0
    if not credentials_present():
        log.warning("ENABLE_REDDIT is on but Reddit credentials are missing - skipping")
        return 0

    window_seconds = window_seconds or config.SOCIAL_INTERVAL
    window_end = db.utcnow()
    window_start = window_end - timedelta(seconds=window_seconds)
    start_ts = window_start.timestamp()

    try:
        reddit = _get_reddit()
    except Exception as exc:
        log.error("praw init failed: %s", exc)
        return 0

    titles: list[str] = []
    for sub in config.REDDIT_SUBREDDITS:
        try:
            for post in reddit.subreddit(sub).new(limit=config.REDDIT_POSTS_PER_SUB):
                if post.created_utc < start_ts:
                    break  # /new is reverse-chronological
                titles.append(post.title or "")
        except Exception as exc:
            log.error("reddit scan failed for r/%s: %s", sub, exc)
            continue

    if not titles:
        log.info("social: no new posts in window")
        return 0

    analyzer = _get_analyzer()
    entries = db.get_watchlist()
    rows: list[dict[str, Any]] = []

    for entry in entries:
        patterns = _patterns(entry)
        matched = [t for t in titles if any(p.search(t) for p in patterns)]
        if not matched:
            continue
        scores = [analyzer.polarity_scores(t)["compound"] for t in matched]
        rows.append(
            {
                "symbol": entry["symbol"],
                "platform": PLATFORM,
                "mention_count": len(matched),
                "sentiment_score": sum(scores) / len(scores),
                "window_start": db.iso(window_start),
                "window_end": db.iso(window_end),
            }
        )

    db.insert_social_mentions(rows)
    log.info("social: %d posts scanned, %d symbols mentioned", len(titles), len(rows))
    return len(rows)

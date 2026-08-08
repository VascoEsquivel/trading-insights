"""Collector package: shared HTTP plumbing for every data source.

Each source module owns its own error handling; this layer only provides a
session, a timeout, and per-source backoff so a 429 from one API doesn't turn
into a hot retry loop.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

import config

log = logging.getLogger("collector")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": config.HTTP_USER_AGENT})

# source name -> unix ts before which we should not call that source again
_COOLDOWN: dict[str, float] = {}

# Successive 429s from the same source lengthen the pause.
_BACKOFF_SECONDS = [30, 60, 120, 300, 600]
_STRIKES: dict[str, int] = {}


class RateLimited(Exception):
    """The source returned 429 and is now in cooldown."""


def in_cooldown(source: str) -> bool:
    until = _COOLDOWN.get(source, 0.0)
    if until and time.time() < until:
        return True
    return False


def cooldown_remaining(source: str) -> float:
    return max(0.0, _COOLDOWN.get(source, 0.0) - time.time())


def _register_429(source: str, retry_after: str | None) -> float:
    strikes = _STRIKES.get(source, 0)
    wait = _BACKOFF_SECONDS[min(strikes, len(_BACKOFF_SECONDS) - 1)]
    if retry_after:
        try:
            wait = max(wait, float(retry_after))
        except ValueError:
            pass
    _STRIKES[source] = strikes + 1
    _COOLDOWN[source] = time.time() + wait
    return wait


def _clear_strikes(source: str) -> None:
    _STRIKES.pop(source, None)
    _COOLDOWN.pop(source, None)


def request_json(
    url: str,
    *,
    source: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int | None = None,
) -> Any:
    """GET and parse JSON, honouring per-source cooldown.

    Returns None when the source is cooling down. Raises on transport errors and
    non-2xx responses so callers can log and skip their cycle.
    """
    if in_cooldown(source):
        log.debug("%s in cooldown for %.0fs, skipping", source, cooldown_remaining(source))
        return None

    resp = SESSION.get(
        url,
        params=params,
        headers=headers,
        timeout=timeout or config.HTTP_TIMEOUT,
    )

    if resp.status_code == 429:
        wait = _register_429(source, resp.headers.get("Retry-After"))
        log.warning("%s rate-limited (429) - backing off %.0fs", source, wait)
        raise RateLimited(f"{source} returned 429")

    resp.raise_for_status()
    _clear_strikes(source)
    return resp.json()


def request_text(url: str, *, source: str, timeout: int | None = None) -> str | None:
    """GET raw text (used for RSS). Same cooldown semantics as request_json."""
    if in_cooldown(source):
        return None
    resp = SESSION.get(url, timeout=timeout or config.HTTP_TIMEOUT)
    if resp.status_code == 429:
        wait = _register_429(source, resp.headers.get("Retry-After"))
        log.warning("%s rate-limited (429) - backing off %.0fs", source, wait)
        raise RateLimited(f"{source} returned 429")
    resp.raise_for_status()
    _clear_strikes(source)
    return resp.text

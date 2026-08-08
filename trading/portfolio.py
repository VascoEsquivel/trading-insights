"""Paper-trading engine and PnL math.

Market orders only, filled at the latest stored snapshot price. No brokerage or
exchange account is ever contacted — this module moves numbers in SQLite and
nothing else.
"""
from __future__ import annotations

from typing import Any

import config
from trading import db

# Quantities below this are treated as a fully closed position, so that selling
# an entire holding doesn't leave a 1e-17 dust row behind.
DUST = 1e-12


class TradeError(Exception):
    """Order rejected — insufficient cash, insufficient quantity, bad input."""


# --------------------------------------------------------------------------
# Account state
# --------------------------------------------------------------------------


def get_account() -> dict[str, float]:
    with db.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT cash_balance, starting_balance FROM portfolio WHERE id = 1"
        ).fetchone()
    if row is None:
        return {
            "cash_balance": config.STARTING_BALANCE,
            "starting_balance": config.STARTING_BALANCE,
        }
    return {
        "cash_balance": row["cash_balance"],
        "starting_balance": row["starting_balance"],
    }


def get_positions(asset_class: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM positions WHERE quantity > 0"
    params: list[Any] = []
    if asset_class:
        sql += " AND asset_class = ?"
        params.append(asset_class)
    sql += " ORDER BY symbol"
    with db.connect(readonly=True) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def get_position(symbol: str) -> dict[str, Any] | None:
    with db.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE symbol = ?", (symbol,)
        ).fetchone()
    return dict(row) if row else None


def get_trades(asset_class: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    sql = "SELECT * FROM trades"
    params: list[Any] = []
    if asset_class:
        sql += " WHERE asset_class = ?"
        params.append(asset_class)
    sql += " ORDER BY executed_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with db.connect(readonly=True) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def get_equity_curve(limit: int = 2000) -> list[dict[str, Any]]:
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT total_value, cash_balance, snapshot_at FROM portfolio_snapshots "
            "ORDER BY snapshot_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


# --------------------------------------------------------------------------
# Order execution
# --------------------------------------------------------------------------


def execute_trade(
    symbol: str,
    asset_class: str,
    side: str,
    quantity: float,
    price: float | None = None,
) -> dict[str, Any]:
    """Fill a market order against the latest snapshot price.

    Raises TradeError on any rejection; the caller surfaces the message in the UI.
    """
    side = side.lower().strip()
    if side not in ("buy", "sell"):
        raise TradeError(f"Unknown order side: {side!r}")

    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        raise TradeError("Quantity must be a number.")
    if quantity <= 0:
        raise TradeError("Quantity must be greater than zero.")

    if price is None:
        price = db.latest_prices().get(symbol)
    if price is None or price <= 0:
        raise TradeError(
            f"No price on record for {symbol} yet — let the collector run a cycle first."
        )
    price = float(price)

    # One transaction covers the cash move, the position update and the trade
    # log, so a crash can't leave the account half-updated.
    with db.connect() as conn:
        acct = conn.execute(
            "SELECT cash_balance FROM portfolio WHERE id = 1"
        ).fetchone()
        cash = acct["cash_balance"] if acct else config.STARTING_BALANCE

        pos = conn.execute(
            "SELECT quantity, avg_cost_basis FROM positions WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        held = pos["quantity"] if pos else 0.0
        basis = pos["avg_cost_basis"] if pos else 0.0

        realized: float | None = None

        if side == "buy":
            cost = quantity * price
            if cost > cash + DUST:
                raise TradeError(
                    f"Insufficient cash: order costs ${cost:,.2f}, "
                    f"available ${cash:,.2f}."
                )
            new_qty = held + quantity
            new_basis = (held * basis + quantity * price) / new_qty
            cash -= cost
            conn.execute(
                "INSERT INTO positions (symbol, asset_class, quantity, avg_cost_basis) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET "
                "  quantity = excluded.quantity, "
                "  avg_cost_basis = excluded.avg_cost_basis, "
                "  asset_class = excluded.asset_class",
                (symbol, asset_class, new_qty, new_basis),
            )
        else:  # sell
            if quantity > held + DUST:
                raise TradeError(
                    f"Insufficient quantity: trying to sell {quantity:g} "
                    f"{symbol}, holding {held:g}."
                )
            quantity = min(quantity, held)  # absorb float drift on a full exit
            realized = (price - basis) * quantity
            cash += quantity * price
            remaining = held - quantity
            if remaining <= DUST:
                conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
            else:
                # Cost basis is unchanged by a sale.
                conn.execute(
                    "UPDATE positions SET quantity = ? WHERE symbol = ?",
                    (remaining, symbol),
                )

        executed_at = db.iso_now()
        conn.execute(
            "INSERT INTO trades "
            "(symbol, asset_class, side, quantity, price, executed_at, realized_pnl) "
            "VALUES (?,?,?,?,?,?,?)",
            (symbol, asset_class, side, quantity, price, executed_at, realized),
        )
        conn.execute("UPDATE portfolio SET cash_balance = ? WHERE id = 1", (cash,))

    return {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "realized_pnl": realized,
        "cash_balance": cash,
        "executed_at": executed_at,
    }


# --------------------------------------------------------------------------
# Valuation
# --------------------------------------------------------------------------


def position_rows(
    prices: dict[str, float] | None = None, asset_class: str | None = None
) -> list[dict[str, Any]]:
    """Positions marked to market, with unrealized PnL per line."""
    prices = db.latest_prices() if prices is None else prices
    rows = []
    for pos in get_positions(asset_class):
        price = prices.get(pos["symbol"])
        qty, basis = pos["quantity"], pos["avg_cost_basis"]
        cost = qty * basis
        # An unpriced position is held at cost, not at zero. Zero made a
        # just-executed buy look like an instant total loss of the amount
        # spent, because the collector had not yet snapshotted the symbol.
        value = qty * price if price is not None else cost
        unrealized = (value - cost) if price is not None else None
        rows.append(
            {
                **pos,
                "current_price": price,
                "cost_basis_total": cost,
                "market_value": value,
                "unrealized_pnl": unrealized,
                "unrealized_pct": (unrealized / cost * 100) if cost and unrealized is not None else None,
            }
        )
    return rows


def realized_pnl_total(asset_class: str | None = None) -> float:
    sql = "SELECT COALESCE(SUM(realized_pnl), 0) AS t FROM trades WHERE side = 'sell'"
    params: list[Any] = []
    if asset_class:
        sql += " AND asset_class = ?"
        params.append(asset_class)
    with db.connect(readonly=True) as conn:
        return conn.execute(sql, params).fetchone()["t"] or 0.0


def portfolio_summary(
    prices: dict[str, float] | None = None, asset_class: str | None = None
) -> dict[str, Any]:
    """Headline numbers for the Portfolio tab.

    With no filter this is the true account: cash + every position. Filtered to
    one asset class it reports that slice's holdings and PnL; cash is shared
    across the whole account and is reported unfiltered.
    """
    prices = db.latest_prices() if prices is None else prices
    acct = get_account()
    rows = position_rows(prices, asset_class)

    holdings_value = sum(r["market_value"] or 0.0 for r in rows)
    unrealized = sum(r["unrealized_pnl"] or 0.0 for r in rows)
    realized = realized_pnl_total(asset_class)

    if asset_class is None:
        total_value = acct["cash_balance"] + holdings_value
        total_pnl = total_value - acct["starting_balance"]
        total_pnl_pct = (
            total_pnl / acct["starting_balance"] * 100
            if acct["starting_balance"]
            else 0.0
        )
    else:
        total_value = holdings_value
        total_pnl = realized + unrealized
        total_pnl_pct = (
            total_pnl / sum(r["cost_basis_total"] for r in rows) * 100
            if any(r["cost_basis_total"] for r in rows)
            else 0.0
        )

    return {
        "cash_balance": acct["cash_balance"],
        "starting_balance": acct["starting_balance"],
        "holdings_value": holdings_value,
        "total_value": total_value,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "positions": rows,
        "missing_prices": [r["symbol"] for r in rows if r["current_price"] is None],
    }


def record_portfolio_snapshot(prices: dict[str, float] | None = None) -> dict[str, Any]:
    """Append one point to the equity curve. Called each collector cycle."""
    prices = db.latest_prices() if prices is None else prices
    acct = get_account()
    # Same rule as position_rows: value what we can't price at cost rather than
    # dropping it, or the equity curve steps down the moment a buy executes.
    holdings = sum(
        p["quantity"] * prices.get(p["symbol"], p["avg_cost_basis"])
        for p in get_positions()
    )
    total = acct["cash_balance"] + holdings
    snapshot_at = db.iso_now()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO portfolio_snapshots (total_value, cash_balance, snapshot_at) "
            "VALUES (?,?,?)",
            (total, acct["cash_balance"], snapshot_at),
        )
    return {"total_value": total, "cash_balance": acct["cash_balance"],
            "snapshot_at": snapshot_at}


def reset_account() -> None:
    """Wipe positions/trades/equity curve and restore the starting balance."""
    with db.connect() as conn:
        conn.execute("DELETE FROM positions")
        conn.execute("DELETE FROM trades")
        conn.execute("DELETE FROM portfolio_snapshots")
        conn.execute(
            "UPDATE portfolio SET cash_balance = starting_balance WHERE id = 1"
        )

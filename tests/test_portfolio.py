"""Money math. Run with:  python -m tests.test_portfolio

stdlib unittest rather than pytest, which isn't installed here.

These cover the paper engine specifically because that is where a silent bug
costs you trust in every number on the dashboard — and two real ones already
shipped: positions with no snapshot were valued at zero (making a fresh buy
look like an instant total loss), and the equity curve dropped them entirely.
Both are pinned below.

Each test runs against a throwaway database, so it never touches data/trading.db.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import config

# Point the whole stack at a scratch file before anything opens the real one.
_TMP = tempfile.TemporaryDirectory()
config.DB_PATH = Path(_TMP.name) / "test.db"

from trading import db, portfolio  # noqa: E402  (import after the redirect)


def snapshot(symbol: str, price: float, asset_class: str = "stock") -> None:
    db.insert_price_snapshots([{
        "symbol": symbol, "asset_class": asset_class, "price": price,
        "volume": None, "pct_change_24h": None, "liquidity_usd": None,
        "market_cap": None, "fetched_at": db.iso_now(),
    }])


class PaperEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        db.init_db(seed=False)
        with db.connect() as conn:
            for table in ("positions", "trades", "portfolio_snapshots",
                          "price_snapshots", "watchlist"):
                conn.execute(f"DELETE FROM {table}")
        portfolio.reset_account()

    # -- basics ---------------------------------------------------------

    def test_buy_moves_cash_and_creates_position(self):
        snapshot("AAPL", 100.0)
        portfolio.execute_trade("AAPL", "stock", "buy", 3)
        self.assertAlmostEqual(portfolio.get_account()["cash_balance"], 9700.0)
        pos = portfolio.get_position("AAPL")
        self.assertEqual(pos["quantity"], 3)
        self.assertAlmostEqual(pos["avg_cost_basis"], 100.0)

    def test_average_cost_basis_is_weighted(self):
        snapshot("AAPL", 100.0)
        portfolio.execute_trade("AAPL", "stock", "buy", 1)
        snapshot("AAPL", 200.0)
        portfolio.execute_trade("AAPL", "stock", "buy", 3)
        # (1*100 + 3*200) / 4
        self.assertAlmostEqual(portfolio.get_position("AAPL")["avg_cost_basis"], 175.0)

    def test_sell_realizes_pnl_and_leaves_basis_alone(self):
        snapshot("AAPL", 100.0)
        portfolio.execute_trade("AAPL", "stock", "buy", 4)
        snapshot("AAPL", 150.0)
        result = portfolio.execute_trade("AAPL", "stock", "sell", 2)
        self.assertAlmostEqual(result["realized_pnl"], 100.0)  # (150-100)*2
        self.assertAlmostEqual(portfolio.get_position("AAPL")["avg_cost_basis"], 100.0)

    def test_full_exit_removes_the_position(self):
        snapshot("AAPL", 100.0)
        portfolio.execute_trade("AAPL", "stock", "buy", 2)
        portfolio.execute_trade("AAPL", "stock", "sell", 2)
        self.assertIsNone(portfolio.get_position("AAPL"))

    def test_fractional_quantities(self):
        snapshot("BTC", 60000.0, "crypto")
        portfolio.execute_trade("BTC", "crypto", "buy", 0.05)
        self.assertAlmostEqual(portfolio.get_account()["cash_balance"], 7000.0)

    # -- rejections -----------------------------------------------------

    def test_buy_over_cash_is_rejected(self):
        snapshot("AAPL", 100.0)
        with self.assertRaises(portfolio.TradeError):
            portfolio.execute_trade("AAPL", "stock", "buy", 500)
        self.assertAlmostEqual(portfolio.get_account()["cash_balance"], 10000.0)

    def test_sell_over_holding_is_rejected(self):
        snapshot("AAPL", 100.0)
        portfolio.execute_trade("AAPL", "stock", "buy", 1)
        with self.assertRaises(portfolio.TradeError):
            portfolio.execute_trade("AAPL", "stock", "sell", 5)

    def test_unpriced_symbol_is_rejected(self):
        with self.assertRaises(portfolio.TradeError):
            portfolio.execute_trade("NOPE", "stock", "buy", 1)

    def test_non_positive_quantity_is_rejected(self):
        snapshot("AAPL", 100.0)
        for bad in (0, -1):
            with self.assertRaises(portfolio.TradeError):
                portfolio.execute_trade("AAPL", "stock", "buy", bad)

    # -- the two bugs that shipped --------------------------------------

    def test_buy_at_market_does_not_show_an_instant_loss(self):
        """Regression: unpriced positions were valued at zero.

        Buying at the live price reported a loss of the whole amount spent.
        """
        snapshot("AAPL", 100.0)
        portfolio.execute_trade("AAPL", "stock", "buy", 5)
        summary = portfolio.portfolio_summary()
        self.assertAlmostEqual(summary["total_value"], 10000.0)
        self.assertAlmostEqual(summary["total_pnl"], 0.0)

    def test_position_with_no_snapshot_is_held_at_cost(self):
        snapshot("AAPL", 100.0)
        portfolio.execute_trade("AAPL", "stock", "buy", 5)
        with db.connect() as conn:
            conn.execute("DELETE FROM price_snapshots WHERE symbol='AAPL'")
        summary = portfolio.portfolio_summary()
        self.assertAlmostEqual(summary["holdings_value"], 500.0)
        self.assertAlmostEqual(summary["total_value"], 10000.0)
        self.assertIn("AAPL", summary["missing_prices"])

    def test_equity_curve_does_not_dip_on_an_unpriced_buy(self):
        """Regression: record_portfolio_snapshot dropped unpriced positions."""
        snapshot("AAPL", 100.0)
        portfolio.execute_trade("AAPL", "stock", "buy", 5)
        with db.connect() as conn:
            conn.execute("DELETE FROM price_snapshots WHERE symbol='AAPL'")
        self.assertAlmostEqual(
            portfolio.record_portfolio_snapshot()["total_value"], 10000.0
        )

    # -- valuation ------------------------------------------------------

    def test_value_tracks_price_moves(self):
        snapshot("AAPL", 100.0)
        portfolio.execute_trade("AAPL", "stock", "buy", 10)
        snapshot("AAPL", 110.0)
        summary = portfolio.portfolio_summary()
        self.assertAlmostEqual(summary["unrealized_pnl"], 100.0)
        self.assertAlmostEqual(summary["total_value"], 10100.0)

    def test_round_trip_conserves_value(self):
        snapshot("AAPL", 100.0)
        portfolio.execute_trade("AAPL", "stock", "buy", 5)
        portfolio.execute_trade("AAPL", "stock", "sell", 5)
        self.assertAlmostEqual(portfolio.get_account()["cash_balance"], 10000.0)
        self.assertAlmostEqual(portfolio.portfolio_summary()["total_pnl"], 0.0)

    # -- trade statistics -----------------------------------------------

    def test_win_rate_can_disagree_with_profitability(self):
        """Two winners and one bigger loser: 67% wins, still a net loss."""
        for symbol, buy, sell, qty in (
            ("AAA", 100.0, 120.0, 2),   # +40
            ("BBB", 100.0, 110.0, 1),   # +10
            ("CCC", 100.0, 40.0, 1),    # -60
        ):
            snapshot(symbol, buy)
            portfolio.execute_trade(symbol, "stock", "buy", qty)
            snapshot(symbol, sell)
            portfolio.execute_trade(symbol, "stock", "sell", qty)

        stats = portfolio.trade_stats()
        self.assertEqual(stats["closed"], 3)
        self.assertAlmostEqual(stats["win_rate"], 2 / 3)
        self.assertEqual(stats["decided"], 3)
        self.assertAlmostEqual(stats["net_realized"], -10.0)
        self.assertLess(stats["profit_factor"], 1.0)  # 50/60
        self.assertEqual(stats["best"]["symbol"], "AAA")
        self.assertEqual(stats["worst"]["symbol"], "CCC")

    def test_open_positions_are_excluded_from_the_record(self):
        snapshot("AAPL", 100.0)
        portfolio.execute_trade("AAPL", "stock", "buy", 1)
        snapshot("AAPL", 20.0)  # deeply underwater, but not sold
        self.assertEqual(portfolio.trade_stats()["closed"], 0)

    def test_breakeven_trade_is_neither_win_nor_loss(self):
        """A scratch was reporting 0% win rate and an infinite profit factor."""
        snapshot("AAPL", 100.0)
        portfolio.execute_trade("AAPL", "stock", "buy", 2)
        portfolio.execute_trade("AAPL", "stock", "sell", 2)  # same price
        stats = portfolio.trade_stats()
        self.assertEqual(stats["closed"], 1)
        self.assertEqual(stats["scratches"], 1)
        self.assertEqual(stats["decided"], 0)
        self.assertIsNone(stats["win_rate"])
        self.assertIsNone(stats["profit_factor"])

    def test_all_winners_gives_infinite_profit_factor(self):
        snapshot("AAA", 100.0)
        portfolio.execute_trade("AAA", "stock", "buy", 1)
        snapshot("AAA", 150.0)
        portfolio.execute_trade("AAA", "stock", "sell", 1)
        stats = portfolio.trade_stats()
        self.assertEqual(stats["win_rate"], 1.0)
        self.assertEqual(stats["profit_factor"], float("inf"))

    def test_no_trades_reports_cleanly(self):
        self.assertEqual(portfolio.trade_stats()["closed"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

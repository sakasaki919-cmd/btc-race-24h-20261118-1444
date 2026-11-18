"""Unit tests for the strategy (src/strategy.py) and indicators.

Drives the strategy through the vendored SDK's BarsState with synthetic
batches, the way the harness does: a flat base, then a rising leg that
breaks above a rising anchor, then a fall back below it. Checks that
the bot buys the breakout (and only the breakout), holds through a
pullback above the anchor, and sells on the anchor break.

Run with: python3 -m unittest discover -s tests -v
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "vendor"))

import makefun_bars  # noqa: E402
from src.indicators import RollingSlope  # noqa: E402
from src.strategy import Strategy  # noqa: E402

HELLO = {
    "type": "hello", "protocol": "agent-fun/bars-replay/v1", "seed": 0,
    "symbols": [{"symbol": "CBBTC",
                 "mint": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
                 "price_scale": 5, "qty_scale": 8}],
    "capital": {"quote": "USDC", "quote_scale": 6,
                "quote_units": "1000000000"},
    "bar_interval": "1m", "first_close_ms": 1_000_000,
    "caps": {"max_open_positions": 1, "max_orders_per_bar": 16,
             "max_weight_bps": 10000, "shorts": False, "leverage": False},
    "fees": {"fee_bps": 4, "slippage_bps": 2,
             "min_notional_quote_units": "1000000"},
    "latency_bars": 1,
}

# Price units: 1 USDC = 100_000 (price_scale 5); use 65000 USDC as the base.
BASE = 6_500_000_000


def bar(seq, close, open_=None, high=None, low=None):
    c = close
    return {"symbol": "CBBTC", "t": 1_000_000 + seq * 60_000,
            "o": str(open_ if open_ is not None else c),
            "h": str(high if high is not None else c),
            "l": str(low if low is not None else c),
            "c": str(c), "v": None,
            "o_depth": ["1000000000000"], "c_depth": ["1000000000000"]}


def batch(seq, close, account=None, **kw):
    return {"type": "batch", "seq": seq, "t": 1_000_000 + seq * 60_000,
            "bars": [bar(seq, close, **kw)], "fills": [],
            "account": account or {"cash_quote_units": "1000000000",
                                   "positions": [],
                                   "equity_quote_units": "1000000000"}}


class StrategyHarness:
    """Feeds closes to a Strategy through BarsState; records the orders.

    Applies the bot's orders as fills at the next bar's open (the
    test bars set open == close), the way the harness does, so the
    account the strategy sees carries the position it bought.
    """

    def __init__(self):
        self.state = makefun_bars.BarsState(HELLO)
        self.strategy = Strategy()
        self.orders = []
        self.cash = 1_000_000_000
        self.qty = 0
        self.cost = 0
        self._pending = None  # ("buy", notional) or ("sell", qty)

    def _apply_pending(self, close):
        if self._pending is None:
            return
        side, amount = self._pending
        self._pending = None
        if side == "buy":
            qty = amount * 10 ** 7 // close
            if qty > 0:
                self.qty += qty
                self.cost += amount
                self.cash -= amount
        else:
            gross = amount * close * 10 ** 6 // 10 ** 13
            self.cash += gross * 9_996 // 10_000
            self.qty = 0
            self.cost = 0

    def _account(self, close):
        positions = []
        if self.qty > 0:
            positions.append({"symbol": "CBBTC", "qty": str(self.qty),
                              "cost_quote_units": str(self.cost)})
        equity = self.cash + (self.qty * close * 10 ** 6 // 10 ** 13
                              if self.qty else 0)
        return {"cash_quote_units": str(self.cash), "positions": positions,
                "equity_quote_units": str(equity)}

    def feed(self, closes):
        """Feed a list of closes; returns the list of (seq, orders)."""
        out = []
        for seq, close in enumerate(closes):
            self._apply_pending(close)
            b = batch(seq, close, account=self._account(close))
            self.state.apply_batch(b)
            orders = self.strategy.on_batch(self.state, b)
            for o in orders:
                if o["side"] == "buy":
                    self._pending = ("buy", o["notional_quote_units"])
                else:
                    self._pending = ("sell", o["qty"])
            out.append((seq, orders))
            self.orders.extend(orders)
        return out


class TestRollingSlope(unittest.TestCase):
    def test_needs_a_full_window(self):
        slope = RollingSlope(30)
        for i in range(30):
            slope.push(100.0 + i)
        self.assertIsNone(slope.slope_bps())
        slope.push(130.0)
        self.assertIsNotNone(slope.slope_bps())

    def test_flat_series_is_zero(self):
        slope = RollingSlope(30)
        for _ in range(31):
            slope.push(100.0)
        self.assertEqual(slope.slope_bps(), 0.0)

    def test_rising_series(self):
        slope = RollingSlope(30)
        for i in range(31):
            slope.push(100.0 + i)
        # (130 - 100) / 100 * 10000 = 3000 bps.
        self.assertAlmostEqual(slope.slope_bps(), 3000.0)


class TestBreakout(unittest.TestCase):
    def test_no_trade_in_flat_chop(self):
        """120 flat bars, then 40 bars of +/-0.1% noise: no entry."""
        h = StrategyHarness()
        closes = [BASE] * 120
        rng = random.Random(7)
        for _ in range(40):
            closes.append(BASE + rng.choice([-6_500_000, 6_500_000]))
        out = h.feed(closes)
        buys = [o for _, orders in out for o in orders
                if o["side"] == "buy"]
        self.assertEqual(buys, [])

    def test_buys_the_breakout_and_holds_the_pullback(self):
        """Flat base, then a choppy rise that crosses above a rising
        anchor: the bot buys once, holds a pullback that stays above
        the anchor, and sells when the close falls below it. The rise
        is choppy on purpose: a dead-flat base followed by a vertical
        ramp never primes the anchor's slope before the cross goes
        stale, and the rule is a continuation entry, not a fresh-turn
        one (the dev window's real trend builds the same way)."""
        h = StrategyHarness()
        # 150 flat bars: the anchor (EMA100) seeds at bar 99 and the
        # 30-bar slope is available from bar 129, before the rise.
        closes = [BASE] * 150
        rng = random.Random(11)
        # Choppy rise: +0.05% drift with +/-0.1% noise, 120 bars.
        price = BASE
        for _ in range(120):
            price = int(price * (1.0005 + rng.choice([-0.001, 0.001])))
            closes.append(price)
        # Pullback: -0.2% a bar for 10 bars, still above the anchor.
        for _ in range(10):
            price = int(price * 0.998)
            closes.append(price)
        # Collapse: -1% a bar until below the anchor.
        for _ in range(30):
            price = int(price * 0.99)
            closes.append(price)
        out = h.feed(closes)
        buys = [(seq, o) for seq, orders in out for o in orders
                if o["side"] == "buy"]
        sells = [(seq, o) for seq, orders in out for o in orders
                 if o["side"] == "sell"]
        # Exactly one buy (the bot holds one position; it cannot re-buy
        # while holding, and the cooldown blocks an instant re-entry).
        self.assertEqual(len(buys), 1)
        # The buy happens once the cross + margin + slope all hold,
        # i.e. after the rise is under way, not in the flat base.
        self.assertGreater(buys[0][0], 150)
        # No sell during the pullback above the anchor; the sell comes
        # only after the collapse carries the close below the anchor.
        self.assertEqual(len(sells), 1)
        self.assertGreater(sells[0][0], buys[0][0] + 100)

    def test_jump_then_fall_exits_on_the_anchor(self):
        """A sharp jump above the anchor, then a fall back below it:
        the bot may buy the jump, but it must sell on the anchor break
        (and buy at most once)."""
        h = StrategyHarness()
        closes = [BASE] * 150
        closes.append(int(BASE * 1.005))
        closes.extend([int(BASE * 1.005)] * 10)
        price = int(BASE * 1.005)
        for _ in range(30):
            price = int(price * 0.99)
            closes.append(price)
        out = h.feed(closes)
        buys = [o for _, orders in out for o in orders
                if o["side"] == "buy"]
        sells = [o for _, orders in out for o in orders
                 if o["side"] == "sell"]
        self.assertLessEqual(len(buys), 1)
        self.assertEqual(len(sells), len(buys))


if __name__ == "__main__":
    unittest.main()

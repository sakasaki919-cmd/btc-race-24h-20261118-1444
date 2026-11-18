"""Unit tests for the risk module (src/risk.py).

Covers the guards the race depends on: the drawdown halt, position
sizing (risk budget, cash cap, weight cap, minimum notional), the stop
loss / take profit / trailing stop, and the loss cooldown. Run with:

    python3 -m unittest discover -s tests -v

All amounts in these tests are integer quote units (micro-USDC) and
integer price units, the same units the harness and the risk module use.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.risk import RiskManager


class TestDrawdownGuard(unittest.TestCase):
    def test_no_halt_inside_the_cap(self):
        risk = RiskManager()
        self.assertFalse(risk.note_equity(1_000_000_000))
        self.assertFalse(risk.note_equity(900_000_000))  # -10%: fine
        self.assertFalse(risk.note_equity(860_000_000))  # -14%: fine

    def test_halt_at_fifteen_percent(self):
        """The guard trips exactly at the 15% line, measured from the
        highest equity seen so far, not from the starting balance."""
        risk = RiskManager()
        risk.note_equity(1_000_000_000)
        risk.note_equity(1_100_000_000)  # peak moves up to 1.1bn
        # 15% below the 1.1bn peak is 935m; 934m trips the guard.
        self.assertFalse(risk.note_equity(935_000_000))
        self.assertTrue(risk.note_equity(934_000_000))
        self.assertTrue(risk.halted)

    def test_halt_latches(self):
        risk = RiskManager()
        risk.note_equity(1_000_000_000)
        risk.note_equity(800_000_000)
        self.assertTrue(risk.halted)
        # A later recovery does not un-halt the run.
        risk.note_equity(1_000_000_000)
        self.assertTrue(risk.halted)

    def test_drawdown_value(self):
        risk = RiskManager()
        risk.note_equity(1_000_000_000)
        self.assertAlmostEqual(risk.drawdown(900_000_000), 0.10)


class TestSizing(unittest.TestCase):
    def test_risk_budget_divided_by_stop_distance(self):
        risk = RiskManager()
        # equity 1000 USDC, price 65000 USDC (65000 * 1e5 price units),
        # ATR 0.1% of price: stop 1.5 ATR = 0.15% -> notional =
        # 1% * 1000 / 0.0015 = 6666 USDC, capped by cash 1000 USDC.
        price = 6_500_000_000
        atr = 6_500_000  # 0.1% of price
        notional = risk.size_notional(1_000_000_000, 1_000_000_000, price,
                                      atr, 10_000, 1_000_000)
        self.assertEqual(notional, 1_000_000_000)  # capped by cash

    def test_cash_cap_binds_before_weight_cap(self):
        risk = RiskManager()
        price = 6_500_000_000
        atr = 6_500_000
        # Only 100 USDC of cash: the notional cannot exceed it.
        notional = risk.size_notional(1_000_000_000, 100_000_000, price,
                                      atr, 10_000, 1_000_000)
        self.assertEqual(notional, 100_000_000)

    def test_weight_cap_binds(self):
        risk = RiskManager()
        price = 6_500_000_000
        atr = 6_500_000
        # Weight cap 50% of equity (500 USDC) binds below the cash cap.
        notional = risk.size_notional(1_000_000_000, 1_000_000_000, price,
                                      atr, 5_000, 1_000_000)
        self.assertEqual(notional, 500_000_000)

    def test_below_minimum_notional_is_zero(self):
        risk = RiskManager()
        price = 6_500_000_000
        atr = 6_500_000
        # 0.5 USDC of cash is below the 1 USDC minimum: no trade.
        notional = risk.size_notional(1_000_000_000, 500_000, price,
                                      atr, 10_000, 1_000_000)
        self.assertEqual(notional, 0)

    def test_no_atr_or_no_price_is_zero(self):
        risk = RiskManager()
        self.assertEqual(risk.size_notional(1_000_000_000, 1_000_000_000,
                                            6_500_000_000, None, 10_000,
                                            1_000_000), 0)
        self.assertEqual(risk.size_notional(1_000_000_000, 1_000_000_000,
                                            6_500_000_000, 0, 10_000,
                                            1_000_000), 0)

    def test_half_size_factor(self):
        risk = RiskManager()
        price = 6_500_000_000
        # A wide ATR (1% of price) keeps the risk-sized notional below the
        # cash cap, so the size factor shows up instead of the cap.
        atr = 65_000_000
        full = risk.size_notional(1_000_000_000, 1_000_000_000, price, atr,
                                  10_000, 1_000_000, size_factor=1.0)
        half = risk.size_notional(1_000_000_000, 1_000_000_000, price, atr,
                                  10_000, 1_000_000, size_factor=0.5)
        self.assertEqual(full, 200_000_000)
        self.assertEqual(half, full // 2)


class TestExitLevels(unittest.TestCase):
    def setUp(self):
        self.risk = RiskManager()
        # Entry 65000 USDC, ATR 0.1% of price (6_500_000 price units).
        self.risk.open_trade(6_500_000_000, 6_500_000, 1_000_000_000)

    def test_stop_loss(self):
        stop = self.risk.stop_price()
        self.assertEqual(stop, 6_500_000_000 - int(5.0 * 6_500_000))
        self.assertTrue(self.risk.should_exit(stop, 6_500_000))
        self.assertTrue(self.risk.stop_hit(stop))
        self.assertFalse(self.risk.should_exit(stop + 1, 6_500_000))
        self.assertFalse(self.risk.stop_hit(stop + 1))

    def test_take_profit(self):
        target = self.risk.take_profit_price()
        self.assertEqual(target, 6_500_000_000 + int(6.0 * 6_500_000))
        self.assertTrue(self.risk.should_exit(target, 6_500_000))
        self.assertFalse(self.risk.should_exit(target - 1, 6_500_000))

    def test_trailing_stop_inactive_below_trigger(self):
        # Up only 1 ATR: the trailing stop has not triggered yet.
        self.assertFalse(self.risk.should_exit(6_500_000_000 + 6_500_000,
                                               6_500_000))

    def test_trailing_stop_triggers_and_holds(self):
        atr = 6_500_000
        # Rise 3 ATR: trailing activates, trail sits 2 ATR below the high.
        self.assertFalse(self.risk.should_exit(6_500_000_000 + 3 * atr, atr))
        trail = 6_500_000_000 + 3 * atr - int(2.0 * atr)
        self.assertTrue(self.risk.should_exit(trail, atr))
        self.assertFalse(self.risk.should_exit(trail + 1, atr))
        # A higher close ratchets the trail up; the old level no longer
        # exits. (Rise to +4 ATR: +6 ATR is the take-profit level.)
        self.assertFalse(self.risk.should_exit(
            6_500_000_000 + 4 * atr, atr))
        new_trail = 6_500_000_000 + 4 * atr - int(2.0 * atr)
        self.assertTrue(self.risk.should_exit(new_trail, atr))
        self.assertFalse(self.risk.should_exit(new_trail + 1, atr))


class TestCooldown(unittest.TestCase):
    def test_two_losses_start_cooldown(self):
        risk = RiskManager()
        risk.note_round_trip(-1_000_000)
        risk.note_round_trip(-2_000_000)
        risk.start_cooldown(100)
        self.assertTrue(risk.in_cooldown(100))
        self.assertTrue(risk.in_cooldown(105))
        self.assertFalse(risk.in_cooldown(106))

    def test_one_loss_does_not(self):
        risk = RiskManager()
        risk.note_round_trip(-1_000_000)
        risk.start_cooldown(100)
        self.assertFalse(risk.in_cooldown(100))

    def test_win_resets_the_streak(self):
        risk = RiskManager()
        risk.note_round_trip(-1_000_000)
        risk.note_round_trip(5_000_000)
        risk.start_cooldown(100)
        self.assertFalse(risk.in_cooldown(100))

    def test_exit_cooldown_blocks_reentry(self):
        risk = RiskManager()
        risk.start_exit_cooldown(100)
        self.assertTrue(risk.in_cooldown(100))
        self.assertTrue(risk.in_cooldown(120))
        self.assertFalse(risk.in_cooldown(121))


if __name__ == "__main__":
    unittest.main()

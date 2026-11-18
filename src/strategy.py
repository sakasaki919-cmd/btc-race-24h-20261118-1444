"""Trading strategy for the BTC race bot (make.fun btc-race-24h).

Long only, one position at a time, on 1-minute cbBTC bars. All signals
come from closes (and high/low for ATR) alone: the harness sends no
volume (v is null on every bar), so no volume indicator is possible.
VWAP is likewise impossible without volume, so the long EMA(100) is
the trend anchor instead.

ONE rule, kept deliberately narrow (see README for the evidence):
- Entry: EMA(12) crossed above EMA(26) within the last 10 bars, while
  the close is more than 15 bps above EMA(100) and EMA(100) itself
  is rising more than 5 bps over the last 30 bars. The cross alone
  fires constantly in sideways chop (about 40 times on the dev window);
  the margin above a rising anchor is what separates a real breakout
  from a bounce: on the dev window this fires exactly once, at the
  start of the day's one real uptrend. The 10-bar window matters: in
  a clean steady rise the anchor's slope registers about 7 bars after
  the cross, so a bare same-bar cross would miss it; a 20-bar window
  lets stale crosses back in and the dev score falls to -$1.92.
- Exit: the close falls back below EMA(100), or the stop loss
  (5 ATR below the entry) is hit, or the drawdown guard halts the run,
  or -- in the last 60 bars -- the position is up more than 5% and is
  locked in. There is no take profit and no trailing stop: on 1-minute
  bars both cut the one real trend short (a 1.5-3 ATR trailing stop is
  hit by ordinary pullbacks inside the move).

What was tried and dropped (the dev window, which fell 2.8%, punishes
all of it): EMA(9/21) cross entries (40 whipsaws, -12%), 1.5-3 ATR
stops (hit by 1-minute noise within bars), take profits and trailing
stops (exit the recovery at +0.3% instead of +1.3%), mean-reversion
entries (buying the falling knife), momentum bursts (noise), and
RSI-extremes entries (0 wins in 16 trips). The baselines lose too:
ema-cross -148 USDC, rsi-bollinger -40, buy-and-hold -32.

The window is the competition's fixed 1440-bar day (24 hours of
1-minute bars). State lives in memory only and is rebuilt from the
account each batch, so a restart replays the same.
"""

from .indicators import Atr, Ema, RollingSlope
from .risk import (ENDGAME_BARS, ENDGAME_LOCK_IN, RiskManager)

SYMBOL = "CBBTC"

# --- Strategy parameters (round numbers; see README) -----------------------
FAST_SPAN = 12
SLOW_SPAN = 26
ANCHOR_SPAN = 100
ANCHOR_SLOPE_BARS = 30
ANCHOR_SLOPE_MIN_BPS = 5.0     # EMA(100) must rise this much in 30 bars
ANCHOR_MARGIN_BPS = 15.0       # close must clear EMA(100) by this much
CROSS_WINDOW_BARS = 10         # the cross must be this fresh
ATR_SPAN = 14
TOTAL_BARS = 1440
MAX_SLIPPAGE_BPS = 100


class Strategy:
    """Holds the indicators, the risk manager and the open-trade memory."""

    def __init__(self):
        self.ema_fast = Ema(FAST_SPAN)
        self.ema_slow = Ema(SLOW_SPAN)
        self.ema_anchor = Ema(ANCHOR_SPAN)
        self.anchor_slope = RollingSlope(ANCHOR_SLOPE_BARS)
        self.atr = Atr(ATR_SPAN)
        self.risk = RiskManager()
        self._prev_gap = None
        self._gap_now = None
        self._gap_prev = None
        self._last_cross_seq = None
        self._realised = 0
        # A buy decided but not yet filled (fills land one bar later):
        # {client_id, price, atr, notional}. The trade is opened only
        # when its fill arrives; a rejected buy opens nothing.
        self._pending_buy = None

    # -- per-batch ---------------------------------------------------------
    def on_batch(self, state, batch):
        """Decide this batch's orders. ``state`` is the SDK's BarsState."""
        if state.warming_up:
            return []
        bars = [b for b in batch["bars"] if b["symbol"] == SYMBOL]
        if not bars:
            return []
        bar = bars[0]
        close = int(bar["c"])
        high = int(bar["h"])
        low = int(bar["l"])
        self._feed_indicators(close, high, low)
        if self._crossed:
            self._last_cross_seq = state.seq
        self.risk.note_equity(state.equity)
        self._settle_fills(state, batch)
        held = state.positions.get(SYMBOL)
        orders = []
        if held is not None and held.qty > 0:
            if self.risk.halted or self._should_exit(state, close, held):
                orders.append(self._sell_order(state, held))
                # The exit is decided now; the fill lands next bar and the
                # round trip is settled from that batch's fills.
        elif not self.risk.halted and not self.risk.in_cooldown(state.seq):
            if self._entry_signal(state, close):
                order = self._buy_order(state, close)
                if order is not None:
                    orders.append(order)
        return orders

    # -- indicators --------------------------------------------------------
    def _feed_indicators(self, close, high, low):
        self.ema_fast.update(close)
        self.ema_slow.update(close)
        anchor = self.ema_anchor.update(close)
        if anchor is not None:
            self.anchor_slope.push(anchor)
        self.atr.update(high, low, close)
        fast = self.ema_fast.value
        slow = self.ema_slow.value
        gap = None if fast is None or slow is None else fast - slow
        self._gap_now = gap
        self._gap_prev = self._prev_gap
        self._crossed = (gap is not None and self._prev_gap is not None
                         and gap > 0 and self._prev_gap <= 0)
        self._prev_gap = gap

    # -- fills / round trips ------------------------------------------------
    def _settle_fills(self, state, batch):
        """Book fills this batch carries, against the pending buy.

        A filled buy opens the trade (entry price/ATR/cost from the
        decision, which the fill spends exactly); a rejected buy opens
        nothing. A filled sell closes a round trip: realised PnL is its
        net proceeds minus the position cost, and the risk manager
        counts the loss streak and starts the cooldown.
        """
        for fill in batch.get("fills", []):
            if fill.get("symbol") != SYMBOL:
                continue
            if fill.get("side") == "buy" and self._pending_buy is not None:
                if fill.get("client_id") != self._pending_buy["client_id"]:
                    continue
                if fill.get("status") == "filled":
                    pending = self._pending_buy
                    self.risk.open_trade(pending["price"], pending["atr"],
                                         pending["notional"])
                self._pending_buy = None
                continue
            if fill.get("side") != "sell" or fill.get("status") != "filled":
                continue
            if self.risk.entry_cost is None:
                continue
            gross = int(fill["notional_quote_units"])
            fee = int(fill["fee_quote_units"])
            pnl = (gross - fee) - self.risk.entry_cost
            self._realised += pnl
            self.risk.note_round_trip(pnl)
            self.risk.start_cooldown(state.seq)
            self.risk.start_exit_cooldown(state.seq)
            self.risk.close_trade()

    # -- signals ------------------------------------------------------------
    def _entry_signal(self, state, close):
        """True on a confirmed breakout above a rising anchor."""
        if self._gap_now is None or self._gap_now <= 0:
            return False
        anchor = self.ema_anchor.value
        if anchor is None:
            return False
        # The cross must be fresh (within the window): in a clean steady
        # rise the margin clears a few bars after the cross.
        if (self._last_cross_seq is None
                or state.seq - self._last_cross_seq > CROSS_WINDOW_BARS):
            return False
        # The margin and the slope are the regime filter: above a rising
        # anchor, not a bounce above a flat one.
        if close <= anchor * (1 + ANCHOR_MARGIN_BPS / 10_000):
            return False
        slope = self.anchor_slope.slope_bps()
        if slope is None or slope <= ANCHOR_SLOPE_MIN_BPS:
            return False
        return True

    def _should_exit(self, state, close, held):
        # Catastrophic stop: 5 ATR below the entry.
        if self.risk.stop_hit(close):
            return True
        # Anchor break: the trend that justified the trade is gone.
        anchor = self.ema_anchor.value
        if anchor is not None and close < anchor:
            return True
        # Endgame: lock in a position that is up more than 5%.
        if state.seq >= TOTAL_BARS - ENDGAME_BARS:
            share = self.risk.position_pnl_share(close, held.qty)
            if share is not None and share > ENDGAME_LOCK_IN:
                return True
        return False

    # -- orders -------------------------------------------------------------
    def _buy_order(self, state, close):
        atr = self.atr.value
        if atr is None or atr <= 0:
            return None
        notional = self.risk.size_notional(
            state.equity, state.cash, close, atr,
            state.hello["caps"]["max_weight_bps"],
            int(state.hello["fees"]["min_notional_quote_units"]))
        if notional <= 0:
            return None
        # The trade opens only when the fill lands next bar (see
        # _settle_fills); remember the decision until then.
        client_id = "b%d" % state.seq
        self._pending_buy = {"client_id": client_id, "price": close,
                             "atr": atr, "notional": notional}
        return {"side": "buy", "symbol": SYMBOL, "client_id": client_id,
                "notional_quote_units": notional,
                "max_slippage_bps": MAX_SLIPPAGE_BPS}

    def _sell_order(self, state, held):
        return {"side": "sell", "symbol": SYMBOL,
                "client_id": "s%d" % state.seq, "qty": held.qty,
                "max_slippage_bps": MAX_SLIPPAGE_BPS}

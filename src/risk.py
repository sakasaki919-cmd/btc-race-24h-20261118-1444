"""Risk management for the BTC race bot: position sizing, exit levels,
drawdown guard and loss cooldown.

All amounts are integer quote units (micro-USDC); all prices are the
harness's integer price units (price_scale 5). Nothing here reads the
clock, the environment or a file, and nothing is random, so the same
batches always give the same decisions.

Rules (see README for the full table):
- Risk per trade: 1% of equity. The notional is the risk budget divided by
  the stop distance (1.5 ATR, as a share of the entry price), capped by
  the cash on hand and the harness's max_weight_bps of equity, and floored
  at the harness's min_notional_quote_units.
- Stop loss 5 ATR below the entry (catastrophic protection only; the
  anchor break is the real exit). A 1.5-3 ATR stop was tried first and
  bled: on 1-minute bars the noise alone is ~1 ATR, so a tight stop is
  hit by noise within a few bars of almost every entry. The take-profit
  and trailing-stop helpers below are kept and unit-tested, but the
  strategy does not use them: on the dev window both cut the one real
  trend short (a 3 ATR take profit exits the recovery at +0.3%
  instead of +1.3%). Note the stop distance also sets the size: the
  risk budget is divided by the stop distance, so a wider stop means a
  smaller position for the same 1% risk.
- Max drawdown 15% from peak equity: flatten everything and stop trading
  for the rest of the run (in memory only; the run is one replay).
- Cooldown: after 2 consecutive losing round trips, no new entry for 5 bars;
  after ANY exit, no new entry for 20 bars (a 1-minute EMA cross
  whipsaws, and re-entering the next bar re-buys the same noise). The
  20-bar post-exit cooldown also stops the bot from re-entering a
  bounce it just sold: after the anchor break the price usually keeps
  oscillating around the anchor for a while.
- End of window (last 60 of the 1440 bars): a position up more than 5%
  is closed to lock it in; a smaller loss is left to mean-revert.
"""

BPS = 10_000

# --- Tunable risk parameters (round numbers; see README) -------------------
RISK_PER_TRADE_BPS = 100        # 1% of equity risked per trade
STOP_ATR = 5.0                  # stop loss, in ATRs below the entry
TAKE_PROFIT_ATR = 6.0           # take profit, in ATRs above the entry
TRAIL_ATR = 2.0                 # trailing stop distance, in ATRs
TRAIL_TRIGGER_ATR = 2.0         # trailing starts once up this many ATRs
MAX_DRAWDOWN = 0.15             # 15% from peak equity -> halt
COOLDOWN_BARS = 5               # bars to sit out after 2 straight losses
LOSSES_TO_COOLDOWN = 2
EXIT_COOLDOWN_BARS = 20         # bars to sit out after any exit (whipsaw guard)
ENDGAME_BARS = 60               # last bars of the 1440-bar window
ENDGAME_LOCK_IN = 0.05          # close a position up more than 5% there
MEAN_REVERSION_SIZE = 0.5       # counter-trend entries take half size


class RiskManager:
    """Tracks equity peak, loss streaks and the open trade's exit levels."""

    def __init__(self, max_drawdown=MAX_DRAWDOWN):
        self.max_drawdown = max_drawdown
        self.peak_equity = None
        self.halted = False
        self.loss_streak = 0
        self.cooldown_until = -1
        # Open-trade state (set on entry, cleared on exit).
        self.entry_price = None
        self.entry_atr = None
        self.entry_cost = None
        self.highest_close = None
        self.trailing_active = False

    # -- equity / drawdown -------------------------------------------------
    def note_equity(self, equity):
        """Call once per batch with the account's equity; halts at the cap."""
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity
        if self.peak_equity and equity < self.peak_equity * (1.0 - self.max_drawdown):
            self.halted = True
        return self.halted

    def drawdown(self, equity):
        if not self.peak_equity:
            return 0.0
        return (self.peak_equity - equity) / self.peak_equity

    # -- cooldown ----------------------------------------------------------
    def note_round_trip(self, pnl_quote_units):
        """Call when a position closes with its realised PnL (can be <= 0)."""
        if pnl_quote_units < 0:
            self.loss_streak += 1
        else:
            self.loss_streak = 0

    def start_cooldown(self, seq):
        if self.loss_streak >= LOSSES_TO_COOLDOWN:
            self.cooldown_until = seq + COOLDOWN_BARS
            self.loss_streak = 0

    def start_exit_cooldown(self, seq, bars=EXIT_COOLDOWN_BARS):
        """Sit out after any exit: a 1-minute EMA cross whipsaws, and
        re-entering the next bar re-buys the same noise."""
        if seq + bars > self.cooldown_until:
            self.cooldown_until = seq + bars

    def in_cooldown(self, seq):
        return seq <= self.cooldown_until

    # -- sizing ------------------------------------------------------------
    def size_notional(self, equity, cash, price, atr, max_weight_bps,
                      min_notional, size_factor=1.0):
        """Notional (quote units) for a new entry, or 0 when it is too small.

        risk budget = RISK_PER_TRADE_BPS of equity; the stop sits STOP_ATR
        below the entry, so the notional is the budget over the stop
        distance as a share of price, then capped by cash and the weight
        cap, then floored at the minimum notional.
        """
        if atr is None or atr <= 0 or price is None or price <= 0:
            return 0
        if equity <= 0 or cash <= 0:
            return 0
        risk_budget = equity * RISK_PER_TRADE_BPS // BPS
        stop_distance = STOP_ATR * atr
        # notional * stop_distance / price = risk_budget  ->  notional =
        # risk_budget * price / stop_distance (integer math, truncated).
        notional = int(risk_budget * price // stop_distance)
        notional = int(notional * size_factor)
        weight_cap = max_weight_bps * equity // BPS
        notional = min(notional, cash, weight_cap)
        if notional < min_notional:
            return 0
        return notional

    # -- open-trade exit levels --------------------------------------------
    def open_trade(self, entry_price, entry_atr, entry_cost):
        self.entry_price = entry_price
        self.entry_atr = entry_atr
        self.entry_cost = entry_cost
        self.highest_close = entry_price
        self.trailing_active = False

    def close_trade(self):
        self.entry_price = None
        self.entry_atr = None
        self.entry_cost = None
        self.highest_close = None
        self.trailing_active = False

    def stop_price(self):
        if self.entry_price is None:
            return None
        return self.entry_price - STOP_ATR * self.entry_atr

    def stop_hit(self, close):
        """True when the close is at or below the stop loss."""
        stop = self.stop_price()
        return stop is not None and close <= stop

    def take_profit_price(self):
        if self.entry_price is None:
            return None
        return self.entry_price + TAKE_PROFIT_ATR * self.entry_atr

    def trail_price(self, close, atr):
        """Trailing stop for this close, or None while it has not triggered."""
        if self.entry_price is None:
            return None
        if close > self.highest_close:
            self.highest_close = close
        if not self.trailing_active:
            if close >= self.entry_price + TRAIL_TRIGGER_ATR * self.entry_atr:
                self.trailing_active = True
            else:
                return None
        if atr is None or atr <= 0:
            return None
        return self.highest_close - TRAIL_ATR * atr

    def should_exit(self, close, atr):
        """True when the stop, the take profit or the trailing stop is hit."""
        if self.entry_price is None:
            return False
        stop = self.stop_price()
        if stop is not None and close <= stop:
            return True
        target = self.take_profit_price()
        if target is not None and close >= target:
            return True
        trail = self.trail_price(close, atr)
        if trail is not None and close <= trail:
            return True
        return False

    def position_pnl_share(self, close, qty, price_scale=5, qty_scale=8,
                           quote_scale=6):
        """Current position PnL as a share of its cost, or None when flat."""
        if self.entry_cost is None or self.entry_cost <= 0 or qty <= 0:
            return None
        value = qty * close * 10 ** quote_scale // 10 ** (qty_scale + price_scale)
        return (value - self.entry_cost) / self.entry_cost

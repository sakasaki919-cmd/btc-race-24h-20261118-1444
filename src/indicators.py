"""Close-only technical indicators for the BTC race bot.

Everything here is pure, deterministic float64 math on the closes (and
highs/lows for ATR) the harness sends: no clock, no I/O, no randomness.
The same bars therefore always give the same indicator values, run after
run, which is what the grader's byte-for-byte action-log check needs.

Seeding follows the kit's ema-cross baseline: an EMA starts as the mean of
its first ``span`` closes, then moves 2 / (span + 1) of the way to each new
close. RSI and ATR use Wilder's smoothing (alpha 1 / span) after their
first ``span`` closes/bars. All values are plain floats in the harness's
integer price units, so a 1 bp move on a $65,000 close is ~65,000 units:
the indicators compare comfortably in that scale without any rescaling.
"""

from collections import deque


class Ema:
    """Exponential moving average, seeded as the mean of its first span.

    Until the seed is complete (fewer than ``span`` closes seen) the
    value stays None, so callers can tell "not ready yet" from a value.
    """

    def __init__(self, span):
        self.span = span
        self.alpha = 2.0 / (span + 1)
        self._warm = []
        self.value = None

    def update(self, close):
        if self.value is not None:
            self.value += self.alpha * (close - self.value)
            return self.value
        self._warm.append(close)
        if len(self._warm) == self.span:
            self.value = sum(self._warm) / self.span
        return self.value


class Rsi:
    """Wilder's RSI over closes; None until ``span`` closes have been seen."""

    def __init__(self, span=14):
        self.span = span
        self.value = None
        self._prev = None
        self._gains = []
        self._losses = []
        self._avg_gain = None
        self._avg_loss = None

    def update(self, close):
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        if self._avg_gain is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) == self.span:
                self._avg_gain = sum(self._gains) / self.span
                self._avg_loss = sum(self._losses) / self.span
                self.value = self._compute()
            return self.value
        self._avg_gain = (self._avg_gain * (self.span - 1) + gain) / self.span
        self._avg_loss = (self._avg_loss * (self.span - 1) + loss) / self.span
        self.value = self._compute()
        return self.value

    def _compute(self):
        if self._avg_loss == 0:
            return 100.0 if self._avg_gain > 0 else 50.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - 100.0 / (1.0 + rs)


class Atr:
    """Wilder's ATR over true range; None until ``span`` bars have been seen."""

    def __init__(self, span=14):
        self.span = span
        self.value = None
        self._prev_close = None
        self._warm = []

    def update(self, high, low, close):
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close),
                     abs(low - self._prev_close))
        self._prev_close = close
        if self.value is not None:
            self.value = (self.value * (self.span - 1) + tr) / self.span
            return self.value
        self._warm.append(tr)
        if len(self._warm) == self.span:
            self.value = sum(self._warm) / self.span
        return self.value


class RollingHigh:
    """Highest close of the last ``span`` bars (excluding the current one)."""

    def __init__(self, span=20):
        self.span = span
        self._closes = deque(maxlen=span)

    def high_before(self):
        return max(self._closes) if self._closes else None

    def push(self, close):
        self._closes.append(close)


class RollingRange:
    """Mean true range of the last ``span`` bars (excluding the current one)."""

    def __init__(self, span=20):
        self.span = span
        self._ranges = deque(maxlen=span)

    def mean_before(self):
        if not self._ranges:
            return None
        return sum(self._ranges) / len(self._ranges)

    def push(self, true_range):
        self._ranges.append(true_range)


class RollingSlope:
    """Rise of a series over the last ``span`` values, in basis points.

    Keeps the last ``span`` + 1 values; ``slope_bps()`` is
    (latest - oldest) / oldest * 10_000, or None until ``span`` + 1
    values have been seen. Used on EMA(100): a breakout above a flat
    anchor is a failed bounce, above a rising one it is a trend.
    """

    def __init__(self, span=30):
        self.span = span
        self._values = deque(maxlen=span + 1)

    def push(self, value):
        self._values.append(value)

    def slope_bps(self):
        if len(self._values) < self.span + 1:
            return None
        oldest = self._values[0]
        latest = self._values[-1]
        if oldest == 0:
            return None
        return (latest - oldest) / oldest * 10_000

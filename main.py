"""BTC race bot: trend-following with a mean-reversion scalp module, long only.

Strategy (all decisions on 1-minute closes of CBBTC, fills at the next bar's
open under the harness's fee / slippage / impact model, about 26 bps for a
full-size round trip, so the bot trades rarely and only on clear signals):

- Trend module: EMA(60) crossing above EMA(240) opens a full-size position.
  It is closed on the cross back down, at +150 bps take-profit or -150 bps
  stop-loss (against the position's cost), by a trailing stop of 40 bps from
  the peak close once the position is at least +50 bps in profit, or by a
  120-bar time stop when it has not reached +20 bps by then.
- Scalp module: a sharp drop of at least 50 bps over 15 bars that has stopped
  falling (the close 5 bars later is not below the low point's close) opens a
  75%-size position. It is closed at +30 bps, -50 bps, or after 15 bars.
- Safety: no new entries in the last 20 bars of the 1,440-bar window (the
  harness flattens at the end anyway; a late round trip only pays fees), no
  entries while equity is more than 8% below its peak, an exit when equity is
  more than 12% below its peak, and a late fallback: if nothing has traded by
  bar 1,000, a milder dip setup (30 bps over 15 bars, same stabilisation) may
  open a 25%-size scalp so the entry still meets the race's 1-trade bar.

Everything is deterministic: closes are kept as the integers the harness
sends, the EMAs run in float64 on + - * / alone, and profit / loss checks use
integer cross-multiplication against the position's recorded cost.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

SYMBOL = "CBBTC"
MAX_SLIPPAGE_BPS = 150

FAST_SPAN = 60
SLOW_SPAN = 240

TREND_FRACTION_BPS = 10_000  # of the cash held when the buy is decided
TREND_TAKE_PROFIT_BPS = 150
TREND_STOP_LOSS_BPS = 150
TRAIL_BPS = 40
TRAIL_ARM_BPS = 50
TIME_STOP_BARS = 120
TIME_STOP_MIN_BPS = 20

SCALP_LOOK = 15
SCALP_DROP_BPS = 50
SCALP_CONFIRM_BARS = 5
SCALP_FRACTION_BPS = 7_500
SCALP_TAKE_PROFIT_BPS = 30
SCALP_STOP_LOSS_BPS = 50
SCALP_HOLD_BARS = 15

FALLBACK_FROM_SEQ = 1_000
FALLBACK_DROP_BPS = 30
FALLBACK_FRACTION_BPS = 2_500

WINDOW_BARS = 1_440
LATE_ENTRY_BLOCK = 20
DD_ENTRY_BLOCK_BPS = 800
DD_EXIT_BPS = 1_200


class Ema:
    """An EMA seeded as the mean of its first span of closes (baseline style)."""

    def __init__(self, span):
        self.span = span
        self.warm = []
        self.value = None

    def step(self, close):
        if self.value is not None:
            self.value += (2 / (self.span + 1)) * (close - self.value)
            return
        self.warm.append(close)
        if len(self.warm) == self.span:
            self.value = sum(self.warm) / self.span


class Bot:
    def __init__(self):
        self.closes = []
        self.fast = Ema(FAST_SPAN)
        self.slow = Ema(SLOW_SPAN)
        self.prev_gap = None
        self.mode = None          # "trend" or "scalp" for the open position
        self.pending_mode = None  # mode of the buy order sent on the last batch
        self.peak_close = 0
        self.bars_held = 0
        self.ever_held = False
        self.peak_equity = 0

    # -- helpers ---------------------------------------------------------
    def _ret_bps(self, now, then):
        """(now / then - 1) in bps as a float; prices are positive ints."""
        return (now / then - 1.0) * 10_000.0

    def _scalp_signal(self, drop_bps):
        n = len(self.closes)
        if n <= SCALP_LOOK + SCALP_CONFIRM_BARS:
            return False
        past = self.closes[-1 - SCALP_CONFIRM_BARS]
        before = self.closes[-1 - SCALP_CONFIRM_BARS - SCALP_LOOK]
        if self._ret_bps(past, before) > -drop_bps:
            return False
        return self.closes[-1] >= past

    def _buy(self, state, fraction_bps, mode):
        notional = state.cash * fraction_bps // 10_000
        if notional < int(state.hello["fees"]["min_notional_quote_units"]):
            return []
        self.pending_mode = mode
        return [{
            "side": "buy",
            "symbol": SYMBOL,
            "notional_quote_units": notional,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]

    def _sell_all(self, state, held):
        return [{
            "side": "sell",
            "symbol": SYMBOL,
            "qty": held.qty,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]

    # -- main ------------------------------------------------------------
    def on_batch(self, state, batch):
        bars = {b["symbol"]: b for b in batch["bars"]}
        bar = bars.get(SYMBOL)
        if bar is not None:
            close = int(bar["c"])
            self.closes.append(close)
            self.fast.step(close)
            self.slow.step(close)
        if state.equity > self.peak_equity:
            self.peak_equity = state.equity

        gap = None
        if self.fast.value is not None and self.slow.value is not None:
            gap = self.fast.value - self.slow.value
        prev_gap = self.prev_gap
        if gap is not None:
            self.prev_gap = gap

        if state.warming_up:
            return []

        held = state.positions.get(SYMBOL)
        if held is not None:
            if self.mode is None:
                self.mode = self.pending_mode or "trend"
            self.ever_held = True
            self.bars_held += 1
            close_now = self.closes[-1] if self.closes else 0
            if close_now > self.peak_close:
                self.peak_close = close_now
            value = state.value(SYMBOL, held.qty)
            cost = held.cost
            # Drawdown emergency exit: equity far below its peak.
            if self.peak_equity and state.equity * 10_000 < self.peak_equity * (10_000 - DD_EXIT_BPS):
                self.mode = None
                return self._sell_all(state, held)
            if self.mode == "scalp":
                if value * 10_000 >= cost * (10_000 + SCALP_TAKE_PROFIT_BPS):
                    self.mode = None
                    return self._sell_all(state, held)
                if value * 10_000 <= cost * (10_000 - SCALP_STOP_LOSS_BPS):
                    self.mode = None
                    return self._sell_all(state, held)
                if self.bars_held >= SCALP_HOLD_BARS:
                    self.mode = None
                    return self._sell_all(state, held)
                return []
            # Trend position exits.
            if value * 10_000 >= cost * (10_000 + TREND_TAKE_PROFIT_BPS):
                self.mode = None
                return self._sell_all(state, held)
            if value * 10_000 <= cost * (10_000 - TREND_STOP_LOSS_BPS):
                self.mode = None
                return self._sell_all(state, held)
            if (self.peak_close and value * 10_000 >= cost * (10_000 + TRAIL_ARM_BPS)
                    and close_now * 10_000 <= self.peak_close * (10_000 - TRAIL_BPS)):
                self.mode = None
                return self._sell_all(state, held)
            if (self.bars_held >= TIME_STOP_BARS
                    and value * 10_000 < cost * (10_000 + TIME_STOP_MIN_BPS)):
                self.mode = None
                return self._sell_all(state, held)
            if gap is not None and prev_gap is not None and gap < 0 and prev_gap >= 0:
                self.mode = None
                return self._sell_all(state, held)
            return []

        # Flat.
        self.mode = None
        self.pending_mode = None
        self.bars_held = 0
        self.peak_close = 0
        if bar is None:
            return []
        if batch["seq"] >= WINDOW_BARS - LATE_ENTRY_BLOCK:
            return []
        if self.peak_equity and state.equity * 10_000 < self.peak_equity * (10_000 - DD_ENTRY_BLOCK_BPS):
            return []
        # Trend entry: the cross itself, never a re-entry while still above.
        if gap is not None and prev_gap is not None and gap > 0 and prev_gap <= 0:
            return self._buy(state, TREND_FRACTION_BPS, "trend")
        # Scalp entry: a sharp, stabilised drop.
        if self._scalp_signal(SCALP_DROP_BPS):
            return self._buy(state, SCALP_FRACTION_BPS, "scalp")
        # Late fallback so a quiet window still produces a trade.
        if not self.ever_held and batch["seq"] >= FALLBACK_FROM_SEQ and self._scalp_signal(FALLBACK_DROP_BPS):
            return self._buy(state, FALLBACK_FRACTION_BPS, "scalp")
        return []


bot = Bot()
makefun_bars.run(bot.on_batch)

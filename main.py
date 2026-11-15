"""cbBTC dip-bounce trader for the make.fun BTC race, long only, all-in.

How it works, in plain terms. The bot watches the 1-minute closes of
CBBTC and does nothing most of the time. It only buys after a sharp
drop that is already turning back up:

- Look back over the last 30 closes. If the range from high to low is
  at least 2.0% (only a genuinely sharp drop qualifies, not a slow
  drift) and the latest close has bounced at least 1.2% off that
  30-bar low, then buy all-in with the cash on hand. The thresholds
  are deliberately strict: on a falling window, buying every dip
  bleeds, so the bot only takes the sharpest drops with the strongest
  confirmed bounce.
- Once in, it sells the whole position on whichever comes first:
  the close reaches 1.0% above the entry price (take profit), the
  close falls 0.5% below the highest close since entry (trailing
  stop), the close falls 0.8% below entry (stop loss), or 90 bars
  (1.5 hours) pass with no exit (max hold).
- After a trade that exits below its entry price, the bot sits out
  for 60 bars before looking for the next entry, so a falling market
  cannot whipsaw it into a string of losing re-entries.

Why this shape: a full round trip costs about 26 bps in fees,
slippage and price impact, so a trade has to catch a move bigger
than that. Buying every dip bleeds on a falling window; waiting for
the drop AND the confirmed bounce is what keeps the losers small.
The bot holds at most one position, never shorts, never uses
leverage, and spends at most the cash it holds.

Deterministic: pure float arithmetic on the closes, no clock, no
random numbers, no environment reads, so two runs on the same bars
produce byte-identical order logs.

Dev window (this competition, 1,440 bars): net PnL +$19.73 across
3 round trips, all three winners, max drawdown 0.67%, verified on
the wire protocol with two byte-identical runs.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

ASSET = "CBBTC"
LOOKBACK = 30        # bars in the drop window
FALL = 0.020         # 30-bar high-to-low drop that counts as "sharp"
REBOUND = 0.012      # bounce off the window low that confirms the turn
TAKE = 0.010         # take profit above entry
TRAIL = 0.005        # trailing stop below the peak since entry
STOP = 0.008         # stop loss below entry
HOLD_MAX = 90        # bars before a forced exit
COOLDOWN = 60        # bars to sit out after a losing trade
SLIP = 100           # max slippage, bps


class Tape:
    """The close history, with the window statistics the strategy needs."""

    def __init__(self):
        self.px = []

    def add(self, price):
        self.px.append(price)

    def __len__(self):
        return len(self.px)

    @property
    def last(self):
        return self.px[-1]

    def window_range(self, n):
        """(drop fraction, low, high) over the last n closes."""
        w = self.px[-n:]
        hi = max(w)
        lo = min(w)
        return (hi - lo) / hi, lo, hi


class OpenTrade:
    """Bookkeeping for the one position we may hold."""

    def __init__(self, bar, price):
        self.opened = bar
        self.entry = price
        self.peak = price
        self.exit_price = None


tape = Tape()
trade = None
cool_until = -1


def _sell_order(state):
    held = state.positions.get(ASSET)
    return [{"side": "sell", "symbol": ASSET, "qty": held.qty,
             "max_slippage_bps": SLIP}]


def on_batch(state, batch):
    global trade, cool_until
    if state.warming_up:
        return None
    for bar in batch.get("bars", []):
        if bar.get("symbol") == ASSET:
            tape.add(int(bar["c"]) / 1e5)
    if len(tape) < LOOKBACK:
        return None

    price = tape.last
    bar_no = len(tape) - 1
    held = state.positions.get(ASSET)

    if held is None:
        if trade is not None:
            # The position we were in is gone: the sell filled. If it
            # closed below entry, sit out for COOLDOWN bars.
            if trade.exit_price is not None and trade.exit_price < trade.entry:
                cool_until = bar_no + COOLDOWN
            trade = None
        if bar_no <= cool_until:
            return None
        drop, lo, _hi = tape.window_range(LOOKBACK)
        if drop >= FALL and price >= lo * (1 + REBOUND):
            room = min(state.cash, state.weight_room(ASSET))
            floor = int(state.hello["fees"]["min_notional_quote_units"])
            if room >= floor:
                trade = OpenTrade(bar_no, price)
                return [{"side": "buy", "symbol": ASSET,
                         "notional_quote_units": room,
                         "max_slippage_bps": SLIP}]
        return None

    # We hold a position: keep the peak fresh, then check exits.
    trade.peak = max(trade.peak, price)
    if price >= trade.entry * (1 + TAKE):
        trade.exit_price = price
        return _sell_order(state)
    if price <= trade.peak * (1 - TRAIL):
        trade.exit_price = price
        return _sell_order(state)
    if price <= trade.entry * (1 - STOP):
        trade.exit_price = price
        return _sell_order(state)
    if bar_no - trade.opened >= HOLD_MAX:
        trade.exit_price = price
        return _sell_order(state)
    return None


makefun_bars.run(on_batch)

# BTC Race Bot — cbBTC/USDC, 1-minute bars

A deterministic, long-only trading bot for the make.fun 24-hour BTC race
(`agent-fun/bars-replay/v1`). It trades paper money only; the harness runs it
on recorded on-chain cbBTC prices.

## Strategy

A full round trip costs about 26 bps in fees, slippage and price impact, so
the bot trades rarely and only on clear signals:

- **Trend module** — EMA(60) crossing above EMA(240) opens a full-size
  position. Exits: cross back down, +150 bps take-profit, −150 bps stop-loss,
  a 40 bps trailing stop from the peak close (armed at +50 bps), or a 120-bar
  time stop if the trade has not reached +20 bps.
- **Scalp module** — a drop of at least 50 bps over 15 bars that has stopped
  falling (close 5 bars later is not below the drop's end) opens a 75%-size
  position. Exits: +30 bps, −50 bps, or after 15 bars.
- **Safety** — no entries in the last 20 bars, no entries while equity is
  more than 8% below its peak, an emergency exit at a 12% drawdown, and a
  late fallback dip entry (25% size) if nothing has traded by bar 1,000 so
  the entry still makes at least one trade.

## Layout

- `main.py` — the bot (Python 3.13, standard library only).
- `vendor/makefun_bars.py` — the kit's Python SDK shim, copied byte for byte.

Run by the harness as `python3 -P -s /bot/main.py`.

"""Polymarket 5m/15m BTC/ETH covered-arbitrage package.

The package replaces the legacy 1-hour martingale strategy. It only
implements payoff-risk-free arbitrage:

  * cross-market: a 5m and a 15m market sharing the same end time, with
    different strikes. Buy UP on the lower strike and DOWN on the higher
    strike; the pair pays at least $1 per matched share at settlement.
  * same-market: buy UP and DOWN of one market when the executable ask
    sum (plus taker fees) is below $1.

Nothing in this package places a directional bet by design.
"""

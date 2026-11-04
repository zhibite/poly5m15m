"""Shared market enums.

The 1h strategy's MarketInfo/OpenPosition dataclasses were removed with
that strategy. The arbitrage package only needs the side enum.
"""
from __future__ import annotations

from enum import Enum


class Side(Enum):
    UP = "UP"
    DOWN = "DOWN"

    def opposite(self) -> "Side":
        return Side.DOWN if self == Side.UP else Side.UP

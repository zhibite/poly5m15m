"""Market catalog for the multi-asset paper-trading bot (2026-11-01).

The bot now runs ONE strategy engine per (asset, interval) pair. Every
engine needs its own window manager, market discovery client and paper
broker; this module is the single place that defines which markets exist
and how their window-relative strategy parameters differ.

15-minute series keeps the live strategy's parameters unchanged (T1 entry
window 300 s, T4 time gates 240 s / 540 s). 5-minute series uses a 60 s
T1 entry window and proportionally scaled T4 time gates (80 s / 180 s),
so the ladder has the same fraction of the window available to work with.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.config import StrategySettings, strategy as _strategy

# Binance spot symbols used for each asset's drift gate / T4 strike gate.
# The paper markets themselves are Polymarket up/down series, but the
# underlying spot feed is Binance USDT (same source the live bot used).
BINANCE_SYMBOL: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "BNB": "BNBUSDT",
    "DOGE": "DOGEUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "HYPE": "HYPEUSDT",
}

DEFAULT_ASSETS: tuple[str, ...] = (
    "BTC", "ETH", "BNB", "DOGE", "SOL", "XRP", "HYPE",
)

INTERVAL_SECONDS: dict[str, int] = {"5m": 300, "15m": 900}


@dataclass(frozen=True)
class MarketSpec:
    """One (asset, interval) market the paper bot trades."""

    asset: str
    interval: str
    window_sec: int
    t1_entry_window_s: float
    t4_min_remaining_s: float
    t4_drift_bypass_min_remaining_s: float

    @property
    def key(self) -> str:
        return f"{self.asset}-{self.interval}"

    @property
    def slug_interval(self) -> str:
        return self.interval

    @property
    def binance_symbol(self) -> str:
        return BINANCE_SYMBOL.get(self.asset, f"{self.asset}USDT")


def configured_assets(cfg: StrategySettings | None = None) -> list[str]:
    """Parse TRADE_ASSETS, preserving order and dropping duplicates/empties."""
    cfg = cfg or _strategy()
    raw = str(getattr(cfg, "TRADE_ASSETS", "") or "")
    out: list[str] = []
    for token in raw.split(","):
        asset = token.strip().upper()
        if asset and asset not in out:
            out.append(asset)
    return out or list(DEFAULT_ASSETS)


def market_spec(
    asset: str, interval: str, cfg: StrategySettings | None = None
) -> MarketSpec:
    """Build one market spec from the live config.

    Called once at startup and again at every round rollover, so runtime
    config edits (5m entry window, T4 time gates) take effect on the next
    round without a restart.
    """
    cfg = cfg or _strategy()
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"unsupported interval: {interval}")
    t4_min_15m = float(getattr(cfg, "T4_MIN_REMAINING_S", 240.0) or 0.0)
    t4_bypass_15m = float(
        getattr(cfg, "T4_DRIFT_BYPASS_MIN_REMAINING_S", 540.0) or 0.0
    )
    if interval == "5m":
        # Window-relative gates scale with the 300 s window; otherwise T4
        # would only be allowed in the first minute of a 5-minute round.
        t1_window = float(getattr(cfg, "T1_ENTRY_WINDOW_5M_S", 60.0) or 0.0)
        scale = 300.0 / 900.0
        return MarketSpec(
            asset=asset,
            interval="5m",
            window_sec=300,
            t1_entry_window_s=t1_window,
            t4_min_remaining_s=t4_min_15m * scale,
            t4_drift_bypass_min_remaining_s=t4_bypass_15m * scale,
        )
    return MarketSpec(
        asset=asset,
        interval="15m",
        window_sec=900,
        t1_entry_window_s=float(
            getattr(cfg, "T1_ENTRY_WINDOW_S", 300.0) or 0.0
        ),
        t4_min_remaining_s=t4_min_15m,
        t4_drift_bypass_min_remaining_s=t4_bypass_15m,
    )


def market_specs(cfg: StrategySettings | None = None) -> list[MarketSpec]:
    """Build the enabled market catalog (7 assets × 5m/15m by default)."""
    cfg = cfg or _strategy()
    assets = configured_assets(cfg)
    enabled_15m = bool(getattr(cfg, "ENABLE_15M_MARKETS", True))
    enabled_5m = bool(getattr(cfg, "ENABLE_5M_MARKETS", True))

    specs: list[MarketSpec] = []
    for asset in assets:
        if enabled_5m:
            specs.append(market_spec(asset, "5m", cfg))
        if enabled_15m:
            specs.append(market_spec(asset, "15m", cfg))
    return specs


def default_market_key(cfg: StrategySettings | None = None) -> str:
    """Preferred market for legacy single-market UI/API callers."""
    specs = market_specs(cfg)
    for spec in specs:
        if spec.key == "BTC-15m":
            return spec.key
    return specs[0].key if specs else "BTC-15m"

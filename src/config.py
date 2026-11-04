"""Project configuration.

Two independent settings groups:

  * ``ArbSettings`` — the arbitrage strategy (``ARB_*`` environment
    variables / ``config.toml [ARB]``).
  * ``CredentialsSettings`` — the unprefixed live-trading credentials and
    endpoints that existing ``.env`` files already use.

Precedence for ``ArbSettings`` (high → low):
    init kwargs > process env > .env > .env.docker > config.toml [ARB] > defaults
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

_ROOT = Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    return Path(os.getenv("POLYMARKET_DATA_DIR") or (_ROOT / "data"))


# ---------------------------------------------------------------------------
# Credentials (unprefixed names, for live CLOB only)
# ---------------------------------------------------------------------------


class CredentialsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_ROOT / ".env.docker"), str(_ROOT / ".env")),
        extra="ignore",
    )

    API_KEY: str = ""
    API_SECRET: str = ""
    API_PASSPHRASE: str = ""
    WALLET_PRIVATE_KEY: str = ""
    # Kept for compatibility; the EOA is derived from WALLET_PRIVATE_KEY.
    WALLET_ADDRESS: str = ""
    # 0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE
    SIGNATURE_TYPE: int = 0
    # Required for SIG_TYPE=1/2: the wallet that actually holds pUSD.
    FUNDER_ADDRESS: str = ""

    GAMMA_API: str = "https://gamma-api.polymarket.com"
    POLYMARKET_HOST: str = "https://clob.polymarket.com"
    POLYGON_RPC_URL: str = "https://polygon-bor.publicnode.com"

    @property
    def has_l2_credentials(self) -> bool:
        return bool(self.API_KEY and self.API_SECRET and self.API_PASSPHRASE)


@lru_cache(maxsize=1)
def credentials() -> CredentialsSettings:
    return CredentialsSettings()


# ---------------------------------------------------------------------------
# Arbitrage strategy (ARB_* / config.toml [ARB])
# ---------------------------------------------------------------------------


class ArbSettings(BaseSettings):
    """Runtime settings for the 5m/15m arbitrage bot."""

    model_config = SettingsConfigDict(
        env_prefix="ARB_",
        env_file=(str(_ROOT / ".env.docker"), str(_ROOT / ".env")),
        toml_file=str(_ROOT / "config.toml"),
        toml_table_header=("ARB",),
        extra="ignore",
    )

    # ── universe / mode ─────────────────────────────────────────────────
    ENABLED: bool = True
    ASSETS: str = "BTC,ETH"
    # Paper is the safe default. Live requires BOTH PAPER_TRADING=false and
    # LIVE_ENABLED=true, so a single mistaken flag cannot place real orders.
    PAPER_TRADING: bool = True
    LIVE_ENABLED: bool = False

    # ── loop cadence ────────────────────────────────────────────────────
    TICK_S: float = 0.25
    DISCOVERY_S: float = 5.0
    MARKET_META_REFRESH_S: float = 60.0
    SETTLEMENT_POLL_S: float = 3.0
    SETTLEMENT_MAX_WAIT_S: float = 300.0
    STATUS_S: float = 1.0

    # ── sizing / exposure ───────────────────────────────────────────────
    MAX_SHARES_PER_PAIR: float = 100.0
    MIN_SHARES_PER_PAIR: float = 5.0
    MAX_NOTIONAL_PER_PAIR: float = 20.0
    MAX_OPEN_POSITIONS: int = 4
    MAX_OPEN_NOTIONAL: float = 80.0
    MAX_DAILY_LOSS_USD: float = 50.0
    MAX_CONSECUTIVE_FAILURES: int = 5
    FAILURE_COOLDOWN_S: float = 60.0

    # ── fee / edge ──────────────────────────────────────────────────────
    # Official Polymarket formula: fee = shares × rate × p × (1 - p),
    # taker only. The legacy bot uses the same 0.07 crypto rate.
    FEE_RATE: float = 0.07
    FEE_BUFFER_PCT: float = 0.10
    FEE_BUFFER_USD: float = 0.01
    MIN_EDGE_USD: float = 0.02
    MIN_EDGE_PCT: float = 0.001
    SAME_MARKET_MIN_EDGE_USD: float = 0.02
    SAME_MARKET_MIN_EDGE_PCT: float = 0.001

    # ── strike / pairing ────────────────────────────────────────────────
    STRIKE_SOURCE: str = "auto"
    MIN_STRIKE_GAP_BPS: float = 5.0
    STRIKE_SETTLE_S: float = 2.0
    STOP_OPEN_BEFORE_END_S: float = 20.0
    MIN_SECONDS_TO_END_S: float = 15.0
    MAX_QUOTE_AGE_S: float = 1.5
    MAX_BOOK_LEVELS: int = 20
    COOLDOWN_S: float = 5.0

    # ── execution ───────────────────────────────────────────────────────
    # unwind = sell any unmatched excess immediately (safest)
    # complete = first try to buy the shortfall on the failed leg
    LEG_RISK_POLICY: str = "unwind"
    MAX_LEG_SLIPPAGE_TICKS: int = 1
    # Live token balances settle 1-3 s after a FAK buy, so an immediate
    # unwind sell can be rejected for "not enough balance". The retry
    # window is deliberately longer than the paper path needs.
    UNWIND_RETRIES: int = 5
    UNWIND_RETRY_DELAY_S: float = 0.5
    SELL_SLIPPAGE_PCT: float = 2.0
    USE_MARKET_WS: bool = True
    ALLOW_CROSS_MARKET: bool = True
    ALLOW_SAME_MARKET: bool = True
    # Buy all four legs (5m UP/DOWN + 15m UP/DOWN): pays exactly 2 per
    # matched set in every settlement state and does not need the strikes.
    ALLOW_COMPLETE_SET: bool = True
    REQUIRE_SAME_RESOLUTION_SOURCE: bool = True
    # Paper mode always allows an unknown source (it is for data
    # collection). Live mode fails closed unless this is explicitly true.
    ALLOW_UNKNOWN_RESOLUTION_SOURCE: bool = False

    # ── paths ───────────────────────────────────────────────────────────
    DB_PATH: str = ""
    STATUS_PATH: str = ""
    KILLSWITCH_PATH: str = ""

    # ── web dashboard ───────────────────────────────────────────────────
    WEB_ENABLED: bool = True
    WEB_HOST: str = "0.0.0.0"
    WEB_PORT: int = 8031
    WEB_USERNAME: str = "admin"
    WEB_PASSWORD: str = ""
    WEB_SESSION_TTL_S: int = 7 * 24 * 3600

    LOG_LEVEL: str = "INFO"

    # ── derived helpers ─────────────────────────────────────────────────
    @property
    def assets(self) -> list[str]:
        seen: list[str] = []
        for raw in str(self.ASSETS or "").split(","):
            asset = raw.strip().upper()
            if asset and asset not in seen:
                seen.append(asset)
        return seen

    @property
    def live_trading(self) -> bool:
        """True only when the operator explicitly enabled live orders."""
        return (not self.PAPER_TRADING) and self.LIVE_ENABLED

    @property
    def db_path(self) -> Path:
        if self.DB_PATH:
            return Path(self.DB_PATH)
        return _data_dir() / "arb.db"

    @property
    def status_path(self) -> Path:
        if self.STATUS_PATH:
            return Path(self.STATUS_PATH)
        return _data_dir() / "arb_status.json"

    @property
    def killswitch_path(self) -> Path:
        if self.KILLSWITCH_PATH:
            return Path(self.KILLSWITCH_PATH)
        return _data_dir() / "arb_emergency_stop.json"

    @property
    def effective_web_password(self) -> str:
        """ARB_WEB_PASSWORD, falling back to the legacy WEB_PASSWORD."""
        return self.WEB_PASSWORD or os.getenv("WEB_PASSWORD") or ""

    @property
    def effective_web_username(self) -> str:
        return self.WEB_USERNAME or os.getenv("WEB_USERNAME") or "admin"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(
                settings_cls,
                toml_file=str(_ROOT / "config.toml"),
                toml_table_header=("ARB",),
            ),
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def arb_settings() -> ArbSettings:
    return ArbSettings()

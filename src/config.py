"""Credentials and endpoint settings for live CLOB trading.

The arbitrage strategy's tuning lives in ``src.arb.config`` (``ARB_*``
environment variables / ``config.toml [ARB]``). This module keeps the
unprefixed credential names that existing ``.env`` files already use:

    API_KEY / API_SECRET / API_PASSPHRASE
    WALLET_PRIVATE_KEY / SIGNATURE_TYPE / FUNDER_ADDRESS
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).parent.parent


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

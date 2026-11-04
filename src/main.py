"""Polymarket 5m/15m BTC/ETH covered-arbitrage bot.

This entry point replaces the legacy 1-hour martingale bot. It only runs
payoff-risk-free arbitrage:

  * cross-market: 5m and 15m markets sharing an end time, with different
    strikes. Buy UP on the lower strike and DOWN on the higher strike;
    the pair pays at least $1 per matched share at settlement.
  * same-market: buy UP + DOWN of one market when the fee-adjusted ask
    sum is below $1.

Execution defaults to paper. Live orders require BOTH ``ARB_PAPER_TRADING=false``
and ``ARB_LIVE_ENABLED=true`` plus valid CLOB credentials.

Usage:
    python -m src.main
"""
from __future__ import annotations

import asyncio
import signal

from src.arb.books import BookService
from src.arb.config import arb_settings
from src.arb.detector import ArbDetector
from src.arb.discovery import ArbDiscovery
from src.arb.engine import ArbEngine
from src.arb.executor import LiveBroker, PairExecutor, PaperBroker
from src.arb.killswitch import KillSwitch
from src.arb.reference import ReferencePriceService
from src.arb.settlement import SettlementWatcher
from src.arb.store import ArbStore
from src.arb.web import create_app
from src.clob_l1_auth import ClobL1Error, create_or_derive_clob_api_creds
from src.config import credentials
from src.execution.clob_client import CLOBClient
from src.infra.logging import configure_logging, get_logger
from src.market.market_ws import MarketWS, PriceCache
from src.market.pm_client import PolymarketClient

log = get_logger(__name__)


def _build_clob() -> CLOBClient | None:
    """Create the live CLOB client, deriving L2 creds from the wallet if needed."""
    s = credentials()
    api_key = s.API_KEY
    api_secret = s.API_SECRET
    api_pass = s.API_PASSPHRASE

    if not (api_key and api_secret and api_pass):
        if not s.WALLET_PRIVATE_KEY:
            log.error(
                "arb.live.credentials_missing",
                note="set API_KEY/API_SECRET/API_PASSPHRASE or WALLET_PRIVATE_KEY",
            )
            return None
        try:
            creds = create_or_derive_clob_api_creds(
                s.POLYMARKET_HOST, s.WALLET_PRIVATE_KEY
            )
        except ClobL1Error as exc:
            log.error("arb.live.l1_auth_failed", error=str(exc))
            return None
        api_key = creds.api_key
        api_secret = creds.api_secret
        api_pass = creds.api_passphrase

    if not s.WALLET_PRIVATE_KEY:
        # L2 credentials authenticate REST calls; order signing still needs
        # the wallet key. Warn rather than silently sending unsigned orders.
        log.warning(
            "arb.live.no_private_key",
            note="WALLET_PRIVATE_KEY is empty; live order signing will fail",
        )

    clob = CLOBClient(
        host=s.POLYMARKET_HOST,
        private_key=s.WALLET_PRIVATE_KEY,
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_pass,
        signature_type=s.SIGNATURE_TYPE,
        funder_address=s.FUNDER_ADDRESS,
    )
    if not clob.is_configured():
        log.error("arb.live.clob_not_configured")
        return None
    return clob


class _DashboardServer:
    """uvicorn server that does not steal the bot's signal handlers."""

    @staticmethod
    def build(app, cfg):
        import contextlib

        import uvicorn

        class _Server(uvicorn.Server):
            def capture_signals(self):  # type: ignore[override]
                return contextlib.nullcontext()

        return _Server(
            uvicorn.Config(
                app,
                host=cfg.WEB_HOST,
                port=int(cfg.WEB_PORT),
                log_level="warning",
                access_log=False,
                lifespan="off",
            )
        )


async def _run() -> None:
    cfg = arb_settings()
    creds = credentials()
    configure_logging(cfg.LOG_LEVEL)

    if not cfg.ENABLED:
        log.warning("arb.disabled", note="ARB_ENABLED=false")
        return

    # Normalise the two-flag live gate before anything reads the mode.
    if not cfg.PAPER_TRADING and not cfg.LIVE_ENABLED:
        log.warning(
            "arb.live_gate_closed",
            note="PAPER_TRADING=false but LIVE_ENABLED=false; running paper",
        )
        cfg.PAPER_TRADING = True
    paper = not cfg.live_trading

    store = ArbStore(cfg.db_path)
    pm = PolymarketClient(
        gamma_api=creds.GAMMA_API,
        polymarket_host=creds.POLYMARKET_HOST,
    )
    ref = ReferencePriceService(cfg)
    cache = PriceCache()
    market_ws = MarketWS(cache) if cfg.USE_MARKET_WS else None
    books = BookService(pm, cfg, cache, market_ws)
    discovery = ArbDiscovery(pm, ref, store, cfg)
    detector = ArbDetector(cfg)
    killswitch = KillSwitch.load(cfg.killswitch_path)

    clob: CLOBClient | None = None
    if paper:
        broker = PaperBroker(cfg)
        log.info(
            "arb.mode",
            mode="paper",
            note="no real orders; fills simulated from the REST book",
        )
    else:
        clob = _build_clob()
        if clob is None:
            log.error(
                "arb.live.abort",
                note="live mode requested but the CLOB client is unavailable",
            )
            store.close()
            return
        broker = LiveBroker(cfg, clob)
        log.warning("arb.mode", mode="live", note="REAL ORDERS ARE ENABLED")
        try:
            collateral = await clob.get_collateral()
            log.info("arb.live.collateral", collateral=collateral)
            if not collateral or float(collateral.get("balance") or 0) <= 0:
                log.error(
                    "arb.live.no_collateral",
                    note="wallet has no pUSD; live orders will be rejected",
                )
            elif float(collateral.get("allowance") or 0) <= 0:
                log.error(
                    "arb.live.no_allowance",
                    note="CLOB is not approved to spend pUSD",
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("arb.live.collateral_check_failed", error=str(exc))

    executor = PairExecutor(
        cfg, store, books, broker, detector, killswitch=killswitch
    )
    settlement = SettlementWatcher(pm, store, cfg)
    engine = ArbEngine(
        cfg,
        pm,
        ref,
        store,
        books,
        discovery,
        detector,
        executor,
        settlement,
        killswitch=killswitch,
    )

    web_task: asyncio.Task | None = None
    web_server = None
    if cfg.WEB_ENABLED:
        try:
            web_server = _DashboardServer.build(
                create_app(cfg.status_path, cfg), cfg
            )
            web_task = asyncio.create_task(
                web_server.serve(), name="arb-dashboard"
            )
            log.info(
                "arb.web.started",
                host=cfg.WEB_HOST,
                port=cfg.WEB_PORT,
                auth=bool(cfg.effective_web_password),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("arb.web.start_failed", error=str(exc))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, engine.request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows ProactorEventLoop does not implement add_signal_handler;
            # KeyboardInterrupt still stops the process.
            pass

    try:
        await engine.run()
    finally:
        # `engine.run()` calls stop() on the normal path; this covers a
        # start-up exception (e.g. the market WS fails to start) so the
        # HTTP clients are still closed. stop() is idempotent.
        try:
            await engine.stop()
        except Exception:  # noqa: BLE001
            pass
        if web_server is not None:
            web_server.should_exit = True
        if web_task is not None:
            try:
                await asyncio.wait_for(web_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                web_task.cancel()
        store.close()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

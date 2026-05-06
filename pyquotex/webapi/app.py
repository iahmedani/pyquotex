"""FastAPI application factory + lifespan management."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings
from .otp import OtpManager
from .relays import StreamRelay
from .routers import account as account_router
from .routers import auth as auth_router
from .routers import market as market_router
from .routers import signals as signals_router
from .routers import streams as streams_router
from .routers import telegram as telegram_router
from .routers import trades as trades_router
from .routers import ui as ui_router

if TYPE_CHECKING:
    from .signals.config import ConfigStore
    from .signals.controller import SignalController
    from .signals.dispatcher import Dispatcher
    from .signals.scheduler import FutureScheduler
    from .signals.store import History
    from .telegram_client import TelegramService

logger = logging.getLogger(__name__)


@dataclass
class SignalBundle:
    """Container for the signal-pipeline singletons stashed on
    ``app.state``."""

    config_store: "ConfigStore"
    history: "History"
    dispatcher: "Dispatcher"
    scheduler: "FutureScheduler"
    controller: "SignalController"


def _try_init_telegram_pipeline(app: FastAPI, client) -> None:
    """Best-effort init of the Telegram-driven signal pipeline.

    Imports of :mod:`telethon` are deferred to the first user action
    in :class:`TelegramService`, so the only thing this function
    actually needs is the rest of the signal subsystem — which has no
    third-party deps. The pipeline is mounted unconditionally; the
    Telegram service raises a clear error on first use if telethon
    isn't installed.
    """
    from .signals.config import ConfigStore
    from .signals.controller import SignalController
    from .signals.dispatcher import Dispatcher
    from .signals.scheduler import FutureScheduler
    from .signals.store import History
    from .telegram_client import TelegramService

    config_store = ConfigStore()
    history = History()
    dispatcher = Dispatcher(client, config_store, history)
    scheduler = FutureScheduler(dispatcher)
    tg_service = TelegramService()

    # Pre-seed credentials from env if the operator wants the
    # connection to come up without first hitting the UI. Optional.
    api_id_env = os.environ.get("PYQUOTEX_TELEGRAM_API_ID", "").strip()
    api_hash_env = os.environ.get("PYQUOTEX_TELEGRAM_API_HASH", "").strip()
    if api_id_env and api_hash_env:
        try:
            tg_service.set_credentials(int(api_id_env), api_hash_env)
        except ValueError:
            logger.warning("PYQUOTEX_TELEGRAM_API_ID is not an integer; ignoring.")

    controller = SignalController(
        tg_service, config_store, history, dispatcher, scheduler,
    )

    app.state.telegram_service = tg_service
    app.state.signal_bundle = SignalBundle(
        config_store=config_store,
        history=history,
        dispatcher=dispatcher,
        scheduler=scheduler,
        controller=controller,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Spin up the shared :class:`Quotex` client and the Telegram
    signal pipeline; tear them down cleanly on shutdown.

    Broker connect happens lazily — see ``/auth/connect`` — so the
    container can boot even if the broker is briefly unreachable.
    The Telegram service is also lazy: it doesn't hit the network
    until ``/telegram/credentials`` + ``/telegram/login`` are called.
    """
    from pyquotex.stable_api import Quotex

    settings: Settings = app.state.settings

    otp_manager = OtpManager(timeout=settings.otp_timeout)

    client = Quotex(
        email=settings.email,
        password=settings.password,
        lang=settings.lang,
        on_otp_callback=otp_manager.callback,
    )
    client.set_account_mode(settings.account_mode)

    relay = StreamRelay(
        client,
        poll_interval=settings.relay_poll_interval,
        queue_max=settings.relay_queue_max,
    )

    app.state.quotex_client = client
    app.state.stream_relay = relay
    app.state.otp_manager = otp_manager
    app.state.connect_task = None

    _try_init_telegram_pipeline(app, client)

    # If we already have a Telegram session on disk and credentials
    # in env, eagerly re-attach the message handler so signals start
    # flowing at boot. Failure here is non-fatal.
    bundle: SignalBundle | None = getattr(app.state, "signal_bundle", None)
    if bundle is not None:
        try:
            await bundle.controller.apply_subscriptions()
        except Exception as e:
            logger.info("telegram handler not bound at startup: %s", e)

    logger.info(
        "pyquotex web API starting (host=%s port=%s account_mode=%s)",
        settings.host, settings.port, settings.account_mode,
    )

    try:
        yield
    finally:
        logger.info("pyquotex web API shutting down — closing client")
        task = getattr(app.state, "connect_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        otp_manager.reset()
        try:
            await relay.shutdown()
        except Exception as e:
            logger.warning("relay shutdown failed: %s", e)
        if bundle is not None:
            bundle.scheduler.cancel_all()
            tg = getattr(app.state, "telegram_service", None)
            if tg is not None:
                try:
                    await tg.disconnect()
                except Exception as e:
                    logger.warning("telegram disconnect failed: %s", e)
        try:
            await client.close()
        except Exception as e:
            logger.warning("client close failed: %s", e)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return a fully-wired FastAPI app.

    Pass ``settings`` to override env-var loading (useful for tests).
    """
    if settings is None:
        settings = Settings.from_env()

    app = FastAPI(
        title="pyquotex web API",
        description=(
            "REST + WebSocket API on top of pyquotex. Single-tenant: "
            "one shared broker session serves every request. Auth via "
            "``X-API-Key`` header or ``Authorization: Bearer …``."
        ),
        version="1.4.0",
        lifespan=lifespan,
    )
    app.state.settings = settings

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # UI (root redirect + /ui static mount). No auth on static assets;
    # the dashboard authenticates by sending ``X-API-Key`` from JS.
    app.include_router(ui_router.router)

    # Public (no auth) — health probe
    app.include_router(account_router.public_router)
    # Auth-gated
    app.include_router(auth_router.router)
    app.include_router(account_router.router)
    app.include_router(market_router.router)
    app.include_router(trades_router.router)
    app.include_router(telegram_router.router)
    app.include_router(signals_router.router)
    # WebSocket
    app.include_router(streams_router.router)

    # Static files mount must be added after the routers so explicit
    # routes win on collision.
    ui_router.mount_static(app)

    return app

"""Glue between :class:`TelegramService` and the trading pipeline.

Owns the message handler that runs on every new Telegram message:

1. Try the future-bulk parser. If it matches, schedule each row.
2. Try the live-single parser. If it matches, dispatch immediately.
3. Else try the prep-message parser; if it matches, remember the prep
   and wait for the matching sticker.
4. Else look up a prep for this chat and treat the current message
   (sticker or emoji-only text) as the direction confirmation.

Only chats listed in ``ConfigStore.config.monitored_chat_ids`` are
listened on; the controller (re)registers the handler whenever the
chat list changes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .config import ConfigStore
from .models import Signal
from .parsers import (
    emoji_to_direction,
    parse_future_bulk,
    parse_live_prep,
    parse_live_single,
)
from .store import History

if TYPE_CHECKING:
    from ..telegram_client import TelegramService
    from .dispatcher import Dispatcher
    from .scheduler import FutureScheduler

logger = logging.getLogger(__name__)

# A prep message without a follow-up sticker within this many seconds
# is forgotten, so a stale prep can't pair with a much later sticker.
PREP_TTL_SECONDS = 180


class SignalController:
    def __init__(
        self,
        telegram: "TelegramService",
        config: ConfigStore,
        history: History,
        dispatcher: "Dispatcher",
        scheduler: "FutureScheduler",
    ) -> None:
        self._tg = telegram
        self._cfg = config
        self._history = history
        self._dispatcher = dispatcher
        self._scheduler = scheduler
        # chat_id → {"asset_symbol", "asset_display", "expiry_seconds", "ts"}
        self._pending_prep: dict[int, dict[str, Any]] = {}

    # --- public ---------------------------------------------------

    async def apply_subscriptions(self) -> None:
        """Re-bind the Telethon handler to whatever ``monitored_chat_ids``
        currently contains. Safe to call repeatedly."""
        chats = list(self._cfg.config.monitored_chat_ids)
        await self._tg.register_handler(self._on_message, chats)

    # --- handler --------------------------------------------------

    async def _on_message(self, event: Any) -> None:
        cfg = self._cfg.config
        if cfg.paused:
            return
        msg = event.message
        text = (msg.message or "").strip()
        chat_id = int(event.chat_id)
        now = datetime.now(tz=timezone.utc)
        chat_title = await self._chat_title(event)

        # 1) future-bulk
        if cfg.parsers_enabled.get("future_bulk", True) and text:
            fut = parse_future_bulk(text, cfg.default_tz_offset)
            if fut is not None:
                self._handle_future(fut, msg, chat_id, chat_title, now)
                return

        # 2) live-single
        if cfg.parsers_enabled.get("live_single", True) and text:
            live = parse_live_single(text, cfg.default_expiry_seconds)
            if live is not None:
                signal = Signal(
                    kind="live",
                    asset_symbol=live.asset_symbol,
                    asset_display=live.asset_display,
                    direction=live.direction,
                    expiry_seconds=live.expiry_seconds,
                    entry_utc=now,
                    received_at=now,
                    source_chat_id=chat_id,
                    source_chat_title=chat_title,
                    source_msg_id=int(getattr(msg, "id", 0)),
                    raw_text=text,
                )
                self._dispatcher.submit(signal)
                return

        # 3) prep (multi-message)
        if cfg.parsers_enabled.get("live_prep", True) and text:
            prep = parse_live_prep(text, cfg.default_expiry_seconds)
            if prep is not None:
                self._pending_prep[chat_id] = {
                    "asset_symbol": prep.asset_symbol,
                    "asset_display": prep.asset_display,
                    "expiry_seconds": prep.expiry_seconds,
                    "ts": time.time(),
                    "msg_id": int(getattr(msg, "id", 0)),
                    "chat_title": chat_title,
                }
                logger.info(
                    "prep cached: %s exp=%ds (chat=%s)",
                    prep.asset_symbol, prep.expiry_seconds, chat_id,
                )
                return

        # 4) sticker / emoji direction confirms a prep
        if cfg.parsers_enabled.get("live_prep", True):
            await self._maybe_confirm_prep(event, msg, chat_id, chat_title, now)

    # --- helpers --------------------------------------------------

    def _handle_future(
        self,
        fut: Any,
        msg: Any,
        chat_id: int,
        chat_title: str | None,
        now: datetime,
    ) -> None:
        cfg = self._cfg.config
        for row in fut.rows:
            delta = (row.entry_utc - now).total_seconds()
            if delta < -cfg.max_signal_age_seconds:
                continue
            kind = "live" if delta < cfg.immediate_threshold_seconds else "future"
            sig = Signal(
                kind=kind,
                asset_symbol=row.asset_symbol,
                asset_display=row.asset_display,
                direction=row.direction,
                expiry_seconds=cfg.default_expiry_seconds,
                entry_utc=row.entry_utc if kind == "future" else now,
                received_at=now,
                source_chat_id=chat_id,
                source_chat_title=chat_title,
                source_msg_id=int(getattr(msg, "id", 0)),
                raw_text=f"FUTURE {row.asset_display} {row.direction.upper()} @ {row.entry_utc.isoformat()}",
            )
            if kind == "future":
                self._history.add_signal(sig)
                self._scheduler.arm(sig)
            else:
                self._dispatcher.submit(sig)

    async def _maybe_confirm_prep(
        self,
        event: Any,
        msg: Any,
        chat_id: int,
        chat_title: str | None,
        now: datetime,
    ) -> None:
        prep = self._pending_prep.get(chat_id)
        if prep is None:
            return
        if (time.time() - prep["ts"]) > PREP_TTL_SECONDS:
            self._pending_prep.pop(chat_id, None)
            return

        # Direction can come from a sticker alt-emoji, or an emoji-only
        # text message. We try both.
        direction = None
        sticker_alt = _sticker_alt(msg)
        if sticker_alt:
            direction = emoji_to_direction(sticker_alt)
        if direction is None:
            text = (msg.message or "").strip()
            direction = emoji_to_direction(text)
        if direction is None:
            return

        self._pending_prep.pop(chat_id, None)

        signal = Signal(
            kind="live",
            asset_symbol=prep["asset_symbol"],
            asset_display=prep["asset_display"],
            direction=direction,
            expiry_seconds=int(prep["expiry_seconds"]),
            entry_utc=now,
            received_at=now,
            source_chat_id=chat_id,
            source_chat_title=chat_title,
            source_msg_id=int(getattr(msg, "id", 0)),
            raw_text=f"LIVE {prep['asset_display']} {direction.upper()}",
        )
        self._dispatcher.submit(signal)

    async def _chat_title(self, event: Any) -> str | None:
        try:
            chat = await event.get_chat()
            return getattr(chat, "title", None) or getattr(chat, "first_name", None)
        except Exception:
            return None


def _sticker_alt(msg: Any) -> str | None:
    """Best-effort extract of the sticker's alt emoji."""
    try:
        from telethon.tl.types import DocumentAttributeSticker
        for attr in msg.media.document.attributes:
            if isinstance(attr, DocumentAttributeSticker):
                return getattr(attr, "alt", None)
    except (AttributeError, ImportError):
        return None
    return None

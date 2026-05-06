"""Thin wrapper around Telethon for the web API.

* ``TelegramSession`` — owns the underlying ``TelegramClient`` and
  drives the multi-step login (send code → submit code → optional
  2FA password).
* ``TelegramService`` — process-singleton facade the FastAPI routes
  call into; lazy-creates the session and holds the new-message
  handler.

The Telethon session file lives at ``settings/telegram.session`` so
re-logging-in across container restarts is just a matter of mounting
the volume.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


SESSION_PATH = Path(
    os.environ.get(
        "PYQUOTEX_TELEGRAM_SESSION", "settings/telegram.session"
    )
).resolve()


@dataclass
class LoginState:
    """Tracks where we are in the login flow."""

    phone: str | None = None
    phone_code_hash: str | None = None
    awaiting_code: bool = False
    awaiting_password: bool = False  # 2FA
    error: str | None = None


class TelegramService:
    """Process-singleton handle on a Telethon ``TelegramClient``.

    Telethon needs an event loop to live on, so we keep the client in
    the same loop as the FastAPI app. The class is async-safe (uses
    an asyncio lock) and import-lazy: importing this module doesn't
    require ``telethon`` until the user actually clicks "connect".
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # Reentrancy from within the same task is fine, but cross-task
        # creation needs the lock above.
        self._sync_lock = threading.Lock()
        self._client: Any = None    # telethon.TelegramClient
        self._login = LoginState()
        self._handler: Callable[[Any], Awaitable[None]] | None = None
        self._handler_unsub: Callable[[], None] | None = None
        # API credentials — supplied at connect-time by the UI or env.
        self._api_id: int | None = None
        self._api_hash: str | None = None

    # --- credentials ----------------------------------------------

    def set_credentials(self, api_id: int, api_hash: str) -> None:
        """Capture the user's ``my.telegram.org`` API credentials.

        Required before ``connect()``. Calling again with different
        values invalidates any cached client (you'd typically only do
        that to fix a typo).
        """
        with self._sync_lock:
            if self._api_id == api_id and self._api_hash == api_hash:
                return
            # New credentials → drop existing client (caller should
            # await ``disconnect`` first if a session was running).
            self._api_id = int(api_id)
            self._api_hash = str(api_hash).strip()
            self._client = None

    def has_credentials(self) -> bool:
        return self._api_id is not None and bool(self._api_hash)

    # --- lifecycle ------------------------------------------------

    async def _ensure_client(self) -> Any:
        if not self.has_credentials():
            raise RuntimeError(
                "Telegram API credentials missing — POST /telegram/credentials first."
            )
        if self._client is not None:
            return self._client
        # Lazy import keeps telethon optional until first use.
        from telethon import TelegramClient

        SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(
            str(SESSION_PATH.with_suffix("")),  # telethon adds .session
            self._api_id,
            self._api_hash,
        )
        self._client = client
        await client.connect()
        return client

    async def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "credentials_set": self.has_credentials(),
            "connected": False,
            "authorised": False,
            "awaiting_code": self._login.awaiting_code,
            "awaiting_password": self._login.awaiting_password,
            "phone": self._login.phone,
            "error": self._login.error,
            "me": None,
        }
        if not self.has_credentials():
            return out
        try:
            client = await self._ensure_client()
            out["connected"] = client.is_connected()
            out["authorised"] = await client.is_user_authorized()
            if out["authorised"]:
                me = await client.get_me()
                out["me"] = {
                    "id": getattr(me, "id", None),
                    "username": getattr(me, "username", None),
                    "first_name": getattr(me, "first_name", None),
                    "phone": getattr(me, "phone", None),
                }
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
        return out

    # --- login flow -----------------------------------------------

    async def send_code(self, phone: str) -> None:
        """Step 1: send a login code to ``phone``."""
        async with self._lock:
            client = await self._ensure_client()
            self._login = LoginState(phone=phone)
            try:
                sent = await client.send_code_request(phone)
            except Exception as e:
                self._login.error = f"{type(e).__name__}: {e}"
                raise
            self._login.phone_code_hash = sent.phone_code_hash
            self._login.awaiting_code = True

    async def submit_code(self, code: str) -> dict[str, Any]:
        """Step 2: submit the code SMS/Telegram sent. May resolve to
        ``awaiting_password`` if the account has 2FA."""
        # Lazy import keeps telethon optional.
        from telethon.errors import SessionPasswordNeededError

        async with self._lock:
            client = await self._ensure_client()
            if not self._login.awaiting_code or not self._login.phone:
                raise RuntimeError("No login in progress; POST /telegram/login first.")
            try:
                await client.sign_in(
                    phone=self._login.phone,
                    code=code,
                    phone_code_hash=self._login.phone_code_hash,
                )
            except SessionPasswordNeededError:
                self._login.awaiting_code = False
                self._login.awaiting_password = True
                return await self.status()
            except Exception as e:
                self._login.error = f"{type(e).__name__}: {e}"
                raise
            self._login.awaiting_code = False
            return await self.status()

    async def submit_password(self, password: str) -> dict[str, Any]:
        """Step 3 (only if 2FA is on): submit the cloud password."""
        async with self._lock:
            client = await self._ensure_client()
            if not self._login.awaiting_password:
                raise RuntimeError("No 2FA prompt is active.")
            try:
                await client.sign_in(password=password)
            except Exception as e:
                self._login.error = f"{type(e).__name__}: {e}"
                raise
            self._login.awaiting_password = False
            return await self.status()

    async def logout(self) -> None:
        """Sign out, drop the local session file, and stop any
        running message handler."""
        async with self._lock:
            self._unregister_handler()
            client = self._client
            if client is not None:
                try:
                    if client.is_connected() and await client.is_user_authorized():
                        await client.log_out()
                except Exception as e:
                    logger.warning("telegram log_out failed: %s", e)
                try:
                    await client.disconnect()
                except Exception:
                    pass
            # Best-effort delete of the session file so a fresh login
            # starts clean.
            try:
                f = SESSION_PATH if SESSION_PATH.exists() else SESSION_PATH.with_suffix(".session")
                if f.exists():
                    f.unlink()
            except OSError:
                pass
            self._client = None
            self._login = LoginState()

    async def disconnect(self) -> None:
        """Stop the WS but keep the session file (for shutdown)."""
        async with self._lock:
            self._unregister_handler()
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass

    # --- dialogs (channels/groups/users) --------------------------

    async def list_dialogs(self, limit: int = 200) -> list[dict[str, Any]]:
        client = await self._ensure_client()
        if not await client.is_user_authorized():
            raise RuntimeError("Not authorised — complete login first.")
        out: list[dict[str, Any]] = []
        async for dialog in client.iter_dialogs(limit=limit):
            entity = dialog.entity
            out.append({
                "id": int(dialog.id),
                "title": getattr(entity, "title", None) or
                         getattr(entity, "first_name", None) or
                         f"chat#{dialog.id}",
                "is_channel": bool(getattr(dialog, "is_channel", False)),
                "is_group": bool(getattr(dialog, "is_group", False)),
                "is_user": bool(getattr(dialog, "is_user", False)),
                "username": getattr(entity, "username", None),
                "unread_count": int(getattr(dialog, "unread_count", 0) or 0),
            })
        return out

    # --- handler registration -------------------------------------

    async def register_handler(
        self,
        handler: Callable[[Any], Awaitable[None]],
        chat_ids: list[int],
    ) -> None:
        """Replace the new-message handler. Idempotent — calling with
        a fresh ``chat_ids`` list rebinds atomically."""
        from telethon import events

        async with self._lock:
            client = await self._ensure_client()
            if not await client.is_user_authorized():
                raise RuntimeError("Not authorised; complete login first.")
            self._unregister_handler()
            if not chat_ids:
                # Nothing to listen on; record the empty handler set.
                self._handler = handler
                return
            # Telethon's ``chats=`` accepts ints or list[int]
            wrapper = handler

            @client.on(events.NewMessage(chats=chat_ids))
            async def _bound(event):
                try:
                    await wrapper(event)
                except Exception:
                    logger.exception("telegram message handler crashed")

            self._handler = wrapper

            def _unsub() -> None:
                try:
                    client.remove_event_handler(_bound)
                except Exception:
                    pass

            self._handler_unsub = _unsub

    def _unregister_handler(self) -> None:
        if self._handler_unsub is not None:
            try:
                self._handler_unsub()
            except Exception:
                pass
        self._handler = None
        self._handler_unsub = None

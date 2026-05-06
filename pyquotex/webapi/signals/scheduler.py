"""Scheduler for FUTURE-type signals.

For each row inside a parsed ``FUTURE SIGNALS`` message we ``arm`` a
task that sleeps until the row's UTC entry time minus a small guard
window, then hands the row off to the live dispatcher. The guard
gives the broker WS just enough head-start to not miss the candle.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .models import Signal

if TYPE_CHECKING:
    from .dispatcher import Dispatcher

logger = logging.getLogger(__name__)

ENTRY_GUARD_SECONDS = 0.5


class FutureScheduler:
    def __init__(self, dispatcher: "Dispatcher") -> None:
        self._dispatcher = dispatcher
        self._tasks: set[asyncio.Task] = set()

    def arm(self, signal: Signal) -> asyncio.Task:
        """Schedule a future trade. Returns the task."""
        task = asyncio.create_task(self._run(signal))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _run(self, signal: Signal) -> None:
        now = datetime.now(tz=timezone.utc)
        delay = (signal.entry_utc - now).total_seconds() - ENTRY_GUARD_SECONDS
        if delay > 0:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
        # Re-stamp the live entry to "now" so the dispatcher records the
        # actual fire moment rather than the original schedule.
        live = Signal(
            kind="live",
            asset_symbol=signal.asset_symbol,
            asset_display=signal.asset_display,
            direction=signal.direction,
            expiry_seconds=signal.expiry_seconds,
            entry_utc=datetime.now(tz=timezone.utc),
            received_at=signal.received_at,
            source_chat_id=signal.source_chat_id,
            source_chat_title=signal.source_chat_title,
            source_msg_id=signal.source_msg_id,
            raw_text=signal.raw_text,
            id=signal.id,
        )
        try:
            await self._dispatcher._run(live)  # noqa: SLF001 — internal coupling is fine
        except Exception:
            logger.exception("scheduler: dispatch raised")

    def cancel_all(self) -> None:
        for t in list(self._tasks):
            if not t.done():
                t.cancel()
        self._tasks.clear()

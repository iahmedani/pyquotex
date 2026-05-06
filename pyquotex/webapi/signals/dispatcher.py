"""Trade execution + Martingale recovery.

The dispatcher owns the broker side of the pipeline. It receives a
:class:`Signal`, places the trade via the shared ``Quotex`` client,
waits for the result, and — on a loss — fires up to ``mtg_levels``
follow-up trades in the same direction.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from .config import ConfigStore
from .models import Signal, TradeRecord
from .store import History

if TYPE_CHECKING:
    from pyquotex.stable_api import Quotex

logger = logging.getLogger(__name__)


class Dispatcher:
    def __init__(
        self,
        client: "Quotex",
        config: ConfigStore,
        history: History,
    ) -> None:
        self._client = client
        self._config = config
        self._history = history
        # Bounded concurrency so a burst of signals can't flood the broker.
        self._sem = asyncio.Semaphore(4)

    def submit(self, signal: Signal) -> asyncio.Task:
        """Fire-and-forget entry point for live signals. Returns the
        task so the scheduler can join on it if it wants result-aware
        chaining."""
        return asyncio.create_task(self._run(signal))

    async def _run(self, signal: Signal) -> None:
        cfg = self._config.config
        if cfg.paused:
            self._history.add_signal(signal)
            self._history.add_trade(TradeRecord(
                id=uuid.uuid4().hex[:12],
                signal_id=signal.id,
                asset_symbol=signal.asset_symbol,
                asset_display=signal.asset_display,
                direction=signal.direction,
                amount=cfg.amount,
                expiry_seconds=signal.expiry_seconds,
                mtg_step=0,
                state="skipped",
                error="dispatcher paused",
            ))
            return

        self._history.add_signal(signal)

        async with self._sem:
            await self._place_with_mtg(signal, cfg.amount, cfg.mtg_levels, cfg.mtg_multiplier)

    async def _place_with_mtg(
        self,
        signal: Signal,
        base_amount: float,
        mtg_levels: int,
        mtg_multiplier: float,
    ) -> None:
        """Place the entry trade and, on loss, up to ``mtg_levels``
        follow-ups in the same direction."""
        amount = base_amount
        for step in range(mtg_levels + 1):
            trade = TradeRecord(
                id=uuid.uuid4().hex[:12],
                signal_id=signal.id,
                asset_symbol=signal.asset_symbol,
                asset_display=signal.asset_display,
                direction=signal.direction,
                amount=round(amount, 2),
                expiry_seconds=signal.expiry_seconds,
                mtg_step=step,
                state="pending",
            )
            self._history.add_trade(trade)
            outcome = await self._buy_and_wait(trade, signal)
            if outcome == "win":
                return
            if outcome != "loss":
                # error / timeout / unknown — don't escalate Martingale.
                return
            amount = amount * mtg_multiplier

    async def _buy_and_wait(
        self, trade: TradeRecord, signal: Signal
    ) -> str:
        """Place a single buy and wait for its outcome. Returns one
        of ``"win"|"loss"|"error"|"timeout"``. Side-effect: updates
        the trade record in :class:`History`."""
        try:
            ok, info = await self._client.buy(
                amount=trade.amount,
                asset=trade.asset_symbol,
                direction=trade.direction,
                duration=trade.expiry_seconds,
                time_mode="TIME",
                confirm_timeout=10.0,
            )
        except Exception as e:
            logger.exception("dispatcher: buy() raised")
            self._history.update_trade(trade.id, state="error", error=str(e))
            return "error"

        if not ok:
            detail = info if isinstance(info, str) else str(info)
            self._history.update_trade(
                trade.id, state="error", error=f"buy rejected: {detail}",
            )
            return "error"

        order_id = info.get("id") if isinstance(info, dict) else None
        if not order_id:
            self._history.update_trade(
                trade.id, state="error", error="no order id in buy response",
            )
            return "error"

        self._history.update_trade(
            trade.id, state="placed", order_id=str(order_id),
        )

        try:
            result, profit = await self._client.wait_for_order_close(
                order_id,
                duration=trade.expiry_seconds,
                # Generous timeout: a 5-minute trade should resolve a
                # few seconds after expiry; cap at expiry + 60s.
                timeout=trade.expiry_seconds + 60,
            )
        except Exception as e:
            logger.exception("dispatcher: wait_for_order_close raised")
            self._history.update_trade(
                trade.id, state="error", error=f"wait failed: {e}",
            )
            return "error"

        import time as _t
        settled = int(_t.time() * 1000)
        if result == "win":
            self._history.update_trade(
                trade.id, state="win", profit=float(profit),
                settled_at_ms=settled,
            )
            return "win"
        if result == "loss":
            self._history.update_trade(
                trade.id, state="loss", profit=float(profit),
                settled_at_ms=settled,
            )
            return "loss"
        self._history.update_trade(
            trade.id, state="error",
            error=f"unknown outcome: {result}", settled_at_ms=settled,
        )
        return "error"

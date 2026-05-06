"""In-memory ring buffers for received signals + executed trades.

The UI polls ``GET /signals/history`` to render a feed; persisting to
disk would tie us to a DB and we don't need ten-year retention for an
ops view. Buffers are bounded so a chatty channel can't blow memory.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict
from typing import Any

from .models import Signal, TradeRecord

_DEFAULT_CAP = 500


class History:
    def __init__(self, cap: int = _DEFAULT_CAP) -> None:
        self._signals: deque[Signal] = deque(maxlen=cap)
        self._trades: dict[str, TradeRecord] = {}
        self._trade_order: deque[str] = deque(maxlen=cap)
        self._lock = threading.Lock()

    # --- mutators -------------------------------------------------

    def add_signal(self, sig: Signal) -> None:
        with self._lock:
            self._signals.append(sig)

    def add_trade(self, trade: TradeRecord) -> None:
        with self._lock:
            if trade.id not in self._trades:
                self._trade_order.append(trade.id)
            self._trades[trade.id] = trade
            # Drop any record evicted from the deque to keep dict bounded.
            seen = set(self._trade_order)
            for stale in [k for k in self._trades if k not in seen]:
                self._trades.pop(stale, None)

    def update_trade(
        self, trade_id: str, **patch: Any
    ) -> TradeRecord | None:
        with self._lock:
            trade = self._trades.get(trade_id)
            if trade is None:
                return None
            for k, v in patch.items():
                if hasattr(trade, k):
                    setattr(trade, k, v)
            return trade

    # --- accessors ------------------------------------------------

    def snapshot(self, limit: int = 100) -> dict[str, list[dict[str, Any]]]:
        """Most-recent-first dump for the UI."""
        with self._lock:
            sigs = [_signal_dict(s) for s in list(self._signals)[-limit:]]
            sigs.reverse()
            trade_ids = list(self._trade_order)[-limit:]
            trade_ids.reverse()
            trades = [
                _trade_dict(self._trades[i])
                for i in trade_ids
                if i in self._trades
            ]
        return {"signals": sigs, "trades": trades}


def _signal_dict(s: Signal) -> dict[str, Any]:
    return {
        "id": s.id,
        "kind": s.kind,
        "asset_symbol": s.asset_symbol,
        "asset_display": s.asset_display,
        "direction": s.direction,
        "expiry_seconds": s.expiry_seconds,
        "entry_utc": s.entry_utc.isoformat(),
        "received_at": s.received_at.isoformat(),
        "source_chat_id": s.source_chat_id,
        "source_chat_title": s.source_chat_title,
        "source_msg_id": s.source_msg_id,
        "raw_text": s.raw_text[:280],   # cap for the UI
    }


def _trade_dict(t: TradeRecord) -> dict[str, Any]:
    return asdict(t)

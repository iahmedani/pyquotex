"""Internal signal + trade dataclasses (separate from Pydantic API
schemas so the parser/dispatcher don't depend on FastAPI)."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Direction = Literal["call", "put"]
SignalKind = Literal["live", "future"]


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Signal:
    """A normalised trading instruction extracted from a Telegram
    message. ``entry_utc`` is when the trade should fire; for live
    signals it's roughly the receive time, for future signals it's a
    schedule point parsed from the message."""

    kind: SignalKind
    asset_symbol: str        # broker-ready (e.g. ``USDINR_otc``)
    asset_display: str       # original text (e.g. ``USD/INR (OTC)``)
    direction: Direction
    expiry_seconds: int
    entry_utc: datetime
    received_at: datetime
    source_chat_id: int | None = None
    source_chat_title: str | None = None
    source_msg_id: int | None = None
    raw_text: str = ""
    id: str = field(default_factory=_new_id)


@dataclass
class TradeRecord:
    """Trade lifecycle record — one per buy attempt (including MTG
    re-entries)."""

    id: str
    signal_id: str
    asset_symbol: str
    asset_display: str
    direction: Direction
    amount: float
    expiry_seconds: int
    mtg_step: int                     # 0 = original entry, 1 = first MTG
    state: str                        # "pending"|"placed"|"win"|"loss"|"error"|"skipped"
    order_id: str | None = None
    profit: float = 0.0
    error: str | None = None
    placed_at_ms: int = field(default_factory=_now_ms)
    settled_at_ms: int | None = None

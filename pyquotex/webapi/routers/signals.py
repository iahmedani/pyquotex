"""Signal config + history routes.

* ``GET /signals/config`` / ``PATCH /signals/config`` — read/write
  the persisted ``SignalConfig`` (trade amount, MTG, channel list,
  parser toggles, etc.).
* ``GET /signals/history`` — recent signals + their executed trades.
* ``POST /signals/test-parse`` — feed a raw message body in to
  verify which parser would catch it (handy for debugging without
  having to wait for a real signal in a channel).
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..auth import verify_api_key
from ..models import (
    HistoryResponse,
    SignalConfigPatch,
    SignalConfigResponse,
    SignalEntry,
    TradeEntry,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/signals",
    tags=["signals"],
    dependencies=[Depends(verify_api_key)],
)


def _bundle(request: Request):
    bundle = getattr(request.app.state, "signal_bundle", None)
    if bundle is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Signal pipeline not initialised. Install 'pyquotex[telegram]'.",
        )
    return bundle


@router.get("/config", response_model=SignalConfigResponse)
async def get_config(request: Request) -> SignalConfigResponse:
    cfg = _bundle(request).config_store.config
    return SignalConfigResponse(**asdict(cfg))


@router.patch("/config", response_model=SignalConfigResponse)
async def patch_config(
    patch: SignalConfigPatch, request: Request,
) -> SignalConfigResponse:
    bundle = _bundle(request)
    payload = patch.model_dump(exclude_unset=True)
    new_cfg = bundle.config_store.update(payload)

    # If the user changed the channel list, re-bind Telethon's handler.
    if "monitored_chat_ids" in payload or "paused" in payload:
        try:
            await bundle.controller.apply_subscriptions()
        except Exception as e:
            logger.warning("apply_subscriptions failed: %s", e)

    return SignalConfigResponse(**asdict(new_cfg))


@router.get("/history", response_model=HistoryResponse)
async def get_history(
    request: Request, limit: int = 100,
) -> HistoryResponse:
    bundle = _bundle(request)
    snap = bundle.history.snapshot(limit=max(1, min(limit, 500)))
    return HistoryResponse(
        signals=[SignalEntry(**s) for s in snap["signals"]],
        trades=[TradeEntry(**t) for t in snap["trades"]],
    )


# --- parser preview ----------------------------------------------

class TestParseRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=8000)
    default_tz_offset: float | None = None
    default_expiry_seconds: int | None = None


class TestParseResponse(BaseModel):
    matched: str | None = None
    detail: dict[str, Any] | None = None


@router.post("/test-parse", response_model=TestParseResponse)
async def test_parse(
    body: TestParseRequest, request: Request,
) -> TestParseResponse:
    """Run all three parsers against ``text`` and return whichever
    matches first. Used by the UI's "test message" button to verify
    a parser catches a sample before going live."""
    from ..signals.parsers import (
        parse_future_bulk,
        parse_live_prep,
        parse_live_single,
    )

    bundle = _bundle(request)
    cfg = bundle.config_store.config
    tz = body.default_tz_offset if body.default_tz_offset is not None else cfg.default_tz_offset
    expiry = body.default_expiry_seconds or cfg.default_expiry_seconds

    fut = parse_future_bulk(body.text, tz)
    if fut is not None:
        return TestParseResponse(
            matched="future_bulk",
            detail={
                "signal_date": fut.signal_date.isoformat(),
                "tz_offset": fut.tz_offset,
                "rows": [
                    {
                        "asset_display": r.asset_display,
                        "asset_symbol": r.asset_symbol,
                        "direction": r.direction,
                        "entry_utc": r.entry_utc.isoformat(),
                    }
                    for r in fut.rows
                ],
            },
        )

    live = parse_live_single(body.text, expiry)
    if live is not None:
        return TestParseResponse(
            matched="live_single",
            detail={
                "asset_display": live.asset_display,
                "asset_symbol": live.asset_symbol,
                "direction": live.direction,
                "expiry_seconds": live.expiry_seconds,
                "received_at": datetime.now(tz=timezone.utc).isoformat(),
            },
        )

    prep = parse_live_prep(body.text, expiry)
    if prep is not None:
        return TestParseResponse(
            matched="live_prep",
            detail={
                "asset_display": prep.asset_display,
                "asset_symbol": prep.asset_symbol,
                "expiry_seconds": prep.expiry_seconds,
                "note": "send a 👍 (call) or 👎 (put) sticker/emoji next to fire.",
            },
        )

    return TestParseResponse(matched=None)

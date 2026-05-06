"""Telegram authentication + dialog routes.

Login flow:

1. ``POST /telegram/credentials`` — supply the ``api_id`` /
   ``api_hash`` from ``my.telegram.org``. Required once per session
   (or persisted between restarts via the on-disk session file).
2. ``POST /telegram/login`` — start sign-in for a phone number; we
   ask Telegram to send a code.
3. ``POST /telegram/code`` — submit the code. If the account has
   2FA, the response will set ``awaiting_password=true`` and the
   caller must follow up with ``POST /telegram/password``.
4. ``GET  /telegram/status`` — check whether we're authorised.
5. ``GET  /telegram/dialogs`` — list channels/groups/chats so the UI
   can let the user pick which to monitor.
6. ``POST /telegram/logout`` — sign out + clear local session.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import verify_api_key
from ..models import (
    TelegramCodeRequest,
    TelegramCredentialsRequest,
    TelegramDialog,
    TelegramDialogsResponse,
    TelegramLoginRequest,
    TelegramPasswordRequest,
    TelegramStatus,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/telegram",
    tags=["telegram"],
    dependencies=[Depends(verify_api_key)],
)


def _service(request: Request):
    svc = getattr(request.app.state, "telegram_service", None)
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram service not initialised. Install the optional "
                   "'telegram' extra: pip install 'pyquotex[telegram]'.",
        )
    return svc


@router.post("/credentials", response_model=TelegramStatus)
async def set_credentials(
    body: TelegramCredentialsRequest, request: Request,
) -> TelegramStatus:
    svc = _service(request)
    svc.set_credentials(body.api_id, body.api_hash)
    return TelegramStatus(**await svc.status())


@router.get("/status", response_model=TelegramStatus)
async def get_status(request: Request) -> TelegramStatus:
    svc = _service(request)
    return TelegramStatus(**await svc.status())


@router.post("/login", response_model=TelegramStatus)
async def start_login(
    body: TelegramLoginRequest, request: Request,
) -> TelegramStatus:
    svc = _service(request)
    try:
        await svc.send_code(body.phone.strip())
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"send_code failed: {e}",
        ) from e
    return TelegramStatus(**await svc.status())


@router.post("/code", response_model=TelegramStatus)
async def submit_code(
    body: TelegramCodeRequest, request: Request,
) -> TelegramStatus:
    svc = _service(request)
    try:
        await svc.submit_code(body.code.strip())
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"sign_in failed: {e}",
        ) from e
    return TelegramStatus(**await svc.status())


@router.post("/password", response_model=TelegramStatus)
async def submit_password(
    body: TelegramPasswordRequest, request: Request,
) -> TelegramStatus:
    svc = _service(request)
    try:
        await svc.submit_password(body.password)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"sign_in (password) failed: {e}",
        ) from e
    return TelegramStatus(**await svc.status())


@router.post("/logout", response_model=TelegramStatus)
async def logout(request: Request) -> TelegramStatus:
    svc = _service(request)
    await svc.logout()
    return TelegramStatus(**await svc.status())


@router.get("/dialogs", response_model=TelegramDialogsResponse)
async def list_dialogs(
    request: Request, limit: int = 200,
) -> TelegramDialogsResponse:
    svc = _service(request)
    try:
        dialogs = await svc.list_dialogs(limit=limit)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"list_dialogs failed: {e}",
        ) from e
    return TelegramDialogsResponse(
        dialogs=[TelegramDialog(**d) for d in dialogs],
    )

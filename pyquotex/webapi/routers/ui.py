"""Static-file UI router.

Mounts ``/ui`` to serve the bundled HTML/CSS/JS dashboard, and
redirects ``/`` to ``/ui/`` for convenience. The UI itself is plain
HTML + vanilla JS — no build step.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

router = APIRouter(tags=["ui"])


@router.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/ui/", status_code=307)


def mount_static(app) -> None:
    """Attach the ``/ui`` static mount to ``app``. Called from the
    app factory after routers are registered."""
    if STATIC_DIR.is_dir():
        app.mount(
            "/ui",
            StaticFiles(directory=str(STATIC_DIR), html=True),
            name="ui",
        )

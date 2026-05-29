from __future__ import annotations

import os
from urllib.parse import quote, urlparse

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()


def _is_safe_callback(cb: str) -> bool:
    """Only allow callbacks to localhost so this can't be an open redirector."""
    try:
        p = urlparse(cb)
        return p.scheme == "http" and p.hostname == "127.0.0.1"
    except Exception:
        return False


@router.get("/auth/portal")
async def portal_page(cb: str = "") -> RedirectResponse:
    """Redirect the browser to the React login page, forwarding the CLI callback URL."""
    if cb and not _is_safe_callback(cb):
        # Reject bad callback — redirect without cb so the user can still sign in
        cb = ""

    frontend_url = os.getenv("FRONTEND_URL", "https://nanoforgeflow.com")
    dest = f"{frontend_url}/login"
    if cb:
        dest += f"?cb={quote(cb, safe='')}"
    return RedirectResponse(url=dest, status_code=302)

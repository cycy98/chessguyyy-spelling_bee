"""Spelling Bee — Jinja2 template helpers shared across route modules."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi.templating import Jinja2Templates

from backend.auth import get_current_user

if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.responses import HTMLResponse

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.autoescape = True  # type: ignore[assignment]


def _name_color(name: str) -> str:
    h = int(hashlib.md5(name.encode()).hexdigest()[:6], 16)
    return f"hsl({h % 360}, 65%, 55%)"


templates.env.filters["name_color"] = _name_color


def _relative_time(ts: float) -> str:
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


templates.env.filters["relative_time"] = _relative_time


def client_ip(request: Request) -> str:
    # We always run behind nginx
    forwarded = request.headers.get("x-real-ip")
    if forwarded:
        return forwarded
    if request.client:
        return request.client.host
    return "unknown"


async def tpl(request: Request, name: str, ctx: dict[str, Any] | None = None) -> HTMLResponse:
    user = get_current_user(request)
    c: dict[str, Any] = {"request": request, "user": user}
    if ctx:
        c.update(ctx)
    return templates.TemplateResponse(request, name, c)

"""Spelling Bee — Jinja2 template helpers shared across route modules."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backend.auth import get_current_user
from backend.game import DIFFICULTIES, ROOT, TIER_COLORS, TOTAL_WORDS, WORDS

templates = Jinja2Templates(directory=str(ROOT / "templates"))
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

templates.env.globals["tier_colors"] = TIER_COLORS


def _catalog_ctx() -> dict[str, Any]:
    return {
        "difficulties": DIFFICULTIES,
        "words": WORDS,
        "total_words": TOTAL_WORDS,
    }


def client_ip(request: Request) -> str:
    if request.client:
        return request.client.host
    return "unknown"


async def tpl(request: Request, name: str, ctx: dict[str, Any] | None = None) -> HTMLResponse:
    user = get_current_user(request)
    c: dict[str, Any] = {"request": request, "user": user}
    if ctx:
        c.update(ctx)
    return templates.TemplateResponse(request, name, c)

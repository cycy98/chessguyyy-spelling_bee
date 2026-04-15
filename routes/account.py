"""Spelling Bee — account & leaderboard routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from backend import db
from backend.auth import get_current_user
from backend.game import ALL_WORDS
from templating import tpl

router = APIRouter()


@router.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard(request: Request) -> HTMLResponse:
    rows = await db.fetchall(
        "SELECT username, elo, games, wins, words, correct, best_wpm, best_word, best_streak, tiers_cleared"
        " FROM users ORDER BY elo DESC LIMIT 100",
    )
    return await tpl(request, "fragments/leaderboard.html", {"players": rows})


async def _account_ctx(row: db.Row) -> dict[str, Any]:
    username = row["username"]
    recent = await db.fetchall(
        "SELECT word, correct, wpm, tier, ts FROM guess_log "
        "WHERE username=? ORDER BY ts DESC LIMIT 20",
        (username,),
    )
    practice = await db.fetchall(
        "SELECT word, COUNT(*) as n FROM guess_log "
        "WHERE username=? AND correct=0 GROUP BY word ORDER BY n DESC LIMIT 10",
        (username,),
    )
    practice_enriched = [
        {
            "word": r["word"],
            "n": r["n"],
            "definition": ALL_WORDS.get(r["word"], {}).get("definition", ""),
        }
        for r in practice
    ]
    avg_row = await db.fetchone(
        "SELECT AVG(wpm) as avg_wpm FROM (SELECT wpm FROM guess_log "
        "WHERE username=? AND correct=1 ORDER BY ts DESC LIMIT 50)",
        (username,),
    )
    avg_wpm = round(avg_row["avg_wpm"], 1) if avg_row and avg_row["avg_wpm"] else 0
    return {
        "recent": recent,
        "practice": practice_enriched,
        "avg_wpm": avg_wpm,
    }


@router.get("/account/{username}", response_class=HTMLResponse)
async def account_view(request: Request, username: str) -> HTMLResponse:
    row = await db.fetchone("SELECT * FROM users WHERE username = ?", (username,))
    if not row:
        return HTMLResponse("<p class='feedback error'>Player not found.</p>", status_code=404)
    return await tpl(
        request,
        "fragments/account.html",
        {"player": row, **(await _account_ctx(row))},
    )


@router.get("/account", response_class=HTMLResponse)
async def own_account(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if not user:
        return HTMLResponse("<p class='feedback error'>Not logged in.</p>", status_code=403)
    row = await db.fetchone("SELECT * FROM users WHERE username = ?", (user,))
    if not row:
        return HTMLResponse("<p class='feedback error'>Player not found.</p>", status_code=404)
    return await tpl(
        request,
        "fragments/account.html",
        {"player": row, **(await _account_ctx(row))},
    )

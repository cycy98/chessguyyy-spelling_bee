"""Spelling Bee — account & leaderboard routes."""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from backend import db
from backend.auth import ADMIN_USERS, get_current_user
from backend.errors import HtmxError
from templating import PICO_THEMES, tpl

router = APIRouter()

_SORT_COLS = {"elo", "games", "wins", "correct", "best_wpm", "best_streak"}

_BADGES: list[tuple[str, str, str, float | int]] = [
    ("Century Club", "🏆", "wins", 100),
    ("Word Master", "📖", "correct", 1000),
    ("Veteran", "⭐", "games", 500),
    ("Speed Demon", "⚡", "best_wpm", 60),
]


def _player_badges(player: db.Row) -> list[tuple[str, str]]:
    earned = [(name, icon) for name, icon, col, threshold in _BADGES if player[col] >= threshold]
    if player["words"] >= 50 and player["correct"] / player["words"] >= 0.9:
        earned.append(("Sharpshooter", "🎯"))
    return earned


@router.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard(request: Request, sort: str = "elo") -> HTMLResponse:
    if sort not in _SORT_COLS:
        sort = "elo"
    tiebreak = "" if sort == "elo" else ", elo DESC"
    rows = await db.fetchall(
        f"SELECT username, elo, games, wins, words, correct, best_wpm, best_word, best_streak"  # noqa: S608
        f" FROM users ORDER BY {sort} DESC{tiebreak} LIMIT 100",
    )
    return await tpl(request, "fragments/leaderboard.html", {"players": rows, "sort": sort})


async def _account_ctx(row: db.Row, all_words: dict[str, Any]) -> dict[str, Any]:
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
            "definition": all_words.get(r["word"], {}).get("definition", ""),
        }
        for r in practice
    ]
    avg_row = await db.fetchone(
        "SELECT AVG(wpm) as avg_wpm FROM (SELECT wpm FROM guess_log "
        "WHERE username=? AND correct=1 ORDER BY ts DESC LIMIT 50)",
        (username,),
    )
    avg_wpm = round(avg_row["avg_wpm"], 1) if avg_row and avg_row["avg_wpm"] else 0
    tier_rows = await db.fetchall("SELECT tier FROM user_tiers WHERE username=?", (username,))
    tiers = [r["tier"] for r in tier_rows]
    discord_id = os.environ.get("DISCORD_ID")
    return {
        "recent": recent,
        "practice": practice_enriched,
        "avg_wpm": avg_wpm,
        "tiers": tiers,
        "badges": _player_badges(row),
        "discord_id": discord_id,
    }


@router.get("/account/{username}", response_class=HTMLResponse)
async def account_view(request: Request, username: str) -> HTMLResponse:
    row = await db.fetchone("SELECT * FROM users WHERE username = ?", (username,))
    if not row:
        msg = "Player not found."
        raise HtmxError(msg, 404)
    all_words = request.app.state.srv.catalog.all_words
    owner_theme = row["theme"] if row["theme"] in PICO_THEMES else "amber"
    return await tpl(
        request,
        "fragments/account.html",
        {"player": row, "pico_theme": owner_theme, **(await _account_ctx(row, all_words))},
    )


@router.post("/account/settings", response_class=HTMLResponse)
async def update_settings(request: Request, theme: Annotated[str, Form()]) -> HTMLResponse:
    user = get_current_user(request)
    if not user:
        msg = "Not logged in."
        raise HtmxError(msg, 403)
    if theme not in PICO_THEMES:
        msg = "Invalid theme."
        raise HtmxError(msg, 400)
    async with db.transaction() as conn:
        await conn.execute("UPDATE users SET theme=? WHERE username=?", (theme, user))
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/admin/user/{username}/edit", response_class=HTMLResponse)
async def admin_edit_user(
    request: Request,
    username: str,
    elo: Annotated[float, Form()],
    games: Annotated[int, Form()],
    wins: Annotated[int, Form()],
    words: Annotated[int, Form()],
    correct: Annotated[int, Form()],
    best_wpm: Annotated[int, Form()],
    best_streak: Annotated[int, Form()],
    best_word: Annotated[str, Form()],
) -> HTMLResponse:
    caller = get_current_user(request)
    if caller not in ADMIN_USERS:
        msg = "Forbidden."
        raise HtmxError(msg, 403)
    if caller == username:
        msg = "Cannot edit your own account via admin."
        raise HtmxError(msg, 400)
    if elo < 0 or any(v < 0 for v in (games, wins, words, correct, best_wpm, best_streak)):
        msg = "Values must be non-negative."
        raise HtmxError(msg, 400)
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE users SET elo=?, games=?, wins=?, words=?,"
            " correct=?, best_wpm=?, best_streak=?, best_word=? WHERE username=?",
            (elo, games, wins, words, correct, best_wpm, best_streak, best_word.strip(), username),
        )
    row = await db.fetchone("SELECT * FROM users WHERE username=?", (username,))
    if not row:
        msg = "Player not found."
        raise HtmxError(msg, 404)
    all_words = request.app.state.srv.catalog.all_words
    ctx = {"player": row, **(await _account_ctx(row, all_words))}
    return await tpl(request, "fragments/account.html", ctx)


@router.post("/admin/user/{username}/delete")
async def admin_delete_user(request: Request, username: str) -> Response:
    caller = get_current_user(request)
    if caller not in ADMIN_USERS:
        msg = "Forbidden."
        raise HtmxError(msg, 403)
    if caller == username:
        msg = "Cannot delete your own account."
        raise HtmxError(msg, 400)
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM users WHERE username=?", (username,))
    root = request.scope.get("root_path", "")
    return Response(status_code=200, headers={"HX-Redirect": f"{root}/leaderboard"})


@router.get("/account", response_class=HTMLResponse)
async def own_account(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if not user:
        msg = "Not logged in."
        raise HtmxError(msg, 403)
    row = await db.fetchone("SELECT * FROM users WHERE username = ?", (user,))
    if not row:
        msg = "Player not found."
        raise HtmxError(msg, 404)
    all_words = request.app.state.srv.catalog.all_words
    return await tpl(
        request,
        "fragments/account.html",
        {"player": row, **(await _account_ctx(row, all_words))},
    )

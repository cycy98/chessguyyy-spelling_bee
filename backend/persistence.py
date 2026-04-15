"""Spelling Bee — async DB persistence helpers."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from backend import db
from backend.game import DIFFICULTIES, Ranking, Room, update_elo

if TYPE_CHECKING:
    from collections.abc import Callable


async def is_name_reserved(player_name: str, account_username: str | None) -> bool:
    """Return True if player_name belongs to a registered account that isn't the current user."""
    if not player_name or player_name == account_username:
        return False
    return await db.fetchone("SELECT 1 FROM users WHERE username = ?", (player_name,)) is not None


async def load_highest_tier(username: str) -> str:
    row = await db.fetchone("SELECT tiers_cleared FROM users WHERE username=?", (username,))
    if not row or not row["tiers_cleared"]:
        return ""
    tiers = [t for t in row["tiers_cleared"].split(",") if t in DIFFICULTIES]
    return max(tiers, key=DIFFICULTIES.index) if tiers else ""


async def record_guess_stats(
    username: str | None,
    wpm: float,
    word_str: str,
    correct: bool,
    tier: str = "",
    streak: int = 0,
) -> None:
    if not username:
        return
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO guess_log(username, word, correct, wpm, tier, ts) VALUES(?,?,?,?,?,?)",
            (username, word_str, int(correct), wpm, tier, time.time()),
        )
        if correct and streak > 0:
            await conn.execute(
                "UPDATE users SET best_streak = MAX(best_streak, ?) WHERE username = ?",
                (streak, username),
            )


async def persist_match_elo(
    room: Room,
    rankings: list[Ranking],
    notify: Callable[[str], None] | None = None,
) -> None:
    """Run ELO update, write match_results rows, and enrich room scoreboard with ELO deltas."""
    if room.visibility == "local":
        return
    tracked: list[dict[str, Any]] = []
    async with db.transaction() as conn:
        for r in rankings:
            if r["account"]:
                cursor = await conn.execute(
                    "SELECT * FROM users WHERE username = ?",
                    (r["account"],),
                )
                row = await cursor.fetchone()
                if row:
                    tracked.append(
                        {**r, "elo": float(row["elo"]), "old_elo": float(row["elo"])},
                    )
        if len(tracked) >= 2:
            update_elo(tracked)
            n_players = len(tracked)
            now = time.time()
            for t in tracked:
                await conn.execute(
                    "UPDATE users SET elo = ? WHERE username = ?",
                    (t["elo"], t["account"]),
                )
                await conn.execute(
                    "INSERT INTO match_results(username, rank, players, ts) VALUES(?,?,?,?)",
                    (t["account"], t["rank"], n_players, now),
                )

    # Enrich the already-set scoreboard entries with ELO data
    for entry in room.last_match_results:
        for t in tracked:
            if t["sid"] == entry["sid"]:
                entry["elo"] = round(t["elo"], 1)
                entry["elo_delta"] = round(t["elo"] - t["old_elo"], 1)
    if notify:
        notify(room.code)

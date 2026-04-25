"""Database singleton — aiosqlite connection with WAL mode."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

Row = sqlite3.Row

_state: dict[str, aiosqlite.Connection] = {}
_lock = asyncio.Lock()

# Trigger bodies are the single source of truth — used in both _DDL (IF NOT EXISTS for fresh
# installs) and the unconditional post-migration reconciliation in init().
_TRIGGERS: tuple[tuple[str, str], ...] = (
    (
        "trg_guess_stats",
        """AFTER INSERT ON guess_log
BEGIN
    UPDATE users SET
        words    = words + 1,
        correct  = correct + NEW.correct,
        best_wpm = MAX(best_wpm, CASE WHEN NEW.correct
                       THEN CAST(NEW.wpm AS INTEGER) ELSE 0 END),
        best_word = CASE
            WHEN NEW.correct AND CAST(NEW.wpm AS INTEGER) > best_wpm
            THEN NEW.word ELSE best_word END
    WHERE username = NEW.username;
END""",
    ),
    (
        "trg_tier_cleared",
        """AFTER INSERT ON guess_log
WHEN NEW.correct = 1 AND NEW.tier != ''
BEGIN
    INSERT OR IGNORE INTO user_tiers(username, tier) VALUES (NEW.username, NEW.tier);
END""",
    ),
    (
        "trg_match_stats",
        """AFTER INSERT ON match_results
BEGIN
    UPDATE users SET
        games = games + 1,
        wins  = wins + (NEW.rank = 1)
    WHERE username = NEW.username;
END""",
    ),
)

_DDL = (
    """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS users (
    username      TEXT    PRIMARY KEY,
    pw_hash       TEXT    NOT NULL,
    elo           REAL    NOT NULL DEFAULT 1000.0 CHECK(elo >= 0),
    games         INTEGER NOT NULL DEFAULT 0      CHECK(games >= 0),
    wins          INTEGER NOT NULL DEFAULT 0      CHECK(wins >= 0),
    words         INTEGER NOT NULL DEFAULT 0      CHECK(words >= 0),
    correct       INTEGER NOT NULL DEFAULT 0      CHECK(correct >= 0),
    best_wpm      INTEGER NOT NULL DEFAULT 0      CHECK(best_wpm >= 0),
    best_word     TEXT    NOT NULL DEFAULT '',
    best_streak   INTEGER NOT NULL DEFAULT 0      CHECK(best_streak >= 0),
    theme         TEXT    NOT NULL DEFAULT 'amber'
) STRICT;

CREATE TABLE IF NOT EXISTS user_tiers (
    username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    tier     TEXT NOT NULL,
    PRIMARY KEY (username, tier)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS guess_log (
    id       INTEGER PRIMARY KEY,
    username TEXT    NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    word     TEXT    NOT NULL,
    correct  INTEGER NOT NULL CHECK(correct IN (0, 1)),
    wpm      REAL    NOT NULL CHECK(wpm >= 0),
    tier     TEXT    NOT NULL,
    ts       INTEGER NOT NULL DEFAULT (UNIXEPOCH())
) STRICT;

CREATE INDEX IF NOT EXISTS ix_gl_user_ts ON guess_log(username, ts);

CREATE TABLE IF NOT EXISTS match_results (
    id       INTEGER PRIMARY KEY,
    username TEXT    NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    rank     INTEGER NOT NULL CHECK(rank >= 1),
    players  INTEGER NOT NULL CHECK(players >= 2),
    ts       INTEGER NOT NULL DEFAULT (UNIXEPOCH())
) STRICT;
"""
    + "\n".join(
        f"CREATE TRIGGER IF NOT EXISTS {name}\n{body};\n" for name, body in _TRIGGERS
    )
)


async def init(path: Path) -> None:
    conn = await aiosqlite.connect(path)
    conn.row_factory = sqlite3.Row
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA busy_timeout = 5000")
    await conn.executescript(_DDL)
    # Purge rows whose user was deleted without CASCADE (FK was historically not always enforced).
    await conn.execute("DELETE FROM guess_log WHERE username NOT IN (SELECT username FROM users)")
    await conn.execute("DELETE FROM match_results WHERE username NOT IN (SELECT username FROM users)")
    _schema_v = 3
    cur = await conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    ver_row = await cur.fetchone()
    ver = int(ver_row[0]) if ver_row else 0
    if ver < 2:
        # v2: add theme column (suppressed if column already exists on fresh installs)
        with suppress(Exception):
            await conn.execute("ALTER TABLE users ADD COLUMN theme TEXT NOT NULL DEFAULT 'amber'")
    if ver < 3:
        # v3: tiers_cleared → user_tiers junction table; ts REAL → INTEGER DEFAULT (UNIXEPOCH())
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_tiers (
                username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                tier     TEXT NOT NULL,
                PRIMARY KEY (username, tier)
            ) STRICT, WITHOUT ROWID
        """)
        cur2 = await conn.execute(
            "SELECT COUNT(*) FROM pragma_table_info('users') WHERE name='tiers_cleared'"
        )
        if (await cur2.fetchone())[0] > 0:
            await conn.execute("""
                INSERT OR IGNORE INTO user_tiers(username, tier)
                SELECT username, j.value
                FROM users, json_each('["' || replace(tiers_cleared, ',', '","') || '"]') j
                WHERE tiers_cleared != ''
            """)
            # Old trg_tier_cleared references tiers_cleared; must drop it before dropping the column.
            await conn.execute("DROP TRIGGER IF EXISTS trg_tier_cleared")
            await conn.execute("ALTER TABLE users DROP COLUMN tiers_cleared")
        cur3 = await conn.execute("SELECT type FROM pragma_table_info('guess_log') WHERE name='ts'")
        ts_row = await cur3.fetchone()
        if ts_row and ts_row[0].upper() != "INTEGER":
            await conn.execute("""
                CREATE TABLE guess_log_new (
                    id       INTEGER PRIMARY KEY,
                    username TEXT    NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                    word     TEXT    NOT NULL,
                    correct  INTEGER NOT NULL CHECK(correct IN (0, 1)),
                    wpm      REAL    NOT NULL CHECK(wpm >= 0),
                    tier     TEXT    NOT NULL,
                    ts       INTEGER NOT NULL DEFAULT (UNIXEPOCH())
                ) STRICT
            """)
            await conn.execute("""
                INSERT INTO guess_log_new(id, username, word, correct, wpm, tier, ts)
                SELECT id, username, word, correct, wpm, tier, CAST(ts AS INTEGER) FROM guess_log
            """)
            await conn.execute("DROP TABLE guess_log")
            await conn.execute("ALTER TABLE guess_log_new RENAME TO guess_log")
            await conn.execute("CREATE INDEX IF NOT EXISTS ix_gl_user_ts ON guess_log(username, ts)")
            await conn.execute("""
                CREATE TABLE match_results_new (
                    id       INTEGER PRIMARY KEY,
                    username TEXT    NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                    rank     INTEGER NOT NULL CHECK(rank >= 1),
                    players  INTEGER NOT NULL CHECK(players >= 2),
                    ts       INTEGER NOT NULL DEFAULT (UNIXEPOCH())
                ) STRICT
            """)
            await conn.execute("""
                INSERT INTO match_results_new(id, username, rank, players, ts)
                SELECT id, username, rank, players, CAST(ts AS INTEGER) FROM match_results
            """)
            await conn.execute("DROP TABLE match_results")
            await conn.execute("ALTER TABLE match_results_new RENAME TO match_results")
        await conn.execute(
            "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
            (str(_schema_v),),
        )
    # Always bring triggers to canonical form. Table re-creation during migrations silently drops
    # triggers, and trigger bodies may evolve across schema versions. _TRIGGERS is the single source.
    for name, body in _TRIGGERS:
        await conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        await conn.execute(f"CREATE TRIGGER {name}\n{body}")
    await conn.commit()
    _state["conn"] = conn


async def close() -> None:
    conn = _state.pop("conn", None)
    if conn:
        await conn.close()


def get() -> aiosqlite.Connection:
    try:
        return _state["conn"]
    except KeyError:
        msg = "db.init() not called"
        raise RuntimeError(msg) from None


async def fetchone(sql: str, params: tuple = ()) -> Row | None:
    cursor = await get().execute(sql, params)
    return await cursor.fetchone()


async def fetchall(sql: str, params: tuple = ()) -> list[Row]:
    return await get().execute_fetchall(sql, params)


async def execute(sql: str, params: tuple = ()) -> None:
    await get().execute(sql, params)


@asynccontextmanager
async def transaction() -> AsyncIterator[aiosqlite.Connection]:
    """Serialize multi-statement read-modify-write sequences."""
    async with _lock:
        conn = get()
        try:
            yield conn
        except BaseException:
            await conn.rollback()
            raise
        else:
            await conn.commit()

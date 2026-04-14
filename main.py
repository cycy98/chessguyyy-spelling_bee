"""Spelling Bee — single-file FastAPI backend."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import string
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

# ── Config ───

ROOT = Path(__file__).parent
DB_PATH = ROOT / "spellingbee.db"
SECRET = secrets.token_bytes(32)
COOKIE_MAX_AGE = 7 * 24 * 3600
MAX_BODY = 8 * 1024
MAX_CHAT = 80
MAX_CHAT_LEN = 300
MAX_DRAFT_LEN = 200
STALE_MINUTES = 30
MAX_SESSIONS_PER_IP = 20
MAX_PLAYERS = 15
MAX_LOCAL_PLAYERS = 12

# ── Word catalog ──

with Path(ROOT / "wordlist.json").open() as f:
    _raw_catalog: dict[str, Any] = json.load(f)

TIER_COLORS: dict[str, str] = _raw_catalog["info"]["color"]
DIFFICULTIES: list[str] = [k for k in _raw_catalog if k != "info"]
WORDS: dict[str, dict[str, dict[str, Any]]] = {d: _raw_catalog[d] for d in DIFFICULTIES}
TOTAL_WORDS = sum(len(v) for v in WORDS.values())

ALL_WORDS: dict[str, dict[str, Any]] = {
    w: {**wdata, "tier": tier} for tier, wdict in WORDS.items() for w, wdata in wdict.items()
}

_WORD_KEYS: dict[str, list[str]] = {d: list(ws) for d, ws in WORDS.items()}
_ALL_WORD_KEYS: list[str] = list(ALL_WORDS)


def pick_word(difficulty: str) -> dict[str, Any]:
    if difficulty == "randomizer":
        word_str = secrets.choice(_ALL_WORD_KEYS)
        return {"word": word_str, **ALL_WORDS[word_str]}
    keys = _WORD_KEYS.get(difficulty, _WORD_KEYS[DIFFICULTIES[0]])
    pool = WORDS.get(difficulty, WORDS[DIFFICULTIES[0]])
    word_str = secrets.choice(keys)
    return {"word": word_str, **pool[word_str], "tier": difficulty}


def has_audio(word: str) -> bool:
    return (ROOT / "audios" / f"{word.lower()}.mp3").exists()


# ── Database ─

db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode = WAL")
db.execute("PRAGMA foreign_keys = ON")
db.execute("PRAGMA busy_timeout = 5000")
db.executescript("""
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
    tiers_cleared TEXT    NOT NULL DEFAULT ''
) STRICT;

CREATE TABLE IF NOT EXISTS guess_log (
    id       INTEGER PRIMARY KEY,
    username TEXT    NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    word     TEXT    NOT NULL,
    correct  INTEGER NOT NULL CHECK(correct IN (0, 1)),
    wpm      REAL    NOT NULL CHECK(wpm >= 0),
    tier     TEXT    NOT NULL,
    ts       REAL    NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS ix_gl_user_ts ON guess_log(username, ts);

CREATE TABLE IF NOT EXISTS match_results (
    id       INTEGER PRIMARY KEY,
    username TEXT    NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    rank     INTEGER NOT NULL CHECK(rank >= 1),
    players  INTEGER NOT NULL CHECK(players >= 2),
    ts       REAL    NOT NULL
) STRICT;

CREATE TRIGGER IF NOT EXISTS trg_guess_stats
AFTER INSERT ON guess_log
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
END;

CREATE TRIGGER IF NOT EXISTS trg_tier_cleared
AFTER INSERT ON guess_log
WHEN NEW.correct = 1 AND NEW.tier != ''
BEGIN
    UPDATE users SET tiers_cleared = CASE
        WHEN tiers_cleared = '' THEN NEW.tier
        WHEN instr(',' || tiers_cleared || ',', ',' || NEW.tier || ',') > 0
            THEN tiers_cleared
        ELSE tiers_cleared || ',' || NEW.tier
    END
    WHERE username = NEW.username;
END;

CREATE TRIGGER IF NOT EXISTS trg_match_stats
AFTER INSERT ON match_results
BEGIN
    UPDATE users SET
        games = games + 1,
        wins  = wins + (NEW.rank = 1)
    WHERE username = NEW.username;
END;
""")
db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', '1')")
db.commit()
_db_lock = threading.Lock()


# ── Password hashing ─


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ":" + h.hex()


def verify_password(pw: str, stored: str) -> bool:
    salt_hex, hash_hex = stored.split(":")
    salt = bytes.fromhex(salt_hex)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1)
    return hmac.compare_digest(h.hex(), hash_hex)


# ── Auth tokens ───


def make_token(username: str) -> str:
    expiry = str(int(time.time()) + COOKIE_MAX_AGE)
    payload = f"{username}:{expiry}"
    sig = hmac.new(SECRET, payload.encode(), "sha256").hexdigest()
    return f"{payload}:{sig}"


def verify_token(token: str) -> str | None:
    parts = token.rsplit(":", 2)
    if len(parts) != 3:
        return None
    username, expiry_str, sig = parts
    try:
        if int(expiry_str) < time.time():
            return None
    except ValueError:
        return None
    expected = hmac.new(SECRET, f"{username}:{expiry_str}".encode(), "sha256").hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return username


def get_current_user(request: Request) -> str | None:
    token = request.cookies.get("auth")
    if not token:
        return None
    return verify_token(token)


def set_auth_cookie(response: Response, username: str) -> None:
    response.set_cookie(
        "auth",
        make_token(username),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


# ── In-memory state ──


@dataclass
class Session:
    id: str
    player_name: str
    difficulty: str
    word: dict[str, Any] | None = None
    room_code: str | None = None
    account_username: str | None = None
    owner_token: str | None = None
    word_served_at: float = 0.0
    time_limit: float = 0.0
    streak: int = 0
    words_attempted: int = 0
    words_correct: int = 0
    last_feedback: dict[str, str] = field(default_factory=dict)
    board_glow: str | None = None  # "correct" or "incorrect", cleared after first render
    ip: str = ""
    last_activity: float = field(default_factory=time.time)


@dataclass
class Room:
    code: str
    difficulty: str
    visibility: str  # "private" | "public"
    sessions: list[str] = field(default_factory=list)  # session IDs, ordered
    turn_index: int = 0
    eliminated: set[str] = field(default_factory=set)
    winner: str | None = None
    draft_text: str = ""
    chat: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=MAX_CHAT))
    intermission_until: float = 0.0
    turn_deadline: float = 0.0
    turn_time_limit: float = 0.0
    game_number: int = 1
    last_match_results: list[dict[str, Any]] = field(default_factory=list)
    locked: bool = False
    last_activity: float = field(default_factory=time.time)
    current_word: dict[str, Any] | None = None
    word_served_at: float = 0.0


sessions: dict[str, Session] = {}
rooms: dict[str, Room] = {}


def purge_stale() -> None:
    cutoff = time.time() - STALE_MINUTES * 60
    stale_rooms = [c for c, r in rooms.items() if r.last_activity < cutoff]
    for c in stale_rooms:
        for sid in rooms[c].sessions:
            sessions.pop(sid, None)
        del rooms[c]
    stale_sessions = [
        s
        for s, sess in sessions.items()
        if (sess.room_code and sess.room_code not in rooms) or sess.last_activity < cutoff
    ]
    for s in stale_sessions:
        sessions.pop(s, None)
    # Evict empty rate-limit buckets
    stale_ips = [
        ip for ip, actions in rate_buckets.items() if all(not ts for ts in actions.values())
    ]
    for ip in stale_ips:
        del rate_buckets[ip]


def count_sessions_for_ip(ip: str) -> int:
    return sum(1 for s in sessions.values() if s.ip == ip)


def make_session_id() -> str:
    return secrets.token_urlsafe(16)


def make_room_code() -> str:
    chars = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(chars) for _ in range(6))
        if code not in rooms:
            return code


def get_session(request: Request) -> Session | None:
    sid = request.cookies.get("session_id")
    if not sid:
        return None
    return sessions.get(sid)


def verify_session_owner(request: Request, sess: Session) -> bool:
    user = get_current_user(request)
    if sess.account_username and user == sess.account_username:
        return True
    owner_tok = request.cookies.get("session_owner")
    return bool(sess.owner_token and owner_tok and hmac.compare_digest(owner_tok, sess.owner_token))


def require_session(
    request: Request,
    rate_key: str | None = None,
) -> tuple[Session | None, HTMLResponse | None]:
    """Rate-check + session + ownership in one call. Returns (session, error_response)."""
    if rate_key:
        ip = client_ip(request)
        if not check_rate(ip, rate_key):
            return None, HTMLResponse(
                "<p class='feedback error'>Too many attempts. Try again later.</p>",
                status_code=429,
            )
    sess = get_session(request)
    if not sess or not verify_session_owner(request, sess):
        return None, HTMLResponse("<p class='feedback error'>Invalid session.</p>", status_code=403)
    return sess, None


def check_creation_limits(request: Request) -> HTMLResponse | None:
    """Rate-check + stale purge + session-count guard. Returns error response or None."""
    ip = client_ip(request)
    if not check_rate(ip, "create_room"):
        return HTMLResponse(
            "<p class='feedback error'>Too many attempts. Try again later.</p>",
            status_code=429,
        )
    purge_stale()
    if count_sessions_for_ip(ip) >= MAX_SESSIONS_PER_IP:
        return HTMLResponse(
            "<p class='feedback error'>Too many active sessions.</p>",
            status_code=429,
        )
    return None


def alive_sessions(room: Room) -> list[str]:
    return [s for s in room.sessions if s not in room.eliminated]


def room_host_sid(room: Room) -> str | None:
    """First alive session is the host."""
    for sid in room.sessions:
        if sid not in room.eliminated:
            return sid
    return None


def active_session_id(room: Room) -> str | None:
    alive = alive_sessions(room)
    if not alive:
        return None
    idx = room.turn_index % len(alive)
    return alive[idx]


def compute_time_limit(word: str, streak: int = 0, multiplayer: bool = False) -> float:
    chars = max(len(word) / 5, 0.2)
    wpm_required = 10.0 if multiplayer else 5 * streak**0.8 + 10
    return max(3.0, (chars / wpm_required) * 60)


def compute_wpm(guess: str, elapsed: float) -> float:
    elapsed = max(elapsed, 2.0)
    chars = max(len(guess) / 5, 0.2)
    return min(300.0, chars / (elapsed / 60))


def evaluate_guess(guess: str, word_entry: dict[str, Any]) -> tuple[bool, str | None]:
    """Returns (correct, matched_homophone_or_None)."""
    target = word_entry["word"].lower()
    g = guess.strip().lower()
    if g == target:
        return True, None
    for h in word_entry.get("homophones", []):
        if g == h.lower():
            return True, h
    return False, None


def is_name_reserved(player_name: str, account_username: str | None) -> bool:
    """Return True if player_name belongs to a registered account that isn't the current user."""
    if not player_name or player_name == account_username:
        return False
    with _db_lock:
        return (
            db.execute("SELECT 1 FROM users WHERE username = ?", (player_name,)).fetchone()
            is not None
        )


def record_guess_stats(
    username: str | None,
    wpm: float,
    word_str: str,
    correct: bool,
    tier: str = "",
    streak: int = 0,
) -> None:
    if not username:
        return
    with _db_lock:
        db.execute(
            "INSERT INTO guess_log(username, word, correct, wpm, tier, ts) VALUES(?,?,?,?,?,?)",
            (username, word_str, int(correct), wpm, tier, time.time()),
        )
        if correct and streak > 0:
            db.execute(
                "UPDATE users SET best_streak = MAX(best_streak, ?) WHERE username = ?",
                (streak, username),
            )
        db.commit()


# ── ELO ─


def update_elo(players: list[dict[str, Any]], k: float = 32.0) -> None:
    n = len(players)
    if n < 2:
        return
    norm = n * (n - 1) / 2
    denom = sum(math.exp(0.01 * p["elo"]) for p in players)
    for p in players:
        p["elo"] += k * ((n - p["rank"]) / norm - math.exp(0.01 * p["elo"]) / denom)


# ── Rate limiter ──

rate_buckets: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

RATE_LIMITS: dict[str, tuple[int, int]] = {
    "login": (5, 60),
    "register": (3, 60),
    "create_room": (5, 60),
    "chat": (10, 30),
    "guess": (60, 60),
}


def check_rate(ip: str, action: str) -> bool:
    limit, window = RATE_LIMITS[action]
    now = time.time()
    bucket = rate_buckets[ip][action]
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


# ── Middleware


class BodyLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if request.method == "POST":
            cl = request.headers.get("content-length")
            if cl:
                try:
                    if int(cl) > MAX_BODY:
                        return HTMLResponse(
                            "<p class='error'>Request too large.</p>",
                            status_code=413,
                        )
                except ValueError:
                    return HTMLResponse("<p class='error'>Invalid request.</p>", status_code=400)
        return await call_next(request)


# ── App ─

app = FastAPI()
app.add_middleware(BodyLimitMiddleware)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
app.mount("/audios", StaticFiles(directory=str(ROOT / "audios")), name="audios")

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


def client_ip(request: Request) -> str:
    if request.client:
        return request.client.host
    return "unknown"


def _catalog_ctx() -> dict[str, Any]:
    return {
        "difficulties": DIFFICULTIES,
        "tier_colors": TIER_COLORS,
        "words": WORDS,
        "total_words": TOTAL_WORDS,
    }


def tpl(request: Request, name: str, ctx: dict[str, Any] | None = None) -> HTMLResponse:
    c: dict[str, Any] = {
        "request": request,
        "user": get_current_user(request),
    }
    if ctx:
        c.update(ctx)
    return templates.TemplateResponse(request, name, c)


# ── Room helpers ──


def serve_new_word(room: Room, streak: int = 0) -> None:
    word_data = pick_word(room.difficulty)
    room.current_word = word_data
    room.word_served_at = time.time()
    room.draft_text = ""
    word_str = word_data["word"]
    is_solo = room.visibility == "solo"
    tl = compute_time_limit(word_str, streak=streak, multiplayer=not is_solo)
    room.turn_time_limit = tl
    room.turn_deadline = time.time() + tl


def check_timeout(room: Room) -> bool:
    """Check and handle timeout. Returns True if someone was eliminated."""
    if room.winner or room.intermission_until > time.time():
        return False
    if room.turn_deadline <= 0:
        return False
    if time.time() < room.turn_deadline:
        return False
    active_sid = active_session_id(room)
    if not active_sid:
        return False
    sess = sessions.get(active_sid)
    if sess:
        sess.last_feedback = {
            "title": "Time's up",
            "body": "Counted as a skip.",
            "type": "error",
        }
    room.eliminated.add(active_sid)
    advance_turn(room, eliminated=True)
    return True


def advance_turn(room: Room, eliminated: bool = False) -> None:
    alive = alive_sessions(room)
    if len(alive) <= 1:
        finish_game(room)
        return
    if not eliminated:
        room.turn_index = (room.turn_index + 1) % len(alive)
    else:
        # After elimination, alive list shifted. Wrap index if past end.
        room.turn_index = room.turn_index % len(alive)
    room.draft_text = ""
    serve_new_word(room)
    room.last_activity = time.time()


def finish_game(room: Room) -> None:
    alive = alive_sessions(room)
    winner_sid = alive[0] if alive else None
    winner_sess = sessions.get(winner_sid) if winner_sid else None
    room.winner = winner_sess.player_name if winner_sess else "Nobody"
    room.turn_deadline = 0
    room.draft_text = ""

    # Build rankings
    rankings: list[dict[str, Any]] = []
    elim_order = [s for s in room.sessions if s in room.eliminated]
    for rank_idx, sid in enumerate(reversed(elim_order)):
        s = sessions.get(sid)
        if s:
            rankings.append(
                {
                    "sid": sid,
                    "name": s.player_name,
                    "rank": rank_idx + 2,
                    "account": s.account_username,
                },
            )
    if winner_sid:
        rankings.append(
            {
                "sid": winner_sid,
                "name": winner_sess.player_name,
                "rank": 1,
                "account": winner_sess.account_username if winner_sess else None,
            },
        )  # type: ignore[union-attr]

    # ELO update (skip for local games)
    tracked: list[dict[str, Any]] = []
    if room.visibility != "local":
        with _db_lock:
            for r in rankings:
                if r["account"]:
                    row = db.execute(
                        "SELECT * FROM users WHERE username = ?",
                        (r["account"],),
                    ).fetchone()
                    if row:
                        tracked.append(
                            {
                                **r,
                                "elo": float(row["elo"]),
                                "old_elo": float(row["elo"]),
                            },
                        )
            if len(tracked) >= 2:
                update_elo(tracked)
                n_players = len(tracked)
                now = time.time()
                for t in tracked:
                    db.execute(
                        "UPDATE users SET elo = ? WHERE username = ?",
                        (t["elo"], t["account"]),
                    )
                    db.execute(
                        "INSERT INTO match_results(username, rank, players, ts) VALUES(?,?,?,?)",
                        (t["account"], t["rank"], n_players, now),
                    )
                db.commit()

    room.last_match_results = []
    for r in rankings:
        entry: dict[str, Any] = {"name": r["name"], "rank": r["rank"], "sid": r["sid"]}
        for t in tracked:
            if t["sid"] == r["sid"]:
                entry["elo"] = round(t["elo"], 1)
                entry["elo_delta"] = round(t["elo"] - t["old_elo"], 1)
        room.last_match_results.append(entry)

    room.last_match_results.sort(key=lambda x: x["rank"])
    if room.visibility != "local":
        room.intermission_until = time.time() + 15
    room.last_activity = time.time()


def start_new_game(room: Room) -> None:
    room.eliminated.clear()
    room.winner = None
    room.last_match_results = []
    room.intermission_until = 0
    room.game_number += 1
    room.turn_index = 0
    room.draft_text = ""
    for sid in room.sessions:
        if sid in sessions:
            sessions[sid].last_feedback = {}
    serve_new_word(room)
    room.last_activity = time.time()


# ── Routes ───


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    elo = None
    if user:
        row = db.execute("SELECT elo FROM users WHERE username = ?", (user,)).fetchone()
        if row:
            elo = row["elo"]
    # Reconnection: detect if session is still in an active room
    reconnect_code = None
    reconnect_mode = None
    sess = get_session(request)
    if sess and sess.room_code:
        rc_room = rooms.get(sess.room_code)
        if rc_room:
            reconnect_code = sess.room_code
            vis = rc_room.visibility
            reconnect_mode = (
                vis if vis in ("solo", "local") else ("public" if vis == "public" else "lobby")
            )
    # Active games indicator
    active_games: list[dict[str, Any]] = []
    total_active_players = 0
    for r in rooms.values():
        if r.current_word and not r.winner and r.visibility == "public":
            n = len(alive_sessions(r))
            if n > 0:
                active_games.append({"difficulty": r.difficulty, "players": n})
                total_active_players += n
    return tpl(
        request,
        "index.html",
        {
            "elo": elo,
            "reconnect_code": reconnect_code,
            "reconnect_mode": reconnect_mode,
            "active_games": active_games,
            "total_active_players": total_active_players,
            **_catalog_ctx(),
        },
    )


@app.post("/register", response_class=HTMLResponse)
async def register(request: Request) -> HTMLResponse:
    ip = client_ip(request)
    if not check_rate(ip, "register"):
        return HTMLResponse(
            "<p class='feedback error'>Too many attempts. Try again later.</p>",
            status_code=429,
        )

    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))

    def _err(msg: str) -> HTMLResponse:
        return tpl(request, "fragments/menu.html", {"auth_error": msg})

    if not re.match(r"^[A-Za-z0-9_]{3,24}$", username):
        return _err("Username must be 3-24 characters (letters, numbers, underscore).")

    if len(password) < 8 or len(password) > 128:
        return _err("Password must be 8-128 characters.")

    with _db_lock:
        existing = db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            return _err("Could not create account. Try a different username.")
        pw_hash = hash_password(password)
        db.execute("INSERT INTO users (username, pw_hash) VALUES (?, ?)", (username, pw_hash))
        db.commit()

    resp = tpl(request, "fragments/menu.html", {"user": username, "elo": 1000.0})
    set_auth_cookie(resp, username)
    return resp


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request) -> HTMLResponse:
    ip = client_ip(request)
    if not check_rate(ip, "login"):
        return HTMLResponse(
            "<p class='feedback error'>Too many attempts. Try again later.</p>",
            status_code=429,
        )

    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))

    with _db_lock:
        row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    if not row or not verify_password(password, row["pw_hash"]):
        return tpl(
            request,
            "fragments/menu.html",
            {"auth_error": "Unknown username or password."},
        )

    resp = tpl(request, "fragments/menu.html", {"user": row["username"], "elo": row["elo"]})
    set_auth_cookie(resp, row["username"])
    return resp


@app.post("/logout", response_class=HTMLResponse)
async def logout(request: Request) -> HTMLResponse:
    resp = tpl(request, "fragments/menu.html", {"user": None})
    resp.delete_cookie("auth", path="/")
    return resp


@app.post("/guess", response_class=HTMLResponse)
async def guess(request: Request) -> HTMLResponse:
    """Handle guesses for all room modes."""
    ip = client_ip(request)
    if not check_rate(ip, "guess"):
        return HTMLResponse("<p class='feedback error'>Too many attempts.</p>", status_code=429)

    # Determine session: cookie-based for lobby/public/solo, local_sessions for local
    sess = get_session(request)
    room: Room | None = None
    is_local = False

    if sess and sess.room_code:
        room = rooms.get(sess.room_code)
        if room and room.visibility == "local":
            is_local = True
        elif not verify_session_owner(request, sess):
            sess = None

    if is_local and room:
        # For local mode, verify the cookie contains the active session
        local_sids = request.cookies.get("local_sessions", "").split(",")
        active_sid = active_session_id(room)
        if active_sid not in local_sids:
            return HTMLResponse("<p class='feedback error'>Invalid session.</p>", status_code=403)
        sess = sessions.get(active_sid)

    if not sess or not sess.room_code:
        return HTMLResponse("<p class='feedback error'>Invalid session.</p>", status_code=403)
    if not room:
        room = rooms.get(sess.room_code)
    if not room or active_session_id(room) != sess.id or not room.current_word:
        return Response(status_code=204)

    form = await request.form()
    guess_text = str(form.get("guess", "")).strip()
    word_data = room.current_word
    word_str = word_data["word"]
    elapsed = time.time() - room.word_served_at
    is_solo = room.visibility == "solo"

    # Server-side timer enforcement
    if room.turn_deadline > 0 and time.time() > room.turn_deadline:
        guess_text = ""

    sess.words_attempted += 1
    wpm = round(compute_wpm(guess_text, elapsed), 1) if guess_text else 0

    if is_solo:
        # Solo: no elimination, track streaks
        if not guess_text:
            sess.streak = 0
            sess.last_feedback = {
                "title": "Skipped",
                "body": f"Answer: {word_str}.",
                "type": "error",
            }
            sess.board_glow = "incorrect"
            record_guess_stats(
                sess.account_username,
                0,
                word_str,
                False,
                tier=word_data["tier"],
                streak=sess.streak,
            )
        else:
            correct, homophone = evaluate_guess(guess_text, word_data)
            if correct:
                sess.streak += 1
                sess.words_correct += 1
                body = f"{wpm} WPM."
                if homophone:
                    body = f'Accepted as "{homophone}". {wpm} WPM.'
                sess.last_feedback = {
                    "title": "Correct",
                    "body": body,
                    "type": "success",
                }
                sess.board_glow = "correct"
                record_guess_stats(
                    sess.account_username,
                    wpm,
                    word_str,
                    True,
                    tier=word_data["tier"],
                    streak=sess.streak,
                )
            else:
                sess.streak = 0
                sess.last_feedback = {
                    "title": "Incorrect",
                    "body": f"Answer: {word_str}. {wpm} WPM.",
                    "type": "error",
                }
                sess.board_glow = "incorrect"
                record_guess_stats(
                    sess.account_username,
                    wpm,
                    word_str,
                    False,
                    tier=word_data["tier"],
                    streak=sess.streak,
                )
        serve_new_word(room, streak=sess.streak)
    # Elimination mode (lobby, public, local)
    elif not guess_text:
        sess.last_feedback = {
            "title": "Eliminated",
            "body": f"Skipped. Answer: {word_str}.",
            "type": "error",
        }
        sess.board_glow = "incorrect"
        record_guess_stats(
            sess.account_username, 0, word_str, False, tier=word_data["tier"], streak=sess.streak
        )
        room.eliminated.add(sess.id)
        advance_turn(room, eliminated=True)
    else:
        correct, homophone = evaluate_guess(guess_text, word_data)
        if correct:
            sess.words_correct += 1
            body = f"{wpm} WPM. You stay in."
            if homophone:
                body = f'Accepted as "{homophone}". {body}'
            sess.last_feedback = {
                "title": "Correct",
                "body": body,
                "type": "success",
            }
            sess.board_glow = "correct"
            record_guess_stats(
                sess.account_username,
                wpm,
                word_str,
                True,
                tier=word_data["tier"],
                streak=sess.streak,
            )
            advance_turn(room, eliminated=False)
        else:
            sess.last_feedback = {
                "title": "Eliminated",
                "body": f"Answer: {word_str}.",
                "type": "error",
            }
            sess.board_glow = "incorrect"
            record_guess_stats(
                sess.account_username,
                wpm,
                word_str,
                False,
                tier=word_data["tier"],
                streak=sess.streak,
            )
            room.eliminated.add(sess.id)
            advance_turn(room, eliminated=True)

    if room.visibility in ("solo", "local") and is_local:
        active_sid_val = active_session_id(room)
        viewer = sessions.get(active_sid_val) if active_sid_val else sess
    else:
        viewer = sess
    return tpl(request, "fragments/room.html", build_room_ctx(request, room, viewer))


# ── Room creation / joining


@app.post("/room/create", response_class=HTMLResponse)
async def room_create(request: Request) -> HTMLResponse:
    err = check_creation_limits(request)
    if err:
        return err

    form = await request.form()
    difficulty = str(form.get("difficulty", DIFFICULTIES[0]))
    if difficulty not in DIFFICULTIES and difficulty != "randomizer":
        difficulty = DIFFICULTIES[0]
    visibility = str(form.get("visibility", "private"))
    if visibility not in ("private", "solo", "local"):
        visibility = "private"

    ip = client_ip(request)
    user = get_current_user(request)
    code = make_room_code()

    if visibility == "local":
        names: list[str] = json.loads(str(form.get("players", "[]")))
        room = Room(code=code, difficulty=difficulty, visibility="local")
        all_sids: list[str] = []
        first_sess: Session | None = None
        for i, name in enumerate(names[:MAX_LOCAL_PLAYERS]):
            sid = make_session_id()
            sess = Session(
                id=sid,
                player_name=(name.strip()[:24] or f"Player {i + 1}"),
                difficulty=difficulty,
                room_code=code,
                ip=ip,
            )
            sessions[sid] = sess
            room.sessions.append(sid)
            all_sids.append(sid)
            if first_sess is None:
                first_sess = sess
        rooms[code] = room
        serve_new_word(room)
        assert first_sess is not None
        active_sid = active_session_id(room)
        viewer = sessions[active_sid] if active_sid else first_sess
        resp = tpl(request, "fragments/room.html", build_room_ctx(request, room, viewer))
        resp.set_cookie("session_id", first_sess.id, httponly=True, samesite="lax", path="/")
        resp.set_cookie(
            "local_sessions",
            ",".join(all_sids),
            httponly=True,
            samesite="lax",
            path="/",
        )
        return resp

    # Solo or private lobby
    player_name = str(form.get("player_name", "Player")).strip()[:24] or "Player"
    if is_name_reserved(player_name, user):
        return HTMLResponse(
            "<p class='feedback error'>That name belongs to a registered account.</p>",
        )
    sid = make_session_id()
    is_solo = visibility == "solo"
    sess = Session(
        id=sid,
        player_name=player_name,
        difficulty=difficulty,
        room_code=code,
        account_username=user if is_solo else user,
        ip=ip,
    )
    owner_token: str | None = None
    if not user:
        owner_token = secrets.token_urlsafe(16)
        sess.owner_token = owner_token
    sessions[sid] = sess

    room = Room(code=code, difficulty=difficulty, visibility=visibility, sessions=[sid])
    rooms[code] = room

    if is_solo:
        serve_new_word(room)

    resp = tpl(request, "fragments/room.html", build_room_ctx(request, room, sess))
    resp.set_cookie("session_id", sid, httponly=True, samesite="lax", path="/")
    if owner_token:
        resp.set_cookie("session_owner", owner_token, httponly=True, samesite="lax", path="/")
    return resp


@app.post("/room/join", response_class=HTMLResponse)
async def room_join(request: Request) -> HTMLResponse:
    err = check_creation_limits(request)
    if err:
        return err

    form = await request.form()
    code = str(form.get("room_code", "")).strip().upper()
    player_name = str(form.get("player_name", "Player")).strip()[:24] or "Player"

    room = rooms.get(code)
    if not room:
        return HTMLResponse("<p class='feedback error'>Room not found.</p>")
    if room.locked:
        return HTMLResponse("<p class='feedback error'>Room is locked.</p>")
    if len(room.sessions) >= MAX_PLAYERS:
        return HTMLResponse("<p class='feedback error'>Room is full.</p>")

    user = get_current_user(request)
    if is_name_reserved(player_name, user):
        return HTMLResponse(
            "<p class='feedback error'>That name belongs to a registered account.</p>",
        )
    ip = client_ip(request)
    sid = make_session_id()
    sess = Session(
        id=sid,
        player_name=player_name,
        difficulty=room.difficulty,
        room_code=code,
        account_username=user,
        ip=ip,
    )
    owner_token: str | None = None
    if not user:
        owner_token = secrets.token_urlsafe(16)
        sess.owner_token = owner_token
    sessions[sid] = sess
    room.sessions.append(sid)
    room.last_activity = time.time()

    # If this is the second player joining a waiting room, start the game
    if len(room.sessions) == 2 and not room.current_word:
        serve_new_word(room)

    _advance_room_state(room)
    resp = tpl(request, "fragments/room.html", build_room_ctx(request, room, sess))
    resp.set_cookie("session_id", sid, httponly=True, samesite="lax", path="/")
    if owner_token:
        resp.set_cookie("session_owner", owner_token, httponly=True, samesite="lax", path="/")
    return resp


@app.post("/public/join", response_class=HTMLResponse)
async def public_join(request: Request) -> HTMLResponse:
    ip = client_ip(request)
    if not check_rate(ip, "create_room"):
        return HTMLResponse(
            "<p class='feedback error'>Too many attempts. Try again later.</p>",
            status_code=429,
        )

    user = get_current_user(request)
    if not user:
        return HTMLResponse(
            "<p class='feedback error'>Login required for Public Arena.</p>",
            status_code=403,
        )

    if count_sessions_for_ip(ip) >= MAX_SESSIONS_PER_IP:
        return HTMLResponse(
            "<p class='feedback error'>Too many active sessions.</p>",
            status_code=429,
        )

    form = await request.form()
    difficulty = str(form.get("difficulty", DIFFICULTIES[0]))
    if difficulty not in DIFFICULTIES and difficulty != "randomizer":
        difficulty = DIFFICULTIES[0]

    purge_stale()

    # Find existing public room for this difficulty
    target_room: Room | None = None
    for r in rooms.values():
        if (
            r.visibility == "public"
            and r.difficulty == difficulty
            and len(r.sessions) < MAX_PLAYERS
        ):
            _advance_room_state(r)
            if not r.winner:
                target_room = r
                break

    sid = make_session_id()
    if target_room is None:
        code = make_room_code()
        target_room = Room(code=code, difficulty=difficulty, visibility="public", sessions=[])
        rooms[code] = target_room

    sess = Session(
        id=sid,
        player_name=user,
        difficulty=difficulty,
        room_code=target_room.code,
        account_username=user,
        ip=ip,
    )
    sessions[sid] = sess
    target_room.sessions.append(sid)
    target_room.last_activity = time.time()

    if len(target_room.sessions) == 2 and not target_room.current_word:
        serve_new_word(target_room)

    _advance_room_state(target_room)
    resp = tpl(request, "fragments/room.html", build_room_ctx(request, target_room, sess))
    resp.set_cookie("session_id", sid, httponly=True, samesite="lax", path="/")
    return resp


def _advance_room_state(room: Room) -> None:
    """Check timeout and intermission — call before building context."""
    check_timeout(room)
    if room.intermission_until and time.time() >= room.intermission_until and room.winner:
        start_new_game(room)


def build_room_ctx(request: Request, room: Room, viewer: Session) -> dict[str, Any]:
    active_sid = active_session_id(room)
    is_active = viewer.id == active_sid
    active_sess = sessions.get(active_sid) if active_sid else None

    players: list[dict[str, Any]] = []
    for sid in room.sessions:
        s = sessions.get(sid)
        if not s:
            continue
        status = "Waiting"
        if room.winner:
            # Find their rank in results
            for mr in room.last_match_results:
                if mr["sid"] == sid:
                    status = f"Rank {mr['rank']}"
                    break
        elif sid in room.eliminated:
            status = "Eliminated"
        elif sid == active_sid:
            status = "Spelling"
        highest_tier = ""
        if s.account_username:
            tr = db.execute(
                "SELECT tiers_cleared FROM users WHERE username=?", (s.account_username,)
            ).fetchone()
            if tr and tr["tiers_cleared"]:
                tiers = [t for t in tr["tiers_cleared"].split(",") if t and t in DIFFICULTIES]
                if tiers:
                    highest_tier = max(tiers, key=DIFFICULTIES.index)
        players.append(
            {
                "sid": sid,
                "name": s.player_name,
                "status": status,
                "is_viewer": sid == viewer.id,
                "eliminated": sid in room.eliminated,
                "account": s.account_username,
                "highest_tier": highest_tier,
            },
        )

    ctx: dict[str, Any] = {
        "room": room,
        "viewer": viewer,
        "players": players,
        "is_active": is_active,
        "active_player_name": active_sess.player_name if active_sess else "",
        "mode": room.visibility
        if room.visibility in ("solo", "local")
        else ("public" if room.visibility == "public" else "lobby"),
        "chat": list(room.chat),
        "tier_color": TIER_COLORS.get(room.difficulty, "#ffbf00"),
        "tier_colors": TIER_COLORS,
        "waiting_for_players": len(room.sessions) < 2 and not room.current_word,
        "is_host": viewer.id == room_host_sid(room),
        "room_locked": room.locked,
    }

    if room.winner:
        ctx["feedback"] = {
            "title": f"{room.winner} wins",
            "body": "",
            "type": "success",
        }
        # Per-viewer intermission feedback
        for mr in room.last_match_results:
            if mr["sid"] == viewer.id:
                parts = [f"Rank: {mr['rank']}."]
                if "elo" in mr:
                    parts.append(
                        f"ELO: {mr['elo']} ({'+' if mr['elo_delta'] >= 0 else ''}{mr['elo_delta']}).",
                    )
                remaining = max(0, round(room.intermission_until - time.time()))
                parts.insert(0, f"Next game in {remaining}s.")
                ctx["feedback"]["body"] = " ".join(parts)
                break
    elif room.current_word and not ctx["waiting_for_players"]:
        word_data = room.current_word
        ctx["word_length"] = len(word_data["word"])
        ctx["definition"] = word_data["definition"]
        ctx["part_of_speech"] = word_data["part_of_speech"]

        ctx["audio_url"] = (
            f"audios/{word_data['word'].lower()}.mp3" if has_audio(word_data["word"]) else None
        )

        if is_active:
            ctx["feedback"] = viewer.last_feedback or {
                "title": "Your turn",
                "body": "",
                "type": "info",
            }
        else:
            ctx["feedback"] = (
                viewer.last_feedback
                if viewer.id in room.eliminated
                else {
                    "title": f"{active_sess.player_name}'s turn" if active_sess else "Waiting",
                    "body": "",
                    "type": "info",
                }
            )

        if room.turn_deadline > 0:
            ctx["time_remaining"] = max(0, room.turn_deadline - time.time())
            ctx["time_limit"] = room.turn_time_limit

        ctx["draft_text"] = room.draft_text
        # Board glow: show once then clear
        if viewer.board_glow:
            ctx["board_glow"] = viewer.board_glow
            viewer.board_glow = None

    if room.visibility == "solo":
        ctx["streak"] = viewer.streak

    return ctx


@app.get("/room/{code}", response_class=HTMLResponse)
async def room_poll(request: Request, code: str) -> HTMLResponse:
    room = rooms.get(code)
    if not room:
        return HTMLResponse("<p class='feedback error'>Room not found.</p>", status_code=404)

    sess = get_session(request)
    if not sess or sess.room_code != code:
        return HTMLResponse("<p class='feedback error'>Not in this room.</p>", status_code=403)

    _advance_room_state(room)
    return tpl(request, "fragments/room.html", build_room_ctx(request, room, sess))


@app.post("/room/{code}/draft")
async def room_draft(request: Request, code: str) -> Response:
    room = rooms.get(code)
    if not room:
        return Response(status_code=404)

    sess, err = require_session(request, "guess")
    if err or not sess or sess.room_code != code:
        return Response(status_code=403)
    if active_session_id(room) != sess.id:
        return Response(status_code=403)

    form = await request.form()
    draft = str(form.get("draft", ""))[:MAX_DRAFT_LEN]
    room.draft_text = draft
    room.last_activity = time.time()
    return Response(status_code=204)


@app.post("/room/{code}/chat", response_class=HTMLResponse)
async def room_chat(request: Request, code: str) -> HTMLResponse:
    room = rooms.get(code)
    if not room:
        return HTMLResponse("<p class='feedback error'>Room not found.</p>", status_code=404)

    sess, err = require_session(request, "chat")
    if err:
        return err
    if not sess or sess.room_code != code:
        return HTMLResponse("<p class='feedback error'>Not in this room.</p>", status_code=403)

    form = await request.form()
    msg = str(form.get("message", "")).strip()[:MAX_CHAT_LEN]
    if msg:
        room.chat.append({"player": sess.player_name, "message": msg, "sid": sess.id})
        room.last_activity = time.time()

    return HTMLResponse("")


@app.post("/room/{code}/lock")
async def room_lock_toggle(request: Request, code: str) -> Response:
    room = rooms.get(code)
    if not room or room.visibility != "private":
        return HTMLResponse("<p class='feedback error'>Invalid room.</p>", status_code=403)
    sess, err = require_session(request)
    if err or not sess or sess.room_code != code:
        return HTMLResponse("<p class='feedback error'>Invalid session.</p>", status_code=403)
    if sess.id != room_host_sid(room):
        return HTMLResponse(
            "<p class='feedback error'>Only the host can lock.</p>", status_code=403
        )
    room.locked = not room.locked
    return Response(status_code=204)


@app.post("/forfeit", response_class=HTMLResponse)
async def forfeit(request: Request) -> HTMLResponse:
    sess, err = require_session(request)
    if err:
        return HTMLResponse("")

    if sess.room_code:
        room = rooms.get(sess.room_code)
        if room and sess.id in room.sessions:
            current_active = active_session_id(room)
            room.eliminated.add(sess.id)
            room.sessions.remove(sess.id)
            alive = alive_sessions(room)
            if len(alive) <= 1 and room.current_word and not room.winner:
                finish_game(room)
            elif current_active and current_active in alive:
                room.turn_index = alive.index(current_active)
            room.last_activity = time.time()

    sess.room_code = None
    return HTMLResponse("")


@app.post("/room/{code}/restart", response_class=HTMLResponse)
async def room_restart(request: Request, code: str) -> HTMLResponse:
    """Restart a solo/local game."""
    room = rooms.get(code)
    if not room or room.visibility not in ("solo", "local"):
        return HTMLResponse("<p class='feedback error'>Invalid room.</p>", status_code=403)

    # Auth: cookie session for solo, local_sessions cookie for local
    if room.visibility == "local":
        local_sids = request.cookies.get("local_sessions", "").split(",")
        if not any(sid in room.sessions for sid in local_sids):
            return HTMLResponse("<p class='feedback error'>Invalid session.</p>", status_code=403)
    else:
        sess = get_session(request)
        if not sess or not verify_session_owner(request, sess) or sess.room_code != code:
            return HTMLResponse("<p class='feedback error'>Invalid session.</p>", status_code=403)

    for sid in room.sessions:
        if sid in sessions:
            sessions[sid].streak = 0
    start_new_game(room)

    active_sid = active_session_id(room)
    viewer = sessions.get(active_sid) if active_sid else sessions.get(room.sessions[0])
    return tpl(request, "fragments/room.html", build_room_ctx(request, room, viewer))


@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard(request: Request) -> HTMLResponse:
    rows = db.execute("SELECT * FROM users ORDER BY elo DESC").fetchall()
    return tpl(request, "fragments/leaderboard.html", {"players": rows})


def _account_ctx(row: sqlite3.Row) -> dict[str, Any]:
    username = row["username"]
    recent = db.execute(
        "SELECT word, correct, wpm, tier, ts FROM guess_log "
        "WHERE username=? ORDER BY ts DESC LIMIT 20",
        (username,),
    ).fetchall()
    practice = db.execute(
        "SELECT word, COUNT(*) as n FROM guess_log "
        "WHERE username=? AND correct=0 GROUP BY word ORDER BY n DESC LIMIT 10",
        (username,),
    ).fetchall()
    practice_enriched = [
        {
            "word": r["word"],
            "n": r["n"],
            "definition": ALL_WORDS.get(r["word"], {}).get("definition", ""),
        }
        for r in practice
    ]
    avg_row = db.execute(
        "SELECT AVG(wpm) as avg_wpm FROM (SELECT wpm FROM guess_log "
        "WHERE username=? AND correct=1 ORDER BY ts DESC LIMIT 50)",
        (username,),
    ).fetchone()
    avg_wpm = round(avg_row["avg_wpm"], 1) if avg_row and avg_row["avg_wpm"] else 0
    return {
        "recent": recent,
        "practice": practice_enriched,
        "avg_wpm": avg_wpm,
        "tier_colors": TIER_COLORS,
    }


@app.get("/account/{username}", response_class=HTMLResponse)
async def account_view(request: Request, username: str) -> HTMLResponse:
    row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        return HTMLResponse("<p class='feedback error'>Player not found.</p>", status_code=404)
    return tpl(request, "fragments/account.html", {"player": row, **_account_ctx(row)})


@app.get("/account", response_class=HTMLResponse)
async def own_account(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if not user:
        return HTMLResponse("<p class='feedback error'>Not logged in.</p>", status_code=403)
    row = db.execute("SELECT * FROM users WHERE username = ?", (user,)).fetchone()
    if not row:
        return HTMLResponse("<p class='feedback error'>Player not found.</p>", status_code=404)
    return tpl(request, "fragments/account.html", {"player": row, **_account_ctx(row)})

"""Spelling Bee — FastAPI HTTP shell."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import string
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from starlette.responses import Response as StarletteResponse

from backend import db
from backend.auth import _session_cookie_kwargs, get_current_user, set_session_cookie
from backend.errors import HtmxError
from backend.game import (
    Catalog,
    MAX_CHAT_LEN,
    MAX_LOCAL_PLAYERS,
    MAX_PLAYERS,
    MAX_SESSIONS_PER_IP,
    MAX_WORD_LEN,
    RATE_LIMITS,
    ROOT,
    STALE_MINUTES,
    Visibility,
    Room,
    Session,
    active_session_id,
    alive_sessions,
    clean_name,
    display_mode,
    feedback,
    make_session_id,
    room_host_sid,
)
from backend.persistence import (
    load_highest_tier,
    is_name_reserved,
    persist_match_elo,
    record_guess_stats,
)
from routes.account import router as account_router
from routes.auth import router as auth_router
from templating import client_ip, tpl, templates



class ImmutableStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Any) -> StarletteResponse:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


# ── Config (HTTP-only) ──

DB_PATH = ROOT / "spellingbee.db"
MAX_BODY = 8 * 1024
DISCONNECT_GRACE = 30  # seconds before a disconnected player is auto-forfeited

# ── App state ──


@dataclass
class AppState:
    catalog: Catalog
    sessions: dict[str, Session] = field(default_factory=dict)
    rooms: dict[str, Room] = field(default_factory=dict)
    rate_buckets: dict[str, dict[str, list[float]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(list)),
    )
    subscribers: dict[str, set[asyncio.Queue]] = field(default_factory=lambda: defaultdict(set))
    room_timers: dict[str, asyncio.TimerHandle] = field(default_factory=dict)
    disconnect_timers: dict[str, asyncio.TimerHandle] = field(default_factory=dict)

    def make_room(self, code: str, difficulty: str, visibility: Visibility) -> Room:
        return Room(
            code=code,
            difficulty=difficulty,
            visibility=visibility,
            sessions_map=self.sessions,
            catalog=self.catalog,
        )

    def make_room_code(self) -> str:
        chars = string.ascii_uppercase + string.digits
        while True:
            code = "".join(secrets.choice(chars) for _ in range(6))
            if code not in self.rooms:
                return code

    def check_rate(self, ip: str, action: str) -> bool:
        limit, window = RATE_LIMITS[action]
        now = time.time()
        bucket = self.rate_buckets[ip][action]
        bucket[:] = [t for t in bucket if now - t < window]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    def count_sessions_for_ip(self, ip: str) -> int:
        return sum(1 for s in self.sessions.values() if s.ip == ip)

    def add_player_to_room(
        self,
        room: Room,
        player_name: str,
        difficulty: str,
        ip: str,
        account: str | None = None,
        highest_tier: str = "",
    ) -> Session:
        sid = make_session_id()
        sess = Session(
            id=sid,
            player_name=player_name,
            difficulty=difficulty,
            room_code=room.code,
            account_username=account,
            ip=ip,
        )
        if highest_tier:
            sess.highest_tier = highest_tier
        self.sessions[sid] = sess
        room.sessions.append(sid)
        room.last_activity = time.time()
        return sess

    def purge_stale(self) -> None:
        cutoff = time.time() - STALE_MINUTES * 60
        stale_rooms = [c for c, r in self.rooms.items() if r.last_activity < cutoff]
        for c in stale_rooms:
            for sid in self.rooms[c].sessions:
                handle = self.disconnect_timers.pop(sid, None)
                if handle:
                    handle.cancel()
                self.sessions.pop(sid, None)
            for q in self.subscribers.pop(c, set()):
                q.put_nowait({"event": "refresh", "data": ""})
            handle = self.room_timers.pop(c, None)
            if handle:
                handle.cancel()
            del self.rooms[c]
        stale_sessions = [
            s for s, sess in self.sessions.items()
            if (sess.room_code and sess.room_code not in self.rooms) or sess.last_activity < cutoff
        ]
        for s in stale_sessions:
            self.sessions.pop(s, None)
        stale_ips = [
            ip for ip, actions in self.rate_buckets.items()
            if all(not ts for ts in actions.values())
        ]
        for ip in stale_ips:
            del self.rate_buckets[ip]

    def room_changed(self, code: str) -> None:
        """Wake SSE subscribers and reschedule the timer for the next deadline."""
        for q in self.subscribers.get(code, set()):
            q.put_nowait({"event": "refresh", "data": ""})
        self._arm_room_timer(code)

    def draft_changed(self, code: str, text: str) -> None:
        """Push draft text to SSE subscribers without a full refresh."""
        for q in self.subscribers.get(code, set()):
            q.put_nowait({"event": "draft", "data": text})

    def _arm_room_timer(self, code: str) -> None:
        handle = self.room_timers.pop(code, None)
        if handle:
            handle.cancel()
        room = self.rooms.get(code)
        if not room:
            return
        now = time.time()
        targets = [t for t in (room.turn_deadline, room.intermission_until) if t > now]
        if not targets:
            return
        delay = min(targets) - now
        loop = asyncio.get_running_loop()
        self.room_timers[code] = loop.call_later(delay, self._room_timer_fire, code)

    def _room_timer_fire(self, code: str) -> None:
        self.room_timers.pop(code, None)
        room = self.rooms.get(code)
        if not room:
            return
        rankings = room.tick()
        self.room_changed(code)
        if rankings:
            asyncio.create_task(
                persist_match_elo(room, rankings, notify=self.room_changed),
                name=f"persist-{code}",
            )

    def _schedule_disconnect_forfeit(self, code: str, sid: str) -> None:
        room = self.rooms.get(code)
        if not room or room.visibility in ("solo", "local"):
            return
        if sid not in room.sessions:
            return
        loop = asyncio.get_running_loop()
        handle = loop.call_later(DISCONNECT_GRACE, self._disconnect_forfeit, code, sid)
        self.disconnect_timers[sid] = handle

    def _disconnect_forfeit(self, code: str, sid: str) -> None:
        self.disconnect_timers.pop(sid, None)
        room = self.rooms.get(code)
        if not room:
            return
        rankings = room.forfeit(sid)
        self.room_changed(code)
        if rankings:
            asyncio.create_task(
                persist_match_elo(room, rankings, notify=self.room_changed),
                name=f"persist-forfeit-{sid}",
            )


# ── HTTP helpers ──


def get_session(state: AppState, request: Request) -> Session | None:
    sid = request.cookies.get("session_id")
    if not sid:
        return None
    return state.sessions.get(sid)


def require_session(
    state: AppState,
    request: Request,
    rate_key: str | None = None,
) -> Session:
    """Rate-check + session lookup. Raises HtmxError on failure."""
    if rate_key:
        ip = client_ip(request)
        if not state.check_rate(ip, rate_key):
            raise HtmxError("Too many attempts. Try again later.", 429)
    sess = get_session(state, request)
    if not sess:
        raise HtmxError("Invalid session.", 403)
    return sess


def require_room(
    state: AppState,
    request: Request,
    code: str,
    rate_key: str | None = None,
) -> tuple[Session, Room]:
    sess = require_session(state, request, rate_key)
    if sess.room_code != code:
        raise HtmxError("Not in this room.", 403)
    room = state.rooms.get(code)
    if not room:
        raise HtmxError("Room not found.", 404)
    return sess, room


def check_creation_limits(state: AppState, request: Request) -> None:
    """Rate-check + stale purge + session-count guard. Raises HtmxError on failure."""
    ip = client_ip(request)
    if not state.check_rate(ip, "create_room"):
        raise HtmxError("Too many attempts. Try again later.", 429)
    state.purge_stale()
    if state.count_sessions_for_ip(ip) >= MAX_SESSIONS_PER_IP:
        raise HtmxError("Too many active sessions.", 429)


# ── Middleware ──


class BodyLimitMiddleware:
    """Pure ASGI middleware — zero overhead for non-POST / static / SSE requests.

    Enforces MAX_BODY on both Content-Length (fast path) and chunked bodies
    (streaming path) so clients cannot bypass the limit by omitting the header.
    """

    def __init__(self, app: Any, max_bytes: int = MAX_BODY) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        cl = next((v for k, v in scope.get("headers", []) if k == b"content-length"), None)
        if cl:
            try:
                if int(cl) > self.max_bytes:
                    resp = HTMLResponse("<p class='error'>Request too large.</p>", status_code=413)
                    await resp(scope, receive, send)
                    return
            except ValueError:
                resp = HTMLResponse("<p class='error'>Invalid request.</p>", status_code=400)
                await resp(scope, receive, send)
                return

        # No Content-Length (chunked): wrap receive and enforce limit during streaming.
        total = 0

        sent = False

        async def limited_receive() -> Any:
            nonlocal total, sent
            msg = await receive()
            if msg.get("type") == "http.request":
                total += len(msg.get("body", b""))
                if total > self.max_bytes and not sent:
                    sent = True
                    resp = HTMLResponse("<p class='error'>Request too large.</p>", status_code=413)
                    await resp(scope, receive, send)
                    return {"type": "http.disconnect"}
            return msg

        await self.app(scope, limited_receive, send)


class SecurityHeadersMiddleware:
    """Add security response headers to every HTML response."""

    _HEADERS: list[tuple[bytes, bytes]] = [
        (n.encode(), v.encode())
        for n, v in [
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "strict-origin-when-cross-origin"),
            ("Permissions-Policy", "geolocation=(), microphone=(), camera=()"),
            (
                "Content-Security-Policy",
                (
                    "default-src 'self'; "
                    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://unpkg.com https://cdn.jsdelivr.net; "
                    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                    "img-src 'self' data:; "
                    "connect-src 'self'; "
                    "media-src 'self'; "
                    "font-src 'self' https://cdn.jsdelivr.net; "
                    "frame-ancestors 'none';"
                ),
            ),
        ]
    ]

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": list(message.get("headers", [])) + self._HEADERS}
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ── App ─


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await db.init(DB_PATH)
    catalog = Catalog.load(ROOT)
    templates.env.globals["tier_colors"] = catalog.tier_colors
    _app.state.srv = AppState(catalog=catalog)
    yield
    await db.close()


app = FastAPI(lifespan=_lifespan)


@app.exception_handler(HtmxError)
async def htmx_error_handler(request: Request, exc: HtmxError) -> HTMLResponse:
    return HTMLResponse(
        f"<p class='feedback error'>{exc.message}</p>",
        status_code=exc.status_code,
    )


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(BodyLimitMiddleware, max_bytes=MAX_BODY)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
app.mount("/audios", ImmutableStaticFiles(directory=str(ROOT / "audios")), name="audios")
app.include_router(auth_router)
app.include_router(account_router)

# ── Routes ───


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    state: AppState = request.app.state.srv
    user = get_current_user(request)
    elo = None
    if user:
        row = await db.fetchone("SELECT elo FROM users WHERE username = ?", (user,))
        if row:
            elo = row["elo"]
    # Reconnection: detect if session is still in an active room
    reconnect_code = None
    reconnect_mode = None
    sess = get_session(state, request)
    if sess and sess.room_code:
        rc_room = state.rooms.get(sess.room_code)
        if rc_room:
            reconnect_code = sess.room_code
            vis = rc_room.visibility
            reconnect_mode = display_mode(vis)
    # Active games indicator
    active_games: list[dict[str, Any]] = []
    total_active_players = 0
    for r in state.rooms.values():
        if r.current_word and not r.winner and r.visibility == "public":
            n = len(alive_sessions(r))
            if n > 0:
                active_games.append({"difficulty": r.difficulty, "players": n})
                total_active_players += n
    template = "fragments/menu_page.html" if request.headers.get("HX-Request") else "index.html"
    return await tpl(
        request,
        template,
        {
            "elo": elo,
            "reconnect_code": reconnect_code,
            "reconnect_mode": reconnect_mode,
            "active_games": active_games,
            "total_active_players": total_active_players,
            **state.catalog.template_ctx(),
        },
    )


@app.post("/guess", response_class=HTMLResponse)
async def guess(request: Request) -> HTMLResponse:
    """Handle guesses for all room modes."""
    state: AppState = request.app.state.srv
    ip = client_ip(request)
    if not state.check_rate(ip, "guess"):
        raise HtmxError("Too many attempts.", 429)

    sess = get_session(state, request)
    room: Room | None = None

    if sess and sess.room_code:
        room = state.rooms.get(sess.room_code)
        if room and room.visibility == "local":
            local_sids = request.cookies.get("local_sessions", "").split(",")
            active_sid = active_session_id(room)
            if active_sid not in local_sids:
                raise HtmxError("Invalid session.", 403)
            sess = state.sessions.get(active_sid) if active_sid else None

    if not sess or not sess.room_code:
        raise HtmxError("Invalid session.", 403)
    if not room:
        room = state.rooms.get(sess.room_code)
    if not room or active_session_id(room) != sess.id or not room.current_word:
        return Response(status_code=204)

    form = await request.form()
    guess_text = re.sub(r"[^A-Za-z]", "", str(form.get("guess", "")))[:MAX_WORD_LEN]
    typing_ms: int | None = None
    raw_tm = form.get("typing_ms")
    if raw_tm is not None:
        try:
            parsed = int(str(raw_tm))
            if 0 <= parsed <= 10 * 60 * 1000:
                typing_ms = parsed
        except (TypeError, ValueError):
            pass
    result, rankings = room.submit_guess(sess, guess_text, typing_ms)
    if result:
        await record_guess_stats(
            sess.account_username,
            result.wpm,
            result.word,
            result.correct,
            tier=result.tier,
            streak=sess.streak,
        )
    if rankings:
        await persist_match_elo(room, rankings, notify=state.room_changed)
    else:
        state.room_changed(room.code)

    if room.visibility == "local":
        active_sid_val = active_session_id(room)
        viewer = state.sessions.get(active_sid_val) if active_sid_val else sess
    else:
        viewer = sess
    return await tpl(request, "fragments/room.html", build_room_ctx(state, room, viewer))


# ── Room creation / joining


@app.post("/room/create", response_class=HTMLResponse)
async def room_create(request: Request) -> HTMLResponse:
    state: AppState = request.app.state.srv
    check_creation_limits(state, request)

    form = await request.form()
    difficulty = state.catalog.validate_difficulty(str(form.get("difficulty", state.catalog.difficulties[0])))
    visibility: Visibility = "private"
    raw_vis = str(form.get("visibility", "private"))
    if raw_vis in ("private", "solo", "local"):
        visibility = raw_vis  # type: ignore[assignment]

    ip = client_ip(request)
    user = get_current_user(request)
    code = state.make_room_code()

    if visibility == "local":
        try:
            raw = json.loads(str(form.get("players", "[]")))
        except (json.JSONDecodeError, ValueError):
            raise HtmxError("Invalid player list.", 400)
        if not isinstance(raw, list) or not raw or not all(isinstance(n, str) for n in raw):
            raise HtmxError("Invalid player list.", 400)
        names: list[str] = raw
        room = state.make_room(code, difficulty, "local")
        all_sids: list[str] = []
        first_sess: Session | None = None
        for i, name in enumerate(names[:MAX_LOCAL_PLAYERS]):
            sess = state.add_player_to_room(
                room,
                clean_name(name, f"Player {i + 1}"),
                difficulty,
                ip,
            )
            all_sids.append(sess.id)
            if first_sess is None:
                first_sess = sess
        state.rooms[code] = room
        room.serve_new_word()
        active_sid = active_session_id(room)
        viewer = state.sessions.get(active_sid) if active_sid else first_sess
        resp = await tpl(request, "fragments/room.html", build_room_ctx(state, room, viewer))
        set_session_cookie(resp, first_sess.id)
        resp.set_cookie("local_sessions", ",".join(all_sids), **_session_cookie_kwargs())
        return resp

    # Solo or private lobby
    player_name = clean_name(str(form.get("player_name", "")))
    if await is_name_reserved(player_name, user):
        return HTMLResponse(
            "<p class='feedback error'>That name belongs to a registered account.</p>",
        )
    room = state.make_room(code, difficulty, visibility)
    state.rooms[code] = room
    highest_tier = await load_highest_tier(user, state.catalog.difficulties) if user else ""
    sess = state.add_player_to_room(
        room,
        player_name,
        difficulty,
        ip,
        account=user,
        highest_tier=highest_tier,
    )

    if visibility == "solo":
        room.serve_new_word()

    resp = await tpl(request, "fragments/room.html", build_room_ctx(state, room, sess))
    set_session_cookie(resp, sess.id)
    return resp


@app.post("/room/join", response_class=HTMLResponse)
async def room_join(request: Request) -> HTMLResponse:
    state: AppState = request.app.state.srv
    check_creation_limits(state, request)

    form = await request.form()
    code = re.sub(r"[^A-Z0-9]", "", str(form.get("room_code", "")).upper())[:6]
    player_name = clean_name(str(form.get("player_name", "")))

    room = state.rooms.get(code)
    if not room:
        return HTMLResponse("<p class='feedback error'>Room not found.</p>")
    if room.locked:
        return HTMLResponse("<p class='feedback error'>Room is locked.</p>")
    if len(room.sessions) >= MAX_PLAYERS:
        return HTMLResponse("<p class='feedback error'>Room is full.</p>")

    user = get_current_user(request)
    if await is_name_reserved(player_name, user):
        return HTMLResponse(
            "<p class='feedback error'>That name belongs to a registered account.</p>",
        )
    ip = client_ip(request)
    highest_tier = await load_highest_tier(user, state.catalog.difficulties) if user else ""
    sess = state.add_player_to_room(
        room,
        player_name,
        room.difficulty,
        ip,
        account=user,
        highest_tier=highest_tier,
    )
    room.begin_if_ready()
    state.room_changed(code)

    resp = await tpl(request, "fragments/room.html", build_room_ctx(state, room, sess))
    set_session_cookie(resp, sess.id)
    return resp


@app.post("/public/join", response_class=HTMLResponse)
async def public_join(request: Request) -> HTMLResponse:
    state: AppState = request.app.state.srv
    ip = client_ip(request)
    if not state.check_rate(ip, "create_room"):
        raise HtmxError("Too many attempts. Try again later.", 429)

    user = get_current_user(request)
    if not user:
        raise HtmxError("Login required for Public Arena.", 403)

    if state.count_sessions_for_ip(ip) >= MAX_SESSIONS_PER_IP:
        raise HtmxError("Too many active sessions.", 429)

    form = await request.form()
    difficulty = state.catalog.validate_difficulty(str(form.get("difficulty", state.catalog.difficulties[0])))

    state.purge_stale()

    # Find existing public room for this difficulty
    target_room: Room | None = None
    for r in state.rooms.values():
        if (
            r.visibility == "public"
            and r.difficulty == difficulty
            and len(r.sessions) < MAX_PLAYERS
        ):
            rankings = r.tick()
            if rankings:
                await persist_match_elo(r, rankings, notify=state.room_changed)
            else:
                state.room_changed(r.code)
            if not r.winner:
                target_room = r
                break

    if target_room is None:
        code = state.make_room_code()
        target_room = state.make_room(code, difficulty, "public")
        state.rooms[code] = target_room

    highest_tier = await load_highest_tier(user, state.catalog.difficulties)
    sess = state.add_player_to_room(
        target_room,
        user,
        difficulty,
        ip,
        account=user,
        highest_tier=highest_tier,
    )
    target_room.begin_if_ready()
    state.room_changed(target_room.code)

    resp = await tpl(request, "fragments/room.html", build_room_ctx(state, target_room, sess))
    set_session_cookie(resp, sess.id)
    return resp


def build_room_ctx(state: AppState, room: Room, viewer: Session) -> dict[str, Any]:
    active_sid = active_session_id(room)
    is_active = viewer.id == active_sid
    active_sess = state.sessions.get(active_sid) if active_sid else None

    players: list[dict[str, Any]] = []
    for sid in room.sessions:
        s = state.sessions.get(sid)
        if not s:
            continue
        status, status_class = room.player_status(sid)
        players.append(
            {
                "sid": sid,
                "name": s.player_name,
                "status": status,
                "status_class": status_class,
                "is_viewer": sid == viewer.id,
                "eliminated": sid in room.eliminated,
                "account": s.account_username,
                "highest_tier": s.highest_tier,
            },
        )

    ctx: dict[str, Any] = {
        "room": room,
        "viewer": viewer,
        "players": players,
        "is_active": is_active,
        "active_player_name": active_sess.player_name if active_sess else "",
        "mode": display_mode(room.visibility),
        "chat": list(room.chat),
        "waiting_for_players": len(room.sessions) < 2 and not room.current_word,
        "is_host": viewer.id == room_host_sid(room),
        "room_locked": room.locked,
    }

    if room.winner:
        ctx["feedback"] = feedback(f"{room.winner} wins", "", "success")
        # Per-viewer intermission feedback
        for mr in room.last_match_results:
            if mr["sid"] == viewer.id:
                parts = [f"Rank: {mr['rank']}."]
                if "elo" in mr:
                    parts.append(
                        f"ELO: {mr['elo']} ({'+' if mr['elo_delta'] >= 0 else ''}{mr['elo_delta']}).",
                    )
                ctx["feedback"]["body"] = " ".join(parts)
                break
        if room.intermission_until > time.time():
            ctx["intermission_remaining"] = max(0, room.intermission_until - time.time())
    elif room.current_word and not ctx["waiting_for_players"]:
        word_data = room.current_word
        ctx["word_length"] = len(word_data["word"])
        ctx["definition"] = word_data["definition"]
        ctx["part_of_speech"] = word_data["part_of_speech"]

        ctx["audio_url"] = (
            f"audios/{word_data['word'].lower()}.mp3" if state.catalog.has_audio(word_data["word"]) else None
        )
        ctx["audio_duration"] = room.word_audio_duration
        ctx["word_served_at"] = room.word_served_at

        if is_active:
            ctx["feedback"] = viewer.last_feedback or feedback("Your turn", "", "info")
        else:
            ctx["feedback"] = (
                viewer.last_feedback
                if viewer.id in room.eliminated
                else feedback(
                    f"{active_sess.player_name}'s turn" if active_sess else "Waiting",
                    "",
                    "info",
                )
            )

        if room.turn_deadline > 0:
            ctx["time_remaining"] = min(room.turn_time_limit, max(0, room.turn_deadline - time.time()))
            ctx["time_limit"] = room.turn_time_limit

        ctx["draft_text"] = room.draft_text

    if room.visibility == "solo":
        ctx["streak"] = viewer.streak

    return ctx


@app.get("/room/{code}", response_class=HTMLResponse)
async def room_poll(request: Request, code: str) -> HTMLResponse:
    state: AppState = request.app.state.srv
    sess, room = require_room(state, request, code)
    return await tpl(request, "fragments/room.html", build_room_ctx(state, room, sess))


@app.get("/room/{code}/stream")
async def room_stream(request: Request, code: str):
    state: AppState = request.app.state.srv
    room = state.rooms.get(code)
    sess = get_session(state, request)
    if not room or not sess or sess.room_code != code:
        return Response(status_code=403)

    sid = sess.id

    # Cancel any pending disconnect timer (player is reconnecting)
    handle = state.disconnect_timers.pop(sid, None)
    if handle:
        handle.cancel()

    q: asyncio.Queue[dict] = asyncio.Queue()
    state.subscribers[code].add(q)

    async def gen():
        try:
            yield {"event": "refresh", "data": ""}
            while code in state.rooms:
                msg = await q.get()
                # Drain queue, keeping only the latest per event type
                latest = {msg["event"]: msg}
                while not q.empty():
                    m = q.get_nowait()
                    latest[m["event"]] = m
                for m in latest.values():
                    yield m
        finally:
            state.subscribers[code].discard(q)
            if code in state.subscribers and not state.subscribers[code]:
                del state.subscribers[code]
            state._schedule_disconnect_forfeit(code, sid)

    return EventSourceResponse(gen(), ping=15)


@app.post("/room/{code}/draft")
async def room_draft(request: Request, code: str) -> Response:
    state: AppState = request.app.state.srv
    sess, room = require_room(state, request, code, "draft")
    if active_session_id(room) != sess.id:
        return Response(status_code=403)

    form = await request.form()
    draft = re.sub(r"[^A-Za-z]", "", str(form.get("draft", "")))[:MAX_WORD_LEN]
    room.set_draft(draft)
    state.draft_changed(code, draft)
    return Response(status_code=204)



@app.post("/room/{code}/chat", response_class=HTMLResponse)
async def room_chat(request: Request, code: str) -> HTMLResponse:
    state: AppState = request.app.state.srv
    sess, room = require_room(state, request, code, "chat")

    form = await request.form()
    msg = str(form.get("message", "")).strip()[:MAX_CHAT_LEN]
    if msg:
        room.add_chat({"player": sess.player_name, "message": msg, "sid": sess.id})
        state.room_changed(code)

    return HTMLResponse("")


@app.post("/room/{code}/lock")
async def room_lock_toggle(request: Request, code: str) -> Response:
    state: AppState = request.app.state.srv
    sess, room = require_room(state, request, code)
    if room.visibility != "private":
        return HTMLResponse("<p class='feedback error'>Invalid room.</p>", status_code=403)
    if sess.id != room_host_sid(room):
        raise HtmxError("Only the host can lock.", 403)
    room.toggle_lock()
    state.room_changed(code)
    return Response(status_code=204)


@app.post("/forfeit", response_class=HTMLResponse)
async def forfeit(request: Request) -> HTMLResponse:
    state: AppState = request.app.state.srv
    sess = get_session(state, request)
    if not sess:
        return HTMLResponse("")

    if sess.room_code:
        room = state.rooms.get(sess.room_code)
        if room and sess.id in room.sessions:
            rankings = room.forfeit(sess.id)
            if rankings:
                await persist_match_elo(room, rankings, notify=state.room_changed)
            else:
                state.room_changed(room.code)

    sess.room_code = None
    return HTMLResponse("")


@app.post("/room/{code}/restart", response_class=HTMLResponse)
async def room_restart(request: Request, code: str) -> HTMLResponse:
    """Restart a solo/local game."""
    state: AppState = request.app.state.srv
    sess, room = require_room(state, request, code)
    if room.visibility not in ("solo", "local"):
        return HTMLResponse("<p class='feedback error'>Invalid room.</p>", status_code=403)

    for sid in room.sessions:
        s = state.sessions.get(sid)
        if s:
            s.streak = 0
    room.start_new_game()
    state.room_changed(code)

    active_sid = active_session_id(room)
    viewer = (
        state.sessions.get(active_sid)
        if active_sid
        else state.sessions.get(room.sessions[0])
    )
    return await tpl(request, "fragments/room.html", build_room_ctx(state, room, viewer))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        proxy_headers=True,  # Trust proxy headers
        forwarded_allow_ips="127.0.0.1",  # ONLY trust headers from localhost
        server_header=False,  # Don't broadcast "Uvicorn" version
        limit_concurrency=100,  # Max simultaneous connections
        timeout_keep_alive=5,  # Seconds to keep an idle connection open
    )

"""Spelling Bee — FastAPI HTTP shell."""

from __future__ import annotations

import asyncio
import json
import re
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
from backend.auth import get_current_user
from backend.errors import HtmxError
from backend.game import (
    AUDIO_DURATIONS,
    DIFFICULTIES,
    MAX_CHAT_LEN,
    MAX_LOCAL_PLAYERS,
    MAX_PLAYERS,
    MAX_SESSIONS_PER_IP,
    MAX_WORD_LEN,
    ROOT,
    Visibility,
    GameState,
    Room,
    Session,
    active_session_id,
    alive_sessions,
    clean_name,
    display_mode,
    feedback,
    has_audio,
    room_host_sid,
    validate_difficulty,
)
from backend.persistence import (
    load_highest_tier,
    is_name_reserved,
    persist_match_elo,
    record_guess_stats,
)
from routes.account import router as account_router
from routes.auth import router as auth_router
from templating import _catalog_ctx, client_ip, tpl



class ImmutableStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Any) -> StarletteResponse:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


# ── Config (HTTP-only) ──

DB_PATH = ROOT / "spellingbee.db"
MAX_BODY = 8 * 1024
DISCONNECT_GRACE = 12  # seconds before a disconnected player is auto-forfeited

# ── Server state (wraps GameState, adds asyncio timers + SSE) ──


@dataclass
class ServerState:
    game: GameState = field(default_factory=GameState)
    subscribers: dict[str, set[asyncio.Event]] = field(default_factory=lambda: defaultdict(set))
    room_timers: dict[str, asyncio.TimerHandle] = field(default_factory=dict)
    disconnect_timers: dict[str, asyncio.TimerHandle] = field(default_factory=dict)

    def make_room(self, code: str, difficulty: str, visibility: Visibility) -> Room:
        return Room(
            code=code,
            difficulty=difficulty,
            visibility=visibility,
            on_change=self.room_changed,
            resolve_session=self.game.sessions.get,
        )

    def room_changed(self, code: str) -> None:
        """Wake SSE subscribers and reschedule the timer for the next deadline."""
        for ev in self.subscribers.get(code, set()):
            ev.set()
        self._arm_room_timer(code)

    def _arm_room_timer(self, code: str) -> None:
        """Cancel any pending timer and schedule the next deadline for this room."""
        handle = self.room_timers.pop(code, None)
        if handle:
            handle.cancel()
        room = self.game.rooms.get(code)
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
        room = self.game.rooms.get(code)
        if not room:
            return
        rankings = room.tick()
        if rankings:
            asyncio.create_task(
                persist_match_elo(room, rankings, notify=self.room_changed),
                name=f"persist-{code}",
            )

    def _schedule_disconnect_forfeit(self, code: str, sid: str) -> None:
        room = self.game.rooms.get(code)
        if not room or room.visibility in ("solo", "local"):
            return
        if sid not in room.sessions:
            return
        loop = asyncio.get_running_loop()
        handle = loop.call_later(DISCONNECT_GRACE, self._disconnect_forfeit, code, sid)
        self.disconnect_timers[sid] = handle

    def _disconnect_forfeit(self, code: str, sid: str) -> None:
        self.disconnect_timers.pop(sid, None)
        room = self.game.rooms.get(code)
        if not room:
            return
        rankings = room.forfeit(sid)
        if rankings:
            asyncio.create_task(
                persist_match_elo(room, rankings, notify=self.room_changed),
                name=f"persist-forfeit-{sid}",
            )

    def purge_stale(self) -> None:
        """Purge stale rooms/sessions and clean up associated timers/subscribers."""
        stale_rooms, purged_sids = self.game.purge_stale()
        for sid in purged_sids:
            handle = self.disconnect_timers.pop(sid, None)
            if handle:
                handle.cancel()
        for c in stale_rooms:
            for ev in self.subscribers.pop(c, set()):
                ev.set()
            handle = self.room_timers.pop(c, None)
            if handle:
                handle.cancel()


# ── HTTP helpers ──


def get_session(state: ServerState, request: Request) -> Session | None:
    sid = request.cookies.get("session_id")
    if not sid:
        return None
    return state.game.sessions.get(sid)


def require_session(
    state: ServerState,
    request: Request,
    rate_key: str | None = None,
) -> Session:
    """Rate-check + session lookup. Raises HtmxError on failure."""
    if rate_key:
        ip = client_ip(request)
        if not state.game.check_rate(ip, rate_key):
            raise HtmxError("Too many attempts. Try again later.", 429)
    sess = get_session(state, request)
    if not sess:
        raise HtmxError("Invalid session.", 403)
    return sess


def check_creation_limits(state: ServerState, request: Request) -> None:
    """Rate-check + stale purge + session-count guard. Raises HtmxError on failure."""
    ip = client_ip(request)
    if not state.game.check_rate(ip, "create_room"):
        raise HtmxError("Too many attempts. Try again later.", 429)
    state.purge_stale()
    if state.game.count_sessions_for_ip(ip) >= MAX_SESSIONS_PER_IP:
        raise HtmxError("Too many active sessions.", 429)


# ── Middleware ──


class BodyLimitMiddleware:
    """Pure ASGI middleware — zero overhead for non-POST / static / SSE requests."""

    def __init__(self, app: Any, max_bytes: int = MAX_BODY) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("method") == "POST":
            headers = dict(scope.get("headers", []))
            cl = headers.get(b"content-length")
            if cl:
                try:
                    if int(cl) > self.max_bytes:
                        resp = HTMLResponse(
                            "<p class='error'>Request too large.</p>", status_code=413,
                        )
                        await resp(scope, receive, send)
                        return
                except ValueError:
                    resp = HTMLResponse(
                        "<p class='error'>Invalid request.</p>", status_code=400,
                    )
                    await resp(scope, receive, send)
                    return
        await self.app(scope, receive, send)


# ── App ─


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await db.init(DB_PATH)
    _app.state.srv = ServerState()
    yield
    await db.close()


app = FastAPI(lifespan=_lifespan)


@app.exception_handler(HtmxError)
async def htmx_error_handler(request: Request, exc: HtmxError) -> HTMLResponse:
    return HTMLResponse(
        f"<p class='feedback error'>{exc.message}</p>",
        status_code=exc.status_code,
    )


app.add_middleware(BodyLimitMiddleware, max_bytes=MAX_BODY)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
app.mount("/audios", ImmutableStaticFiles(directory=str(ROOT / "audios")), name="audios")
app.include_router(auth_router)
app.include_router(account_router)

# ── Routes ───


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    state: ServerState = request.app.state.srv
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
        rc_room = state.game.rooms.get(sess.room_code)
        if rc_room:
            reconnect_code = sess.room_code
            vis = rc_room.visibility
            reconnect_mode = display_mode(vis)
    # Active games indicator
    active_games: list[dict[str, Any]] = []
    total_active_players = 0
    for r in state.game.rooms.values():
        if r.current_word and not r.winner and r.visibility == "public":
            n = len(alive_sessions(r))
            if n > 0:
                active_games.append({"difficulty": r.difficulty, "players": n})
                total_active_players += n
    return await tpl(
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


@app.post("/guess", response_class=HTMLResponse)
async def guess(request: Request) -> HTMLResponse:
    """Handle guesses for all room modes."""
    state: ServerState = request.app.state.srv
    ip = client_ip(request)
    if not state.game.check_rate(ip, "guess"):
        raise HtmxError("Too many attempts.", 429)

    # Determine session: cookie-based for lobby/public/solo, local_sessions for local
    sess = get_session(state, request)
    room: Room | None = None
    is_local = False

    if sess and sess.room_code:
        room = state.game.rooms.get(sess.room_code)
        if room and room.visibility == "local":
            is_local = True

    if is_local and room:
        # For local mode, verify the cookie contains the active session
        local_sids = request.cookies.get("local_sessions", "").split(",")
        active_sid = active_session_id(room)
        if active_sid not in local_sids:
            raise HtmxError("Invalid session.", 403)
        sess = state.game.sessions.get(active_sid)

    if not sess or not sess.room_code:
        raise HtmxError("Invalid session.", 403)
    if not room:
        room = state.game.rooms.get(sess.room_code)
    if not room or active_session_id(room) != sess.id or not room.current_word:
        return Response(status_code=204)

    form = await request.form()
    guess_text = re.sub(r"[^A-Za-z]", "", str(form.get("guess", "")))[:MAX_WORD_LEN]
    result, rankings = room.submit_guess(sess, guess_text)
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

    if room.visibility in ("solo", "local") and is_local:
        active_sid_val = active_session_id(room)
        viewer = state.game.sessions.get(active_sid_val) if active_sid_val else sess
    else:
        viewer = sess
    return await tpl(request, "fragments/room.html", build_room_ctx(state, room, viewer))


# ── Room creation / joining


@app.post("/room/create", response_class=HTMLResponse)
async def room_create(request: Request) -> HTMLResponse:
    state: ServerState = request.app.state.srv
    check_creation_limits(state, request)

    form = await request.form()
    difficulty = validate_difficulty(str(form.get("difficulty", DIFFICULTIES[0])))
    visibility: Visibility = "private"
    raw_vis = str(form.get("visibility", "private"))
    if raw_vis in ("private", "solo", "local"):
        visibility = raw_vis  # type: ignore[assignment]

    ip = client_ip(request)
    user = get_current_user(request)
    code = state.game.make_room_code()

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
            sess = state.game.add_player_to_room(
                room,
                clean_name(name, f"Player {i + 1}"),
                difficulty,
                ip,
            )
            all_sids.append(sess.id)
            if first_sess is None:
                first_sess = sess
        state.game.rooms[code] = room
        room.serve_new_word()
        active_sid = active_session_id(room)
        viewer = state.game.sessions.get(active_sid) if active_sid else first_sess
        resp = await tpl(request, "fragments/room.html", build_room_ctx(state, room, viewer))
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
    player_name = clean_name(str(form.get("player_name", "")))
    if await is_name_reserved(player_name, user):
        return HTMLResponse(
            "<p class='feedback error'>That name belongs to a registered account.</p>",
        )
    room = state.make_room(code, difficulty, visibility)
    state.game.rooms[code] = room
    highest_tier = await load_highest_tier(user) if user else ""
    sess = state.game.add_player_to_room(
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
    resp.set_cookie("session_id", sess.id, httponly=True, samesite="lax", path="/")
    return resp


@app.post("/room/join", response_class=HTMLResponse)
async def room_join(request: Request) -> HTMLResponse:
    state: ServerState = request.app.state.srv
    check_creation_limits(state, request)

    form = await request.form()
    code = re.sub(r"[^A-Z0-9]", "", str(form.get("room_code", "")).upper())[:6]
    player_name = clean_name(str(form.get("player_name", "")))

    room = state.game.rooms.get(code)
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
    highest_tier = await load_highest_tier(user) if user else ""
    sess = state.game.add_player_to_room(
        room,
        player_name,
        room.difficulty,
        ip,
        account=user,
        highest_tier=highest_tier,
    )
    room.notify_or_begin()

    resp = await tpl(request, "fragments/room.html", build_room_ctx(state, room, sess))
    resp.set_cookie("session_id", sess.id, httponly=True, samesite="lax", path="/")
    return resp


@app.post("/public/join", response_class=HTMLResponse)
async def public_join(request: Request) -> HTMLResponse:
    state: ServerState = request.app.state.srv
    ip = client_ip(request)
    if not state.game.check_rate(ip, "create_room"):
        raise HtmxError("Too many attempts. Try again later.", 429)

    user = get_current_user(request)
    if not user:
        raise HtmxError("Login required for Public Arena.", 403)

    if state.game.count_sessions_for_ip(ip) >= MAX_SESSIONS_PER_IP:
        raise HtmxError("Too many active sessions.", 429)

    form = await request.form()
    difficulty = validate_difficulty(str(form.get("difficulty", DIFFICULTIES[0])))

    state.purge_stale()

    # Find existing public room for this difficulty
    target_room: Room | None = None
    for r in state.game.rooms.values():
        if (
            r.visibility == "public"
            and r.difficulty == difficulty
            and len(r.sessions) < MAX_PLAYERS
        ):
            rankings = r.tick()
            if rankings:
                await persist_match_elo(r, rankings, notify=state.room_changed)
            if not r.winner:
                target_room = r
                break

    if target_room is None:
        code = state.game.make_room_code()
        target_room = state.make_room(code, difficulty, "public")
        state.game.rooms[code] = target_room

    highest_tier = await load_highest_tier(user)
    sess = state.game.add_player_to_room(
        target_room,
        user,
        difficulty,
        ip,
        account=user,
        highest_tier=highest_tier,
    )
    target_room.notify_or_begin()

    resp = await tpl(request, "fragments/room.html", build_room_ctx(state, target_room, sess))
    resp.set_cookie("session_id", sess.id, httponly=True, samesite="lax", path="/")
    return resp


def build_room_ctx(state: ServerState, room: Room, viewer: Session) -> dict[str, Any]:
    active_sid = active_session_id(room)
    is_active = viewer.id == active_sid
    active_sess = state.game.sessions.get(active_sid) if active_sid else None

    players: list[dict[str, Any]] = []
    for sid in room.sessions:
        s = state.game.sessions.get(sid)
        if not s:
            continue
        status = "Waiting"
        status_class = "waiting"
        if room.winner:
            # Find their rank in results
            for mr in room.last_match_results:
                if mr["sid"] == sid:
                    status = f"Rank {mr['rank']}"
                    status_class = "winner" if mr["rank"] == 1 else "eliminated"
                    break
        elif sid in room.eliminated:
            status = "Eliminated"
            status_class = "eliminated"
        elif sid == active_sid:
            status = "Spelling"
            status_class = "spelling"
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
            ctx["intermission_until"] = room.intermission_until
    elif room.current_word and not ctx["waiting_for_players"]:
        word_data = room.current_word
        ctx["word_length"] = len(word_data["word"])
        ctx["definition"] = word_data["definition"]
        ctx["part_of_speech"] = word_data["part_of_speech"]

        word_lower = word_data["word"].lower()
        ctx["audio_url"] = f"audios/{word_lower}.mp3" if has_audio(word_data["word"]) else None
        ctx["audio_duration"] = AUDIO_DURATIONS.get(word_lower, 0.0)
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
            ctx["turn_deadline"] = room.turn_deadline
            ctx["time_limit"] = room.turn_time_limit

        ctx["draft_text"] = room.draft_text

    if room.visibility == "solo":
        ctx["streak"] = viewer.streak

    return ctx


@app.get("/room/{code}", response_class=HTMLResponse)
async def room_poll(request: Request, code: str) -> HTMLResponse:
    state: ServerState = request.app.state.srv
    room = state.game.rooms.get(code)
    if not room:
        return HTMLResponse("<p class='feedback error'>Room not found.</p>", status_code=404)

    sess = get_session(state, request)
    if not sess or sess.room_code != code:
        return HTMLResponse("<p class='feedback error'>Not in this room.</p>", status_code=403)

    return await tpl(request, "fragments/room.html", build_room_ctx(state, room, sess))


@app.get("/room/{code}/stream")
async def room_stream(request: Request, code: str):
    state: ServerState = request.app.state.srv
    room = state.game.rooms.get(code)
    sess = get_session(state, request)
    if not room or not sess or sess.room_code != code:
        return Response(status_code=403)

    sid = sess.id

    # Cancel any pending disconnect timer (player is reconnecting)
    handle = state.disconnect_timers.pop(sid, None)
    if handle:
        handle.cancel()

    ev = asyncio.Event()
    state.subscribers[code].add(ev)

    async def gen():
        try:
            yield {"event": "refresh", "data": ""}
            while code in state.game.rooms:
                ev.clear()
                await ev.wait()
                yield {"event": "refresh", "data": ""}
        finally:
            state.subscribers[code].discard(ev)
            if code in state.subscribers and not state.subscribers[code]:
                del state.subscribers[code]
            state._schedule_disconnect_forfeit(code, sid)

    return EventSourceResponse(gen(), ping=15)


@app.post("/room/{code}/draft")
async def room_draft(request: Request, code: str) -> Response:
    state: ServerState = request.app.state.srv
    room = state.game.rooms.get(code)
    if not room:
        return Response(status_code=404)

    sess = require_session(state, request, "draft")
    if sess.room_code != code:
        return Response(status_code=403)
    if active_session_id(room) != sess.id:
        return Response(status_code=403)

    form = await request.form()
    draft = re.sub(r"[^A-Za-z]", "", str(form.get("draft", "")))[:MAX_WORD_LEN]
    room.set_draft(draft)
    return Response(status_code=204)


@app.get("/room/{code}/draft-text")
async def get_draft_text(request: Request, code: str, h: str = "") -> Response:
    state: ServerState = request.app.state.srv
    room = state.game.rooms.get(code)
    if not room:
        return Response(status_code=404)
    if h == room.draft_text:
        return Response(status_code=204)
    return Response(room.draft_text, media_type="text/plain")


@app.post("/room/{code}/chat", response_class=HTMLResponse)
async def room_chat(request: Request, code: str) -> HTMLResponse:
    state: ServerState = request.app.state.srv
    room = state.game.rooms.get(code)
    if not room:
        return HTMLResponse("<p class='feedback error'>Room not found.</p>", status_code=404)

    sess = require_session(state, request, "chat")
    if sess.room_code != code:
        raise HtmxError("Not in this room.", 403)

    form = await request.form()
    msg = str(form.get("message", "")).strip()[:MAX_CHAT_LEN]
    if msg:
        room.add_chat({"player": sess.player_name, "message": msg, "sid": sess.id})

    return HTMLResponse("")


@app.post("/room/{code}/lock")
async def room_lock_toggle(request: Request, code: str) -> Response:
    state: ServerState = request.app.state.srv
    room = state.game.rooms.get(code)
    if not room or room.visibility != "private":
        return HTMLResponse("<p class='feedback error'>Invalid room.</p>", status_code=403)
    sess = require_session(state, request)
    if sess.room_code != code:
        raise HtmxError("Invalid session.", 403)
    if sess.id != room_host_sid(room):
        raise HtmxError("Only the host can lock.", 403)
    room.toggle_lock()
    return Response(status_code=204)


@app.post("/forfeit", response_class=HTMLResponse)
async def forfeit(request: Request) -> HTMLResponse:
    state: ServerState = request.app.state.srv
    sess = get_session(state, request)
    if not sess:
        return HTMLResponse("")

    if sess.room_code:
        room = state.game.rooms.get(sess.room_code)
        if room and sess.id in room.sessions:
            rankings = room.forfeit(sess.id)
            if rankings:
                await persist_match_elo(room, rankings, notify=state.room_changed)

    sess.room_code = None
    return HTMLResponse("")


@app.post("/room/{code}/restart", response_class=HTMLResponse)
async def room_restart(request: Request, code: str) -> HTMLResponse:
    """Restart a solo/local game."""
    state: ServerState = request.app.state.srv
    room = state.game.rooms.get(code)
    if not room or room.visibility not in ("solo", "local"):
        return HTMLResponse("<p class='feedback error'>Invalid room.</p>", status_code=403)

    # Auth: cookie session for solo, local_sessions cookie for local
    if room.visibility == "local":
        local_sids = request.cookies.get("local_sessions", "").split(",")
        if not any(sid in room.sessions for sid in local_sids):
            return HTMLResponse("<p class='feedback error'>Invalid session.</p>", status_code=403)
    else:
        sess = get_session(state, request)
        if not sess or sess.room_code != code:
            return HTMLResponse("<p class='feedback error'>Invalid session.</p>", status_code=403)

    for sid in room.sessions:
        s = state.game.sessions.get(sid)
        if s:
            s.streak = 0
    room.start_new_game()

    active_sid = active_session_id(room)
    viewer = (
        state.game.sessions.get(active_sid)
        if active_sid
        else state.game.sessions.get(room.sessions[0])
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

"""Spelling Bee — game engine (pure logic, no IO)."""

from __future__ import annotations

import json
import math
import re
import secrets
import string
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast

if TYPE_CHECKING:
    from collections.abc import Callable

# ── Domain types ──

type Visibility = Literal["private", "public", "solo", "local"]


class WordEntry(TypedDict):
    word: str
    definition: str
    part_of_speech: str
    tier: str
    homophones: NotRequired[list[str]]


class Feedback(TypedDict):
    title: str
    body: str
    type: str


class Ranking(TypedDict):
    sid: str
    name: str
    rank: int
    account: str | None


class MatchResult(TypedDict):
    name: str
    rank: int
    sid: str
    elo: NotRequired[float]
    elo_delta: NotRequired[float]


def display_mode(vis: Visibility) -> str:
    return vis if vis in ("solo", "local") else ("public" if vis == "public" else "lobby")

# ── Config ───

ROOT = Path(__file__).resolve().parent.parent
MAX_CHAT = 80
MAX_CHAT_LEN = 150
MAX_WORD_LEN = 50
STALE_MINUTES = 30
NETWORK_GRACE = 1.5  # seconds added to deadline for network + render latency (audio accounted separately)
MAX_SESSIONS_PER_IP = 20
MAX_PLAYERS = 15
MAX_LOCAL_PLAYERS = 12

RATE_LIMITS: dict[str, tuple[int, int]] = {
    "login": (5, 60),
    "register": (3, 60),
    "create_room": (5, 60),
    "chat": (10, 30),
    "guess": (60, 60),
    "draft": (60, 60),
}

# ── Word catalog ──

with Path(ROOT / "wordlist.json").open() as _f:
    _raw_catalog: dict[str, Any] = json.load(_f)

TIER_COLORS: dict[str, str] = _raw_catalog["info"]["color"]
DIFFICULTIES: list[str] = [k for k in _raw_catalog if k != "info"]
WORDS: dict[str, dict[str, dict[str, Any]]] = {d: _raw_catalog[d] for d in DIFFICULTIES}
TOTAL_WORDS = sum(len(v) for v in WORDS.values())

ALL_WORDS: dict[str, dict[str, Any]] = {
    w: {**wdata, "tier": tier} for tier, wdict in WORDS.items() for w, wdata in wdict.items()
}

_WORD_KEYS: dict[str, list[str]] = {d: list(ws) for d, ws in WORDS.items()}
_ALL_WORD_KEYS: list[str] = list(ALL_WORDS)


def pick_word(difficulty: str) -> WordEntry:
    if difficulty == "randomizer":
        word_str = secrets.choice(_ALL_WORD_KEYS)
        return cast(WordEntry, {"word": word_str, **ALL_WORDS[word_str]})
    keys = _WORD_KEYS.get(difficulty, _WORD_KEYS[DIFFICULTIES[0]])
    pool = WORDS.get(difficulty, WORDS[DIFFICULTIES[0]])
    word_str = secrets.choice(keys)
    return cast(WordEntry, {"word": word_str, **pool[word_str], "tier": difficulty})


def _load_audio_durations() -> dict[str, float]:
    """Pre-scan audios/ for MP3 durations. Uses mutagen if available, else estimates from file size."""
    audios_dir = ROOT / "audios"
    if not audios_dir.is_dir():
        return {}
    durations: dict[str, float] = {}
    try:
        from mutagen.mp3 import MP3

        for p in audios_dir.glob("*.mp3"):
            try:
                durations[p.stem.lower()] = MP3(p).info.length
            except Exception:  # noqa: BLE001
                durations[p.stem.lower()] = len(p.stem) * 0.12 + 0.5
    except ImportError:
        for p in audios_dir.glob("*.mp3"):
            durations[p.stem.lower()] = len(p.stem) * 0.12 + 0.5
    return durations


AUDIO_DURATIONS: dict[str, float] = _load_audio_durations()


def has_audio(word: str) -> bool:
    return word.lower() in AUDIO_DURATIONS


# ── In-memory state ──


@dataclass
class Session:
    id: str
    player_name: str
    difficulty: str
    room_code: str | None = None
    account_username: str | None = None
    highest_tier: str = ""
    streak: int = 0
    words_attempted: int = 0
    words_correct: int = 0
    last_feedback: Feedback | None = None
    ip: str = ""
    last_activity: float = field(default_factory=time.time)


@dataclass
class GuessResult:
    correct: bool
    skipped: bool
    wpm: float
    word: str
    tier: str
    homophone: str | None = None


@dataclass
class Room:
    code: str
    difficulty: str
    visibility: Visibility
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
    last_match_results: list[MatchResult] = field(default_factory=list)
    locked: bool = False
    last_activity: float = field(default_factory=time.time)
    current_word: WordEntry | None = None
    word_served_at: float = 0.0
    # Callbacks — injected at construction; all default None so Room is
    # fully usable in tests / local mode without a GameState.
    on_change: Callable[[str], None] | None = field(default=None, repr=False)
    resolve_session: Callable[[str], Session | None] | None = field(default=None, repr=False)

    # --- State-transition methods ---

    def serve_new_word(self, streak: int = 0) -> None:
        word_data = pick_word(self.difficulty)
        self.current_word = word_data
        self.word_served_at = time.time()
        self.draft_text = ""
        word_str = word_data["word"]
        is_solo = self.visibility == "solo"
        tl = compute_time_limit(word_str, streak=streak, multiplayer=not is_solo)
        self.turn_time_limit = tl
        audio_dur = AUDIO_DURATIONS.get(word_str.lower(), 0.0)
        self.turn_deadline = time.time() + tl + audio_dur + NETWORK_GRACE

    def check_timeout(self) -> list[Ranking] | None:
        """Check and handle timeout. Returns rankings if game ended."""
        if self.winner or self.intermission_until > time.time():
            return None
        if self.turn_deadline <= 0:
            return None
        if time.time() < self.turn_deadline:
            return None
        active_sid = active_session_id(self)
        if not active_sid:
            return None
        sess = self.resolve_session(active_sid) if self.resolve_session else None
        if sess:
            sess.last_feedback = feedback("Time's up", "Counted as a skip.")
        self.eliminated.add(active_sid)
        return self.advance_turn(eliminated=True)

    def advance_turn(self, eliminated: bool = False) -> list[Ranking] | None:
        alive = alive_sessions(self)
        if len(alive) <= 1:
            return self.finish_game()
        if not eliminated:
            self.turn_index = (self.turn_index + 1) % len(alive)
        else:
            self.turn_index = self.turn_index % len(alive)
        self.draft_text = ""
        self.serve_new_word()
        self.last_activity = time.time()
        return None

    def finish_game(self) -> list[Ranking]:
        alive = alive_sessions(self)
        winner_sid = alive[0] if alive else None
        winner_sess = (
            (self.resolve_session(winner_sid) if self.resolve_session else None)
            if winner_sid
            else None
        )
        self.winner = winner_sess.player_name if winner_sess else "Nobody"
        self.turn_deadline = 0
        self.draft_text = ""

        rankings: list[Ranking] = []
        elim_order = [s for s in self.sessions if s in self.eliminated]
        for rank_idx, sid in enumerate(reversed(elim_order)):
            s = self.resolve_session(sid) if self.resolve_session else None
            if s:
                rankings.append(
                    Ranking(
                        sid=sid,
                        name=s.player_name,
                        rank=rank_idx + 2,
                        account=s.account_username,
                    ),
                )
        if winner_sid:
            rankings.append(
                Ranking(
                    sid=winner_sid,
                    name=winner_sess.player_name,  # type: ignore[union-attr]
                    rank=1,
                    account=winner_sess.account_username if winner_sess else None,
                ),
            )

        # Set basic scoreboard immediately (ELO enriched later by persist_match_elo)
        self.last_match_results = [
            MatchResult(name=r["name"], rank=r["rank"], sid=r["sid"]) for r in rankings
        ]
        self.last_match_results.sort(key=lambda x: x["rank"])
        if self.visibility != "local":
            self.intermission_until = time.time() + 15
        self.last_activity = time.time()

        return rankings

    def start_new_game(self) -> None:
        self.eliminated.clear()
        self.winner = None
        self.last_match_results = []
        self.intermission_until = 0
        self.game_number += 1
        self.turn_index = 0
        self.draft_text = ""
        for sid in self.sessions:
            s = self.resolve_session(sid) if self.resolve_session else None
            if s:
                s.last_feedback = None
        self.serve_new_word()
        self.last_activity = time.time()
        self._notify()

    def _notify(self) -> None:
        if self.on_change:
            self.on_change(self.code)

    def tick(self) -> list[Ranking] | None:
        """Check timeout and intermission. Returns rankings if a game ended."""
        rankings = self.check_timeout()
        if self.intermission_until and time.time() >= self.intermission_until and self.winner:
            self.start_new_game()  # calls _notify
            return rankings
        if rankings is not None:
            self._notify()
        return rankings

    def begin_game(self) -> None:
        """Called when enough players join to start."""
        self.serve_new_word()
        self._notify()

    def notify_or_begin(self) -> None:
        """Notify SSE subscribers, or start the game if this is the second player."""
        if len(self.sessions) == 2 and not self.current_word:
            self.begin_game()
        else:
            self._notify()

    def set_draft(self, text: str) -> None:
        self.draft_text = text
        self.last_activity = time.time()

    def add_chat(self, entry: dict[str, str]) -> None:
        self.chat.append(entry)
        self.last_activity = time.time()
        self._notify()

    def toggle_lock(self) -> None:
        self.locked = not self.locked
        self._notify()

    def _apply_to_session(self, sess: Session, result: GuessResult, is_solo: bool) -> None:
        """Update session stats and set last_feedback from a GuessResult."""
        if result.correct:
            sess.words_correct += 1
            if is_solo:
                sess.streak += 1
            if sess.account_username:
                t = result.tier
                if t in DIFFICULTIES and (
                    not sess.highest_tier
                    or DIFFICULTIES.index(t) > DIFFICULTIES.index(sess.highest_tier)
                ):
                    sess.highest_tier = t
            body = f"{result.wpm} WPM."
            if result.homophone:
                body = f'Accepted as "{result.homophone}". {body}'
            if not is_solo:
                body += " You stay in."
            sess.last_feedback = feedback("Correct", body, "success")
        elif result.skipped:
            if is_solo:
                sess.streak = 0
                sess.last_feedback = feedback("Skipped", f"Answer: {result.word}.")
            else:
                sess.last_feedback = feedback("Eliminated", f"Skipped. Answer: {result.word}.")
        elif is_solo:
            sess.streak = 0
            sess.last_feedback = feedback(
                "Incorrect",
                f"Answer: {result.word}. {result.wpm} WPM.",
            )
        else:
            sess.last_feedback = feedback("Eliminated", f"Answer: {result.word}.")

    def submit_guess(
        self,
        sess: Session,
        guess_text: str,
    ) -> tuple[GuessResult | None, list[Ranking] | None]:
        """Evaluate a guess and update game state. Returns (result, rankings_if_game_ended)."""
        word_data = self.current_word
        if not word_data:
            return None, None

        elapsed = time.time() - self.word_served_at
        is_solo = self.visibility == "solo"

        if self.turn_deadline > 0 and time.time() > self.turn_deadline:
            guess_text = ""

        sess.words_attempted += 1
        skipped = not guess_text

        if skipped:
            correct, homophone, wpm = False, None, 0.0
        else:
            correct, homophone = evaluate_guess(guess_text, word_data)
            wpm = round(compute_wpm(guess_text, elapsed), 1)

        result = GuessResult(
            correct=correct,
            skipped=skipped,
            wpm=wpm,
            word=word_data["word"],
            tier=word_data["tier"],
            homophone=homophone,
        )

        self._apply_to_session(sess, result, is_solo)

        rankings = None
        if is_solo:
            self.serve_new_word(streak=sess.streak)
        elif skipped or not correct:
            self.eliminated.add(sess.id)
            rankings = self.advance_turn(eliminated=True)
        else:
            rankings = self.advance_turn(eliminated=False)

        self._notify()
        return result, rankings

    def forfeit(self, sid: str) -> list[Ranking] | None:
        """Remove a player who forfeits. Returns rankings if the game ended."""
        if sid not in self.sessions:
            return None
        prev_active = active_session_id(self)
        was_active = prev_active == sid
        self.eliminated.add(sid)
        self.sessions.remove(sid)
        alive = alive_sessions(self)
        rankings = None
        if len(alive) <= 1 and self.current_word and not self.winner:
            rankings = self.finish_game()
        elif was_active and alive:
            # Forfeiter held the turn — wrap index and serve a fresh word
            self.turn_index = self.turn_index % len(alive)
            self.draft_text = ""
            self.serve_new_word()
        elif alive and prev_active and prev_active in alive:
            # Forfeiter was waiting — keep the same active player
            self.turn_index = alive.index(prev_active)
        self.last_activity = time.time()
        self._notify()
        return rankings


# ── Pure helpers ──


def feedback(title: str, body: str, type: str = "error") -> Feedback:
    return Feedback(title=title, body=body, type=type)


def make_session_id() -> str:
    return secrets.token_urlsafe(16)


_NAME_RE = re.compile(r"[^A-Za-z0-9 '_-]")


def clean_name(raw: str, fallback: str = "Player") -> str:
    return _NAME_RE.sub("", raw).strip()[:24] or fallback


def validate_difficulty(raw: str) -> str:
    if raw in DIFFICULTIES or raw == "randomizer":
        return raw
    return DIFFICULTIES[0]


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


def evaluate_guess(guess: str, word_entry: WordEntry) -> tuple[bool, str | None]:
    """Returns (correct, matched_homophone_or_None)."""
    target = word_entry["word"].lower()
    g = guess.strip().lower()
    if g == target:
        return True, None
    for h in word_entry.get("homophones", []):
        if g == h.lower():
            return True, h
    return False, None


def update_elo(players: list[dict[str, Any]], k: float = 32.0) -> None:
    n = len(players)
    if n < 2:
        return
    norm = n * (n - 1) / 2

    def _exp(elo: float) -> float:
        return math.exp(0.01 * min(elo, 5000))

    denom = sum(_exp(p["elo"]) for p in players)
    for p in players:
        p["elo"] += k * ((n - p["rank"]) / norm - _exp(p["elo"]) / denom)


# ── GameState (pure — no asyncio, no IO) ──


@dataclass
class GameState:
    sessions: dict[str, Session] = field(default_factory=dict)
    rooms: dict[str, Room] = field(default_factory=dict)
    rate_buckets: dict[str, dict[str, list[float]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(list)),
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

    def purge_stale(self) -> tuple[list[str], list[str]]:
        """Remove stale rooms/sessions/rate buckets. Returns (purged_room_codes, purged_sids)."""
        cutoff = time.time() - STALE_MINUTES * 60
        stale_rooms = [c for c, r in self.rooms.items() if r.last_activity < cutoff]
        purged_sids: list[str] = []
        for c in stale_rooms:
            for sid in self.rooms[c].sessions:
                self.sessions.pop(sid, None)
                purged_sids.append(sid)
            del self.rooms[c]
        stale_sessions = [
            s
            for s, sess in self.sessions.items()
            if (sess.room_code and sess.room_code not in self.rooms) or sess.last_activity < cutoff
        ]
        for s in stale_sessions:
            self.sessions.pop(s, None)
        stale_ips = [
            ip
            for ip, actions in self.rate_buckets.items()
            if all(not ts for ts in actions.values())
        ]
        for ip in stale_ips:
            del self.rate_buckets[ip]
        return stale_rooms, purged_sids

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

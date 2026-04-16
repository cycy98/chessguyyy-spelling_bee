"""Spelling Bee — game engine (pure logic, no IO)."""

from __future__ import annotations

import json
import math
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast

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
NETWORK_GRACE = 1.0  # seconds of submit-POST cushion on the deadline - absorbs round-trip latency after visible timer expires
MAX_CHARS_PER_SEC = 12  # physics floor for typing speed — bounds client-reported typing_ms
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


@dataclass
class Catalog:
    tier_colors: dict[str, str]
    words: dict[str, dict[str, dict[str, Any]]]
    audio_durations: dict[str, float]
    difficulties: list[str] = field(init=False, repr=False)
    total_words: int = field(init=False, repr=False)
    all_words: dict[str, dict[str, Any]] = field(init=False, repr=False)
    _word_keys: dict[str, list[str]] = field(init=False, repr=False)
    _all_word_keys: list[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.difficulties = list(self.words)
        self.total_words = sum(len(v) for v in self.words.values())
        self.all_words = {
            w: {**wdata, "tier": tier}
            for tier, wdict in self.words.items()
            for w, wdata in wdict.items()
        }
        self._word_keys = {d: list(ws) for d, ws in self.words.items()}
        self._all_word_keys = list(self.all_words)

    @classmethod
    def load(cls, root: Path) -> Catalog:
        with (root / "wordlist.json").open() as f:
            raw: dict[str, Any] = json.load(f)
        return cls(
            tier_colors=raw["info"]["color"],
            words={k: v for k, v in raw.items() if k != "info"},
            audio_durations=_load_audio_durations(root),
        )

    def pick_word(self, difficulty: str) -> WordEntry:
        if difficulty == "randomizer":
            word_str = secrets.choice(self._all_word_keys)
            return cast("WordEntry", {"word": word_str, **self.all_words[word_str]})
        keys = self._word_keys.get(difficulty, self._word_keys[self.difficulties[0]])
        pool = self.words.get(difficulty, self.words[self.difficulties[0]])
        word_str = secrets.choice(keys)
        return cast("WordEntry", {"word": word_str, **pool[word_str], "tier": difficulty})

    def has_audio(self, word: str) -> bool:
        return word.lower() in self.audio_durations

    def validate_difficulty(self, raw: str) -> str:
        if raw in self.difficulties or raw == "randomizer":
            return raw
        return self.difficulties[0]

    def template_ctx(self) -> dict[str, Any]:
        return {
            "difficulties": self.difficulties,
            "words": self.words,
            "total_words": self.total_words,
        }


def _load_audio_durations(root: Path) -> dict[str, float]:
    audios_dir = root / "audios"
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
    word_audio_duration: float = 0.0
    # Injected at construction; defaults to empty/None so Room is usable in tests.
    sessions_map: dict[str, Session] = field(default_factory=dict, repr=False)
    catalog: Catalog | None = field(default=None, repr=False)

    # --- State-transition methods ---

    def serve_new_word(self, streak: int = 0) -> None:
        assert self.catalog is not None, "Room.catalog must be injected before serving words"
        word_data = self.catalog.pick_word(self.difficulty)
        self.current_word = word_data
        self.word_served_at = time.time()
        self.draft_text = ""
        word_str = word_data["word"]
        is_solo = self.visibility == "solo"
        tl = compute_time_limit(word_str, streak=streak, multiplayer=not is_solo)
        self.turn_time_limit = tl
        self.word_audio_duration = self.catalog.audio_durations.get(word_str.lower(), 0.0)
        self.turn_deadline = time.time() + tl + self.word_audio_duration + NETWORK_GRACE

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
        sess = self.sessions_map.get(active_sid)
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
        winner_sess = self.sessions_map.get(winner_sid) if winner_sid else None
        self.winner = winner_sess.player_name if winner_sess else "Nobody"
        self.turn_deadline = 0
        self.draft_text = ""

        rankings: list[Ranking] = []
        elim_order = [s for s in self.sessions if s in self.eliminated]
        for rank_idx, sid in enumerate(reversed(elim_order)):
            s = self.sessions_map.get(sid)
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
            s = self.sessions_map.get(sid)
            if s:
                s.last_feedback = None
        self.serve_new_word()
        self.last_activity = time.time()

    def tick(self) -> list[Ranking] | None:
        """Check timeout and intermission. Returns rankings if a game ended."""
        rankings = self.check_timeout()
        if self.intermission_until and time.time() >= self.intermission_until and self.winner:
            self.start_new_game()
        return rankings

    def begin_if_ready(self) -> bool:
        """Start the game if this is the second player. Returns True if game started."""
        if len(self.sessions) == 2 and not self.current_word:
            self.serve_new_word()
            return True
        return False

    def set_draft(self, text: str) -> None:
        self.draft_text = text
        self.last_activity = time.time()

    def add_chat(self, entry: dict[str, str]) -> None:
        self.chat.append(entry)
        self.last_activity = time.time()

    def toggle_lock(self) -> None:
        self.locked = not self.locked

    def _apply_to_session(self, sess: Session, result: GuessResult, is_solo: bool) -> None:
        """Update session stats and set last_feedback from a GuessResult."""
        if result.correct:
            sess.words_correct += 1
            if is_solo:
                sess.streak += 1
            if sess.account_username and self.catalog:
                t = result.tier
                diffs = self.catalog.difficulties
                if t in diffs and (
                    not sess.highest_tier or diffs.index(t) > diffs.index(sess.highest_tier)
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
        typing_ms: int | None = None,
    ) -> tuple[GuessResult | None, list[Ranking] | None]:
        """Evaluate a guess and update game state. Returns (result, rankings_if_game_ended)."""
        word_data = self.current_word
        if not word_data:
            return None, None

        elapsed = typing_window_s(
            typing_ms,
            guess_text,
            self.word_served_at,
            self.word_audio_duration,
        )
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
        return rankings

    def player_status(self, sid: str) -> tuple[str, str]:
        if self.winner:
            for mr in self.last_match_results:
                if mr["sid"] == sid:
                    return f"Rank {mr['rank']}", "winner" if mr["rank"] == 1 else "eliminated"
        elif sid in self.eliminated:
            return "Eliminated", "eliminated"
        elif sid == active_session_id(self):
            return "Spelling", "spelling"
        return "Waiting", "waiting"


# ── Pure helpers ──


def feedback(title: str, body: str, type: str = "error") -> Feedback:
    return Feedback(title=title, body=body, type=type)


def make_session_id() -> str:
    return secrets.token_urlsafe(16)


_NAME_RE = re.compile(r"[^A-Za-z0-9 '_-]")


def clean_name(raw: str, fallback: str = "Player") -> str:
    return _NAME_RE.sub("", raw).strip()[:24] or fallback


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


def typing_window_s(
    typing_ms: int | None,
    guess: str,
    served_at: float,
    audio_duration: float,
) -> float:
    """Typing window in seconds. Trusts client-reported ms above a physics floor;
    falls back to server elapsed (minus audio) when the client doesn't report."""
    floor = max(len(guess), 1) / MAX_CHARS_PER_SEC
    if typing_ms is not None:
        return max(typing_ms / 1000, floor)
    return max(time.time() - served_at - audio_duration, floor)


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

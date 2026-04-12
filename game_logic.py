from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
from pathlib import Path
import random
import re
import secrets
import string
import time
from threading import Lock
from typing import Any
import unicodedata
from urllib.parse import quote

from elo import elo_last_man_standing


@dataclass(frozen=True)
class WordEntry:
    word: str
    difficulty: str
    part_of_speech: str
    definition: str
    homophones: list[str]


class WordRepository:
    def __init__(self, catalog: dict[str, Any], audio_dir: Path | str = "audios") -> None:
        self._catalog = catalog
        self._difficulties = [name for name in catalog.keys() if name != "info"]
        self._colors = catalog.get("info", {}).get("color", {})
        self._randomizer_name = "randomizer"
        self._audio_dir = Path(audio_dir)
        self._audio_index = self._build_audio_index(self._audio_dir)

    @property
    def difficulties(self) -> list[str]:
        return [*self._difficulties, self._randomizer_name]

    @property
    def colors(self) -> dict[str, str]:
        return dict(self._colors)

    def difficulty_summary(self) -> list[dict[str, Any]]:
        summary = [
            {
                "name": difficulty,
                "word_count": len(self._catalog[difficulty]),
                "color": self._colors.get(difficulty, "#cccccc"),
            }
            for difficulty in self._difficulties
        ]
        summary.append(
            {
                "name": self._randomizer_name,
                "word_count": self.total_words(),
                "color": "#d4d4d8",
            }
        )
        return summary

    def total_words(self) -> int:
        return sum(len(self._catalog[difficulty]) for difficulty in self._difficulties)

    def _slugify(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
        cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-")
        return cleaned.casefold()

    def _build_audio_index(self, audio_dir: Path) -> dict[str, str]:
        index: dict[str, str] = {}
        if not audio_dir.exists():
            return index

        for audio_file in audio_dir.glob("*.mp3"):
            if not audio_file.is_file():
                continue
            stem = audio_file.stem
            index.setdefault(stem.casefold(), audio_file.name)
            slug = self._slugify(stem)
            if slug:
                index.setdefault(slug, audio_file.name)
        return index

    def audio_url_for_word(self, word: str) -> str | None:
        if not word:
            return None

        def find_match(candidate: str) -> str | None:
            direct = self._audio_index.get(candidate.casefold())
            if direct is not None:
                return direct
            slug = self._slugify(candidate)
            if slug:
                return self._audio_index.get(slug)
            return None

        def plural_candidates(base: str) -> list[str]:
            variants: list[str] = []
            lower = base.casefold()
            if lower.endswith("y") and len(base) > 1 and lower[-2] not in "aeiou":
                variants.append(f"{base[:-1]}ies")
            if lower.endswith("ity"):
                variants.append(f"{base[:-3]}ities")
            if lower.endswith(("s", "x", "z", "ch", "sh")):
                variants.append(f"{base}es")
            if lower.endswith("us"):
                variants.append(f"{base[:-2]}i")
            if lower.endswith("is"):
                variants.append(f"{base[:-2]}es")
            variants.append(f"{base}s")
            return variants

        for candidate in [word, *plural_candidates(word)]:
            matched_name = find_match(candidate)
            if matched_name is not None:
                return f"/audios/{quote(matched_name)}"

        return None

    def random_word(self, difficulty: str, rng: random.Random) -> WordEntry:
        if difficulty == self._randomizer_name:
            chosen_difficulty = rng.choice(self._difficulties)
            return self.random_word(chosen_difficulty, rng)

        if difficulty not in self._catalog or difficulty == "info":
            raise KeyError(f"Unknown difficulty: {difficulty}")

        word, payload = rng.choice(list(self._catalog[difficulty].items()))
        return WordEntry(
            word=word,
            difficulty=difficulty,
            part_of_speech=payload.get("part_of_speech", "unknown"),
            definition=payload.get("definition", "No definition available."),
            homophones=list(payload.get("homophones", [])),
        )


class AccountStore:
    def __init__(self, path: Path | str, default_elo: float = 1000.0) -> None:
        self.path = Path(path)
        self.default_elo = default_elo
        self._lock = Lock()
        self._username_pattern = re.compile(r"^[A-Za-z0-9_]{3,24}$")

    def _read_all(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}

        with self.path.open("r", encoding="utf-8") as accounts_file:
            return json.load(accounts_file)

    def _write_all(self, payload: dict[str, Any]) -> None:
        with self.path.open("w", encoding="utf-8") as accounts_file:
            json.dump(payload, accounts_file, indent=2)

    def _normalize_account(self, account: dict[str, Any]) -> dict[str, Any]:
        account.setdefault("elo", self.default_elo)
        account.setdefault("games_played", 0)
        account.setdefault("wins", 0)
        account.setdefault("words_attempted", 0)
        account.setdefault("correct_words", 0)
        account.setdefault("highest_wpm", 0)
        account.setdefault("best_wpm_word", "")
        account.setdefault("password_hash", "")
        account.setdefault("password_salt", "")
        account.setdefault("discord_id", "")
        account.setdefault("discord_username", "")
        highest_wpm = int(account.get("highest_wpm", 0))
        return account

    def _validate_username(self, username: str) -> str:
        cleaned_name = username.strip()
        if not self._username_pattern.fullmatch(cleaned_name):
            raise ValueError("Username must be 3-24 characters using letters, numbers, or underscores.")
        return cleaned_name

    def _hash_password(self, password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            120000,
        ).hex()

    def _public_account_payload(self, username: str, account: dict[str, Any]) -> dict[str, Any]:
        games_played = int(account.get("games_played", 0))
        wins = int(account.get("wins", 0))
        words_attempted = int(account.get("words_attempted", 0))
        correct_words = int(account.get("correct_words", 0))
        return {
            "username": username,
            "elo": float(account.get("elo", self.default_elo)),
            "games_played": games_played,
            "wins": wins,
            "words_attempted": words_attempted,
            "correct_words": correct_words,
            "win_rate": round((wins / games_played) * 100, 2) if games_played > 0 else 0.0,
            "correct_rate": round((correct_words / words_attempted) * 100, 2) if words_attempted > 0 else 0.0,
            "highest_wpm": int(account.get("highest_wpm", 0)),
            "best_wpm_word": str(account.get("best_wpm_word", "")),
            "discord_linked": bool(account.get("discord_id")),
            "has_password": bool(account.get("password_hash")),
        }

    def register_account(self, username: str, password: str) -> dict[str, Any]:
        cleaned_name = self._validate_username(username)
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters long.")

        with self._lock:
            accounts = self._read_all()
            account = self._normalize_account(accounts.setdefault(cleaned_name, {}))
            if account.get("password_hash") or account.get("discord_id"):
                raise ValueError("That username already has an account.")

            salt = secrets.token_hex(16)
            account["password_salt"] = salt
            account["password_hash"] = self._hash_password(password, salt)
            accounts[cleaned_name] = account
            self._write_all(accounts)
            return self._public_account_payload(cleaned_name, account)

    def authenticate_account(self, username: str, password: str) -> dict[str, Any]:
        cleaned_name = self._validate_username(username)
        with self._lock:
            accounts = self._read_all()
            account = accounts.get(cleaned_name)
            if account is None:
                raise ValueError("Unknown username or password.")

            normalized = self._normalize_account(account)
            stored_hash = normalized.get("password_hash", "")
            stored_salt = normalized.get("password_salt", "")
            if not stored_hash or not stored_salt:
                raise ValueError("Unknown username or password.")

            computed_hash = self._hash_password(password, stored_salt)
            if not hmac.compare_digest(stored_hash, computed_hash):
                raise ValueError("Unknown username or password.")

            accounts[cleaned_name] = normalized
            self._write_all(accounts)
            return self._public_account_payload(cleaned_name, normalized)

    def account_by_username(self, username: str) -> dict[str, Any]:
        cleaned_name = self._validate_username(username)
        with self._lock:
            accounts = self._read_all()
            account = accounts.get(cleaned_name)
            if account is None:
                raise KeyError("Unknown account.")

            normalized = self._normalize_account(account)
            accounts[cleaned_name] = normalized
            self._write_all(accounts)
            return self._public_account_payload(cleaned_name, normalized)

    def leaderboard(self) -> list[dict[str, Any]]:
        with self._lock:
            accounts = self._read_all()
            entries: list[dict[str, Any]] = []

            for username, account in accounts.items():
                normalized = self._normalize_account(account)
                accounts[username] = normalized
                entries.append(self._public_account_payload(username, normalized))

            self._write_all(accounts)

        return sorted(
            entries,
            key=lambda item: (
                -float(item["elo"]),
                -int(item["wins"]),
                -int(item["correct_words"]),
                -int(item["highest_wpm"]),
                str(item["username"]).casefold(),
            ),
        )

    def authenticate_discord_user(self, discord_id: str, preferred_username: str) -> dict[str, Any]:
        if not discord_id:
            raise ValueError("Discord account is missing an id.")

        base_name = re.sub(r"[^A-Za-z0-9_]+", "_", preferred_username.strip())[:24].strip("_") or "DiscordUser"
        base_name = base_name[:24]

        with self._lock:
            accounts = self._read_all()
            existing_username = next(
                (
                    username
                    for username, account in accounts.items()
                    if str(account.get("discord_id", "")) == str(discord_id)
                ),
                None,
            )

            if existing_username is None:
                candidate = base_name
                suffix = 1
                while candidate in accounts and str(accounts[candidate].get("discord_id", "")) != str(discord_id):
                    suffix += 1
                    suffix_text = str(suffix)
                    candidate = f"{base_name[: max(0, 24 - len(suffix_text))]}{suffix_text}"
                existing_username = candidate

            account = self._normalize_account(accounts.setdefault(existing_username, {}))
            account["discord_id"] = str(discord_id)
            account["discord_username"] = preferred_username.strip()
            accounts[existing_username] = account
            self._write_all(accounts)
            return self._public_account_payload(existing_username, account)

    def username_owner(self, username: str) -> str | None:
        cleaned_name = username.strip()
        if not cleaned_name:
            return None
        with self._lock:
            accounts = self._read_all()
            if cleaned_name not in accounts:
                return None
            account = self._normalize_account(accounts[cleaned_name])
            accounts[cleaned_name] = account
            self._write_all(accounts)
            if account.get("password_hash") or account.get("discord_id"):
                return cleaned_name
            return None

    def apply_last_man_standing(self, placements: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        with self._lock:
            accounts = self._read_all()
            elo_players: list[dict[str, Any]] = []

            for placement in placements:
                player_name = placement["name"].strip() or "Player One"
                account = self._normalize_account(accounts.setdefault(player_name, {}))
                accounts[player_name] = account
                elo_players.append(
                    {
                        "name": player_name,
                        "elo": float(account.get("elo", self.default_elo)),
                        "rank": int(placement["rank"]),
                    }
                )

            updated_players = elo_last_man_standing(elo_players) if len(elo_players) > 1 else elo_players
            updated_accounts: dict[str, dict[str, Any]] = {}

            for player in updated_players:
                account = accounts[player["name"]]
                account["elo"] = round(float(player["elo"]), 2)
                account["games_played"] = int(account.get("games_played", 0)) + 1
                if int(player["rank"]) == 1:
                    account["wins"] = int(account.get("wins", 0)) + 1
                updated_accounts[player["name"]] = dict(account)

            self._write_all(accounts)
            return updated_accounts

    def record_word_result(self, player_name: str, word: str, was_correct: bool, wpm: int | float) -> dict[str, Any]:
        cleaned_name = player_name.strip() or "Player One"
        safe_wpm = max(0, int(round(float(wpm))))
        with self._lock:
            accounts = self._read_all()
            account = accounts.get(cleaned_name)
            if account is None:
                raise KeyError("Unknown account.")
            account = self._normalize_account(account)
            accounts[cleaned_name] = account
            account["words_attempted"] = int(account.get("words_attempted", 0)) + 1
            if was_correct:
                account["correct_words"] = int(account.get("correct_words", 0)) + 1
            current_highest_wpm = int(account.get("highest_wpm", 0))
            if safe_wpm >= current_highest_wpm:
                account["highest_wpm"] = safe_wpm
                account["best_wpm_word"] = word
            self._write_all(accounts)
            return dict(account)

    def public_account(self, username: str) -> dict[str, Any]:
        cleaned_name = self._validate_username(username)
        with self._lock:
            accounts = self._read_all()
            account = accounts.get(cleaned_name)
            if account is None:
                raise KeyError("Unknown account.")
            account = self._normalize_account(account)
            accounts[cleaned_name] = account
            self._write_all(accounts)
            return self._public_account_payload(cleaned_name, account)


@dataclass
class Session:
    session_id: str
    player_name: str
    difficulty: str
    room_code: str | None = None
    current_word: WordEntry | None = None
    tracked_account_username: str | None = None


@dataclass
class Room:
    room_code: str
    difficulty: str
    session_ids: set[str]
    visibility: str = "private"
    turn_order: list[str] = field(default_factory=list)
    active_turn_index: int = 0
    eliminated_session_ids: set[str] = field(default_factory=set)
    draft_text: str = ""
    winner_session_id: str | None = None
    elimination_order: list[str] = field(default_factory=list)
    intermission_until: float | None = None
    turn_deadline: float | None = None
    game_number: int = 1
    last_match_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    chat_messages: list[dict[str, Any]] = field(default_factory=list)
    next_chat_message_id: int = 1


class GameService:
    INTERMISSION_SECONDS = 15
    MAX_ROOM_SIZE = 15

    @staticmethod
    def required_wpm_for_streak(current_streak: int = 0) -> float:
        safe_streak = max(0, int(current_streak))
        return 5 * (safe_streak ** 0.8) + 10

    @classmethod
    def time_limit_seconds_for_word(cls, word: WordEntry, current_streak: int = 0) -> float:
        typed_units = max(len(word.word.replace(" ", "")) / 5, 0.2)
        return max(3.0, (typed_units / cls.required_wpm_for_streak(current_streak)) * 60)

    def __init__(
        self,
        repository: WordRepository,
        account_store: AccountStore | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.repository = repository
        self.account_store = account_store
        self.rng = rng or random.Random()
        self.sessions: dict[str, Session] = {}
        self.rooms: dict[str, Room] = {}

    def create_round_payload(self, session: Session) -> dict[str, Any]:
        if session.current_word is None:
            raise RuntimeError("Session is missing a current word.")

        word = session.current_word
        return {
            "player_name": session.player_name,
            "session_id": session.session_id,
            "difficulty": word.difficulty,
            "difficulty_color": self.repository.colors.get(word.difficulty, "#cccccc"),
            "session_difficulty": session.difficulty,
            "account": self.account_store.public_account(session.tracked_account_username) if self.account_store is not None and session.tracked_account_username else None,
            "part_of_speech": word.part_of_speech,
            "definition": word.definition,
            "homophones": word.homophones,
            "pronunciation": word.word,
            "audio_url": self.repository.audio_url_for_word(word.word),
            "room_code": session.room_code,
        }

    def next_word(self, session: Session) -> dict[str, Any]:
        session.current_word = self.repository.random_word(session.difficulty, self.rng)
        return self.create_round_payload(session)

    def _set_room_turn_deadline(self, room: Room) -> None:
        active_session = self._active_room_session(room)
        if active_session is None or active_session.current_word is None or room.intermission_until is not None:
            room.turn_deadline = None
            return

        room.turn_deadline = time.time() + self.time_limit_seconds_for_word(active_session.current_word)

    def _expire_room_turn_if_needed(self, room: Room) -> None:
        if room.intermission_until is not None or room.turn_deadline is None:
            return

        while room.turn_deadline is not None and time.time() >= room.turn_deadline:
            active_session = self._active_room_session(room)
            if active_session is None:
                room.turn_deadline = None
                return

            self._record_elimination(room, active_session.session_id)
            room.draft_text = ""
            self._advance_room_turn(room)
            if room.intermission_until is not None or room.winner_session_id is not None:
                return

    def _active_room_session(self, room: Room) -> Session | None:
        if room.winner_session_id is not None:
            return self.sessions.get(room.winner_session_id)

        if not room.turn_order:
            return None

        active_session_id = room.turn_order[room.active_turn_index]
        return self.sessions.get(active_session_id)

    def _alive_room_session_ids(self, room: Room) -> list[str]:
        return [
            session_id
            for session_id in room.turn_order
            if session_id in self.sessions and session_id not in room.eliminated_session_ids
        ]

    def _record_elimination(self, room: Room, session_id: str) -> None:
        room.eliminated_session_ids.add(session_id)
        if session_id not in room.elimination_order:
            room.elimination_order.append(session_id)

    def _start_room_game(self, room: Room) -> None:
        room.eliminated_session_ids.clear()
        room.elimination_order.clear()
        room.winner_session_id = None
        room.draft_text = ""
        room.intermission_until = None
        room.turn_deadline = None
        room.last_match_results = {}

        for session_id in room.turn_order:
            session = self.sessions.get(session_id)
            if session is not None:
                self.next_word(session)

        if room.turn_order:
            room.active_turn_index %= len(room.turn_order)
            self._set_room_turn_deadline(room)

    def _finalize_room_game(self, room: Room) -> None:
        if room.winner_session_id is None:
            return

        room.turn_deadline = None

        ordered_session_ids = [*room.elimination_order, room.winner_session_id]
        total_players = len(ordered_session_ids)
        if total_players < 2:
            room.intermission_until = time.time() + self.INTERMISSION_SECONDS
            return

        placements = []
        for index, session_id in enumerate(ordered_session_ids):
            session = self.sessions.get(session_id)
            if session is None:
                continue
            rank = total_players - index
            placements.append(
                {
                    "session_id": session_id,
                    "name": session.player_name,
                    "account_username": session.tracked_account_username,
                    "rank": rank,
                }
            )

        room.last_match_results = {
            item["session_id"]: {
                "rank": item["rank"],
            }
            for item in placements
        }

        tracked_placements = [
            {
                "session_id": item["session_id"],
                "name": item["account_username"],
                "rank": item["rank"],
            }
            for item in placements
            if item["account_username"]
        ]
        if self.account_store is not None and tracked_placements:
            updated_accounts = self.account_store.apply_last_man_standing(tracked_placements)
            for item in tracked_placements:
                room.last_match_results[item["session_id"]].update(updated_accounts.get(item["name"], {}))

        room.intermission_until = time.time() + self.INTERMISSION_SECONDS

    def _ensure_room_ready(self, room: Room) -> None:
        if room.intermission_until is None:
            return

        if time.time() < room.intermission_until:
            return

        room.game_number += 1
        if room.winner_session_id is not None and room.winner_session_id in room.turn_order:
            room.active_turn_index = room.turn_order.index(room.winner_session_id)
        self._start_room_game(room)
        self._expire_room_turn_if_needed(room)

    def _advance_room_turn(self, room: Room) -> None:
        alive_session_ids = self._alive_room_session_ids(room)
        if len(alive_session_ids) <= 1:
            room.winner_session_id = alive_session_ids[0] if alive_session_ids else None
            room.draft_text = ""
            room.turn_deadline = None
            self._finalize_room_game(room)
            return

        total_players = len(room.turn_order)
        for _ in range(total_players):
            room.active_turn_index = (room.active_turn_index + 1) % total_players
            active_session_id = room.turn_order[room.active_turn_index]
            if active_session_id not in room.eliminated_session_ids and active_session_id in self.sessions:
                room.draft_text = ""
                self._set_room_turn_deadline(room)
                return

    def _room_payload(self, room: Room) -> dict[str, Any]:
        self._ensure_room_ready(room)
        self._expire_room_turn_if_needed(room)
        participants = [
            self.sessions[session_id]
            for session_id in room.turn_order
            if session_id in self.sessions
        ]
        active_session = self._active_room_session(room)
        active_session_id = active_session.session_id if active_session is not None else None
        leaderboard = [
            {
                "player_name": session.player_name,
                "session_id": session.session_id,
                "is_active": session.session_id == active_session_id and room.winner_session_id is None,
                "is_eliminated": session.session_id in room.eliminated_session_ids,
            }
            for session in participants
        ]

        winner_name = None
        if room.winner_session_id and room.winner_session_id in self.sessions:
            winner_name = self.sessions[room.winner_session_id].player_name

        intermission_remaining = 0
        if room.intermission_until is not None:
            intermission_remaining = max(0, int(room.intermission_until - time.time() + 0.999))

        turn_seconds_remaining = 0.0
        turn_time_limit_seconds = 0.0
        if room.turn_deadline is not None and active_session is not None and active_session.current_word is not None:
            turn_time_limit_seconds = self.time_limit_seconds_for_word(active_session.current_word)
            turn_seconds_remaining = max(0.0, room.turn_deadline - time.time())

        return {
            "room_code": room.room_code,
            "visibility": room.visibility,
            "difficulty": room.difficulty,
            "difficulty_color": self.repository.colors.get(room.difficulty, "#cccccc"),
            "max_players": self.MAX_ROOM_SIZE,
            "player_count": len(leaderboard),
            "active_session_id": active_session_id,
            "active_player_name": active_session.player_name if active_session is not None else None,
            "active_round": self.create_round_payload(active_session) if active_session is not None and room.intermission_until is None else None,
            "draft_text": room.draft_text,
            "winner_session_id": room.winner_session_id,
            "winner_name": winner_name,
            "game_number": room.game_number,
            "game_phase": "intermission" if room.intermission_until is not None else "active",
            "intermission_seconds_remaining": intermission_remaining,
            "turn_seconds_remaining": round(turn_seconds_remaining, 2),
            "turn_time_limit_seconds": round(turn_time_limit_seconds, 2),
            "last_match_results": room.last_match_results,
            "chat_messages": list(room.chat_messages),
            "leaderboard": leaderboard,
        }

    def _build_session(
        self,
        session_id: str,
        player_name: str,
        difficulty: str,
        room_code: str | None = None,
        tracked_account_username: str | None = None,
    ) -> Session:
        cleaned_name = player_name.strip() or "Player One"
        return Session(
            session_id=session_id,
            player_name=cleaned_name,
            difficulty=difficulty,
            room_code=room_code,
            tracked_account_username=tracked_account_username,
        )

    def create_session(
        self,
        session_id: str,
        player_name: str,
        difficulty: str,
        room_code: str | None = None,
        tracked_account_username: str | None = None,
    ) -> dict[str, Any]:
        session = self._build_session(session_id, player_name, difficulty, room_code, tracked_account_username)
        self.sessions[session_id] = session
        if room_code is not None:
            room = self.rooms[room_code]
            if len(room.turn_order) >= self.MAX_ROOM_SIZE:
                self.sessions.pop(session_id, None)
                raise RuntimeError("This lobby is full.")
            room.session_ids.add(session_id)
            room.turn_order.append(session_id)
        round_payload = self.next_word(session)
        if room_code is not None:
            room = self.rooms[room_code]
            if room.intermission_until is None and room.turn_deadline is None:
                self._set_room_turn_deadline(room)
        return round_payload

    def _generate_room_code(self) -> str:
        alphabet = string.ascii_uppercase + string.digits
        while True:
            room_code = "".join(self.rng.choice(alphabet) for _ in range(6))
            if room_code not in self.rooms:
                return room_code

    def create_room(
        self,
        session_id: str,
        player_name: str,
        difficulty: str,
        visibility: str = "private",
        tracked_account_username: str | None = None,
    ) -> dict[str, Any]:
        room_code = self._generate_room_code()
        self.rooms[room_code] = Room(
            room_code=room_code,
            difficulty=difficulty,
            session_ids=set(),
            visibility=visibility,
        )
        round_payload = self.create_session(
            session_id,
            player_name,
            difficulty,
            room_code=room_code,
            tracked_account_username=tracked_account_username,
        )
        return {
            "session_id": session_id,
            "round": round_payload,
            "room": self.get_room_state(room_code),
        }

    def join_public_room(
        self,
        session_id: str,
        player_name: str,
        difficulty: str,
        tracked_account_username: str | None = None,
    ) -> dict[str, Any]:
        public_room = next(
            (
                room
                for room in self.rooms.values()
                if room.visibility == "public" and room.difficulty == difficulty and len(room.turn_order) < self.MAX_ROOM_SIZE
            ),
            None,
        )

        if public_room is None:
            return self.create_room(
                session_id,
                player_name,
                difficulty,
                visibility="public",
                tracked_account_username=tracked_account_username,
            )

        round_payload = self.create_session(
            session_id=session_id,
            player_name=player_name,
            difficulty=public_room.difficulty,
            room_code=public_room.room_code,
            tracked_account_username=tracked_account_username,
        )
        return {
            "session_id": session_id,
            "round": round_payload,
            "room": self.get_room_state(public_room.room_code),
        }

    def list_public_rooms(self) -> list[dict[str, Any]]:
        public_rooms = []
        for room in self.rooms.values():
            if room.visibility != "public":
                continue
            payload = self._room_payload(room)
            public_rooms.append(
                {
                    "room_code": payload["room_code"],
                    "difficulty": payload["difficulty"],
                    "difficulty_color": payload["difficulty_color"],
                    "player_count": payload["player_count"],
                    "max_players": payload["max_players"],
                    "game_phase": payload["game_phase"],
                    "game_number": payload["game_number"],
                }
            )
        return sorted(public_rooms, key=lambda item: (item["difficulty"], item["room_code"]))

    def join_room(
        self,
        session_id: str,
        room_code: str,
        player_name: str,
        tracked_account_username: str | None = None,
    ) -> dict[str, Any]:
        normalized_code = room_code.strip().upper()
        room = self.rooms.get(normalized_code)
        if room is None:
            raise KeyError("Unknown room code.")

        round_payload = self.create_session(
            session_id=session_id,
            player_name=player_name,
            difficulty=room.difficulty,
            room_code=normalized_code,
            tracked_account_username=tracked_account_username,
        )
        return {
            "session_id": session_id,
            "round": round_payload,
            "room": self.get_room_state(normalized_code),
        }

    def get_room_state(self, room_code: str) -> dict[str, Any]:
        normalized_code = room_code.strip().upper()
        room = self.rooms.get(normalized_code)
        if room is None:
            raise KeyError("Unknown room code.")
        self._ensure_room_ready(room)
        return self._room_payload(room)

    def update_room_draft(self, session_id: str, draft_text: str) -> dict[str, Any]:
        session = self.sessions[session_id]
        if session.room_code is None:
            raise RuntimeError("This session is not in a room.")

        room = self.rooms[session.room_code]
        self._ensure_room_ready(room)
        self._expire_room_turn_if_needed(room)
        active_session = self._active_room_session(room)
        if active_session is None or active_session.session_id != session_id:
            raise RuntimeError("It is not your turn.")

        room.draft_text = draft_text
        return self._room_payload(room)

    def send_room_chat(self, session_id: str, message: str) -> dict[str, Any]:
        session = self.sessions[session_id]
        if session.room_code is None:
            raise RuntimeError("This session is not in a room.")

        cleaned_message = " ".join(message.strip().split())
        if not cleaned_message:
            raise RuntimeError("Chat message cannot be empty.")
        if len(cleaned_message) > 300:
            raise RuntimeError("Chat message is too long.")

        room = self.rooms[session.room_code]
        room.chat_messages.append(
            {
                "id": room.next_chat_message_id,
                "player_name": session.player_name,
                "message": cleaned_message,
            }
        )
        room.next_chat_message_id += 1
        room.chat_messages = room.chat_messages[-80:]
        return self._room_payload(room)

    def forfeit_session(self, session_id: str) -> dict[str, Any] | None:
        session = self.sessions.get(session_id)
        if session is None or session.room_code is None:
            return None

        room = self.rooms.get(session.room_code)
        if room is None:
            return None

        self._ensure_room_ready(room)
        was_active = bool(room.turn_order) and room.turn_order[room.active_turn_index] == session_id

        if room.intermission_until is None and session_id not in room.eliminated_session_ids:
            self._record_elimination(room, session_id)

        if session_id in room.turn_order:
            removed_index = room.turn_order.index(session_id)
            room.turn_order.pop(removed_index)
            room.session_ids.discard(session_id)
            if room.turn_order:
                if removed_index < room.active_turn_index:
                    room.active_turn_index -= 1
                elif removed_index == room.active_turn_index:
                    room.active_turn_index %= len(room.turn_order)
            else:
                room.active_turn_index = 0

        if not room.turn_order:
            room.winner_session_id = None
            room.intermission_until = None
            room.turn_deadline = None
            room.draft_text = ""
            return None

        if room.intermission_until is None:
            if was_active:
                alive_session_ids = self._alive_room_session_ids(room)
                if len(alive_session_ids) <= 1:
                    room.winner_session_id = alive_session_ids[0] if alive_session_ids else None
                    self._finalize_room_game(room)
                else:
                    room.draft_text = ""
                    self._set_room_turn_deadline(room)
            else:
                alive_session_ids = self._alive_room_session_ids(room)
                if len(alive_session_ids) <= 1:
                    room.winner_session_id = alive_session_ids[0] if alive_session_ids else None
                    self._finalize_room_game(room)
                else:
                    self._set_room_turn_deadline(room)

        return self._room_payload(room)

    def evaluate_guess(self, session_id: str, guess: str, reported_wpm: int | float = 0) -> dict[str, Any]:
        session = self.sessions[session_id]
        if session.current_word is None:
            raise RuntimeError("Session is missing a current word.")

        room = self.rooms.get(session.room_code) if session.room_code is not None else None
        if room is not None:
            self._ensure_room_ready(room)
            self._expire_room_turn_if_needed(room)
            if room.intermission_until is not None:
                raise RuntimeError("The room is in intermission.")
            active_session = self._active_room_session(room)
            if active_session is None or active_session.session_id != session_id:
                raise RuntimeError("It is not your turn.")

        answer = session.current_word.word
        normalized_guess = guess.strip().casefold()
        accepted_spellings = {answer.casefold(), *(item.casefold() for item in session.current_word.homophones)}
        correct = normalized_guess in accepted_spellings
        matched_spelling = next(
            (
                spelling
                for spelling in [answer, *session.current_word.homophones]
                if spelling.casefold() == normalized_guess
            ),
            answer,
        )

        result = {
            "correct": correct,
            "answer": answer,
            "matched_spelling": matched_spelling,
            "accepted_as_homophone": matched_spelling.casefold() != answer.casefold(),
        }
        if self.account_store is not None and session.tracked_account_username:
            result["account"] = self.account_store.record_word_result(
                session.tracked_account_username,
                answer,
                correct,
                reported_wpm,
            )

        if room is not None:
            if correct:
                self.next_word(session)
            else:
                self._record_elimination(room, session_id)

            room.draft_text = ""
            self._advance_room_turn(room)
            response = {"result": result, "round": self._room_payload(room)["active_round"], "room": self._room_payload(room)}
            return response

        round_payload = self.next_word(session)
        response = {"result": result, "round": round_payload}
        return response

    def skip_word(self, session_id: str) -> dict[str, Any]:
        session = self.sessions[session_id]
        if session.current_word is None:
            raise RuntimeError("Session is missing a current word.")

        room = self.rooms.get(session.room_code) if session.room_code is not None else None
        if room is not None:
            self._ensure_room_ready(room)
            self._expire_room_turn_if_needed(room)
            if room.intermission_until is not None:
                raise RuntimeError("The room is in intermission.")
            active_session = self._active_room_session(room)
            if active_session is None or active_session.session_id != session_id:
                raise RuntimeError("It is not your turn.")

        skipped_answer = session.current_word.word
        if room is not None:
            self._record_elimination(room, session_id)
            room.draft_text = ""
            self._advance_room_turn(room)
            room_payload = self._room_payload(room)
            response = {"skipped_answer": skipped_answer, "round": room_payload["active_round"], "room": room_payload}
            return response

        round_payload = self.next_word(session)
        response = {"skipped_answer": skipped_answer, "round": round_payload}
        return response

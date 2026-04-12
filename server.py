from __future__ import annotations

from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
import uuid

from game_logic import AccountStore, GameService, WordRepository


ROOT = Path(__file__).resolve().parent
WORDLIST_PATH = ROOT / "wordlist.json"
ACCOUNTS_PATH = ROOT / "accounts.json"
DISCORD_TOKEN_PATH = ROOT / "token"
DISCORD_CLIENT_ID_PATH = ROOT / "client_id"
DISCORD_CLIENT_SECRET_PATH = ROOT / "client_secret"


with WORDLIST_PATH.open("r", encoding="utf-8") as wordlist_file:
    WORD_CATALOG = json.load(wordlist_file)

REPOSITORY = WordRepository(WORD_CATALOG)
ACCOUNT_STORE = AccountStore(ACCOUNTS_PATH)
GAME_SERVICE = GameService(REPOSITORY, account_store=ACCOUNT_STORE)


def _read_discord_oauth_config() -> dict[str, str]:
    env_client_id = ""
    env_client_secret = ""
    file_client_id = ""
    file_client_secret = ""

    if DISCORD_CLIENT_ID_PATH.exists():
        file_client_id = DISCORD_CLIENT_ID_PATH.read_text(encoding="utf-8").strip()

    if DISCORD_CLIENT_SECRET_PATH.exists():
        file_client_secret = DISCORD_CLIENT_SECRET_PATH.read_text(encoding="utf-8").strip()

    if DISCORD_TOKEN_PATH.exists() and (not file_client_id or not file_client_secret):
        raw_value = DISCORD_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if raw_value.startswith("{"):
            try:
                parsed = json.loads(raw_value)
            except json.JSONDecodeError:
                parsed = {}
            file_client_id = str(parsed.get("client_id", "")).strip()
            file_client_secret = str(parsed.get("client_secret", "")).strip()

    return {
        "client_id": env_client_id or file_client_id,
        "client_secret": env_client_secret or file_client_secret,
    }


class AuthService:
    def __init__(self, account_store: AccountStore) -> None:
        self.account_store = account_store
        self.sessions: dict[str, str] = {}

    def issue_token(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        self.sessions[token] = username
        return token

    def register(self, username: str, password: str) -> dict[str, object]:
        account = self.account_store.register_account(username, password)
        token = self.issue_token(account["username"])
        return {"token": token, "account": account}

    def login(self, username: str, password: str) -> dict[str, object]:
        account = self.account_store.authenticate_account(username, password)
        token = self.issue_token(account["username"])
        return {"token": token, "account": account}

    def login_with_discord(self, discord_user: dict[str, object]) -> dict[str, object]:
        preferred_name = str(
            discord_user.get("global_name")
            or discord_user.get("username")
            or discord_user.get("id")
            or "DiscordUser"
        )
        account = self.account_store.authenticate_discord_user(
            str(discord_user.get("id", "")),
            preferred_name,
        )
        token = self.issue_token(account["username"])
        return {"token": token, "account": account}

    def account_for_token(self, token: str) -> dict[str, object]:
        username = self.sessions.get(token)
        if not username:
            raise KeyError("Unknown session.")
        return self.account_store.account_by_username(username)

    def logout(self, token: str) -> None:
        self.sessions.pop(token, None)


class DiscordOAuthService:
    AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
    TOKEN_URL = "https://discord.com/api/oauth2/token"
    USER_URL = "https://discord.com/api/users/@me"

    def __init__(self, auth_service: AuthService) -> None:
        self.auth_service = auth_service
        self.pending_states: dict[str, str] = {}

    def status(self, origin: str) -> dict[str, object]:
        config = _read_discord_oauth_config()
        enabled = bool(config["client_id"] and config["client_secret"])
        reason = ""
        if not enabled:
            reason = (
                "Discord login needs an OAuth client ID and client secret. "
                "The current token file does not contain those OAuth credentials."
            )
        return {
            "enabled": enabled,
            "reason": reason,
            "start_url": f"{origin}/api/auth/discord/start" if enabled else "",
        }

    def start_url(self, origin: str) -> str:
        config = _read_discord_oauth_config()
        client_id = config["client_id"]
        client_secret = config["client_secret"]
        if not client_id or not client_secret:
            raise RuntimeError(
                "Discord login needs an OAuth client ID and client secret. "
                "The current token file does not contain those OAuth credentials."
            )

        state = secrets.token_urlsafe(24)
        redirect_uri = f"{origin}/api/auth/discord/callback"
        self.pending_states[state] = redirect_uri
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "identify",
                "state": state,
                "prompt": "consent",
            }
        )
        return f"{self.AUTHORIZE_URL}?{query}"

    def complete(self, origin: str, code: str, state: str) -> dict[str, object]:
        redirect_uri = self.pending_states.pop(state, "")
        if not redirect_uri:
            raise RuntimeError("Discord login session expired. Please try again.")

        config = _read_discord_oauth_config()
        client_id = config["client_id"]
        client_secret = config["client_secret"]
        if not client_id or not client_secret:
            raise RuntimeError("Discord OAuth is not configured.")

        token_payload = urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            }
        ).encode("utf-8")
        token_request = Request(
            self.TOKEN_URL,
            data=token_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(token_request, timeout=15) as token_response:
            token_data = json.loads(token_response.read().decode("utf-8"))

        access_token = token_data.get("access_token", "")
        if not access_token:
            raise RuntimeError("Discord did not return an access token.")

        user_request = Request(
            self.USER_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            method="GET",
        )
        with urlopen(user_request, timeout=15) as user_response:
            discord_user = json.loads(user_response.read().decode("utf-8"))

        return self.auth_service.login_with_discord(discord_user)


AUTH_SERVICE = AuthService(ACCOUNT_STORE)
DISCORD_SERVICE = DiscordOAuthService(AUTH_SERVICE)


class ActivityHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        # Prevent stale frontend assets from being served after rapid local edits.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        origin = self._origin()

        if parsed.path == "/api/config":
            self._write_json(
                {
                    "difficulties": REPOSITORY.difficulty_summary(),
                    "total_words": REPOSITORY.total_words(),
                }
            )
            return

        if parsed.path == "/api/auth/me":
            token = self._bearer_token()
            if not token:
                self._write_json({"account": None})
                return

            try:
                account = AUTH_SERVICE.account_for_token(token)
            except KeyError:
                self._write_json({"account": None})
                return

            self._write_json({"account": account})
            return

        if parsed.path == "/api/leaderboard":
            self._write_json({"players": ACCOUNT_STORE.leaderboard()})
            return

        if parsed.path == "/api/account":
            username = parse_qs(parsed.query).get("username", [""])[0]
            if not username:
                account = self._optional_account()
                if account is None:
                    self._write_json({"error": "No account selected."}, status=HTTPStatus.BAD_REQUEST)
                    return
                self._write_json({"account": account})
                return

            try:
                account = ACCOUNT_STORE.public_account(username)
            except (KeyError, ValueError):
                self._write_json({"error": "Unknown account."}, status=HTTPStatus.NOT_FOUND)
                return

            self._write_json({"account": account})
            return

        if parsed.path == "/api/auth/discord/status":
            self._write_json(DISCORD_SERVICE.status(origin))
            return

        if parsed.path == "/api/auth/discord/start":
            try:
                start_url = DISCORD_SERVICE.start_url(origin)
            except RuntimeError as error:
                self._write_json({"error": str(error)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
                return

            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", start_url)
            self.end_headers()
            return

        if parsed.path == "/api/auth/discord/callback":
            query = parse_qs(parsed.query)
            code = query.get("code", [""])[0]
            state = query.get("state", [""])[0]
            error_message = query.get("error_description", query.get("error", [""]))[0]
            if error_message:
                self._write_oauth_popup("error", error_message)
                return

            try:
                payload = DISCORD_SERVICE.complete(origin, code, state)
            except Exception as error:
                self._write_oauth_popup("error", str(error))
                return

            self._write_oauth_popup("success", json.dumps(payload))
            return

        if parsed.path == "/api/public-rooms":
            self._write_json({"rooms": GAME_SERVICE.list_public_rooms()})
            return

        if parsed.path == "/api/room-state":
            room_code = parse_qs(parsed.query).get("room_code", [""])[0]
            try:
                room = GAME_SERVICE.get_room_state(room_code)
            except KeyError:
                self._write_json({"error": "Unknown room code."}, status=HTTPStatus.BAD_REQUEST)
                return

            self._write_json(room)
            return

        if parsed.path in {"/", "/index.html"}:
            self.path = "/index.html"

        super().do_GET()

    def do_POST(self) -> None:
        if self.path == "/api/auth/register":
            payload = self._read_json()
            try:
                response = AUTH_SERVICE.register(
                    str(payload.get("username", "")),
                    str(payload.get("password", "")),
                )
            except ValueError as error:
                self._write_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return

            self._write_json(response)
            return

        if self.path == "/api/auth/login":
            payload = self._read_json()
            try:
                response = AUTH_SERVICE.login(
                    str(payload.get("username", "")),
                    str(payload.get("password", "")),
                )
            except ValueError as error:
                self._write_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return

            self._write_json(response)
            return

        if self.path == "/api/auth/logout":
            token = self._bearer_token()
            if token:
                AUTH_SERVICE.logout(token)
            self._write_json({"status": "ok"})
            return

        if self.path == "/api/session":
            payload = self._read_json()
            difficulty = payload.get("difficulty")
            if difficulty not in REPOSITORY.difficulties:
                self._write_json({"error": "Unknown difficulty."}, status=HTTPStatus.BAD_REQUEST)
                return

            session_id = str(uuid.uuid4())
            try:
                player_name, tracked_account_username = self._resolve_player_identity(
                    payload,
                    allow_account_override=True,
                    allow_reserved_name=bool(payload.get("local_mode")),
                )
                round_payload = GAME_SERVICE.create_session(
                    session_id,
                    player_name,
                    difficulty,
                    tracked_account_username=tracked_account_username,
                )
            except ValueError as error:
                self._write_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._write_json({"session_id": session_id, "round": round_payload})
            return

        if self.path == "/api/room":
            payload = self._read_json()
            difficulty = payload.get("difficulty")
            if difficulty not in REPOSITORY.difficulties:
                self._write_json({"error": "Unknown difficulty."}, status=HTTPStatus.BAD_REQUEST)
                return

            try:
                player_name, tracked_account_username = self._resolve_player_identity(payload, allow_account_override=False)
                response = GAME_SERVICE.create_room(
                    session_id=str(uuid.uuid4()),
                    player_name=player_name,
                    difficulty=difficulty,
                    tracked_account_username=tracked_account_username,
                )
            except ValueError as error:
                self._write_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            except RuntimeError as error:
                self._write_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._write_json(response)
            return

        if self.path == "/api/public-room":
            payload = self._read_json()
            difficulty = payload.get("difficulty")
            if difficulty not in REPOSITORY.difficulties:
                self._write_json({"error": "Unknown difficulty."}, status=HTTPStatus.BAD_REQUEST)
                return

            account = self._require_account()
            if account is None:
                return

            try:
                response = GAME_SERVICE.join_public_room(
                    session_id=str(uuid.uuid4()),
                    player_name=str(account["username"]),
                    difficulty=difficulty,
                    tracked_account_username=str(account["username"]),
                )
            except RuntimeError as error:
                self._write_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._write_json(response)
            return

        if self.path == "/api/room/join":
            payload = self._read_json()
            try:
                player_name, tracked_account_username = self._resolve_player_identity(payload, allow_account_override=False)
                response = GAME_SERVICE.join_room(
                    session_id=str(uuid.uuid4()),
                    room_code=payload.get("room_code", ""),
                    player_name=player_name,
                    tracked_account_username=tracked_account_username,
                )
            except KeyError:
                self._write_json({"error": "Unknown room code."}, status=HTTPStatus.BAD_REQUEST)
                return
            except ValueError as error:
                self._write_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            except RuntimeError as error:
                self._write_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return

            self._write_json(response)
            return

        if self.path == "/api/guess":
            payload = self._read_json()
            try:
                response = self._with_session(
                    payload,
                    GAME_SERVICE.evaluate_guess,
                    payload.get("guess", ""),
                    payload.get("wpm", 0),
                )
                if response is not None:
                    self._write_json(response)
            except RuntimeError as error:
                self._write_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/skip":
            payload = self._read_json()
            try:
                response = self._with_session(payload, GAME_SERVICE.skip_word)
                if response is not None:
                    self._write_json(response)
            except RuntimeError as error:
                self._write_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/room/draft":
            payload = self._read_json()
            try:
                response = self._with_session(payload, GAME_SERVICE.update_room_draft, payload.get("draft_text", ""))
                if response is not None:
                    self._write_json(response)
            except RuntimeError as error:
                self._write_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/room/chat":
            payload = self._read_json()
            try:
                response = self._with_session(payload, GAME_SERVICE.send_room_chat, payload.get("message", ""))
                if response is not None:
                    self._write_json(response)
            except RuntimeError as error:
                self._write_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/forfeit":
            payload = self._read_json()
            session_id = payload.get("session_id")
            if not session_id or session_id not in GAME_SERVICE.sessions:
                self._write_json({"status": "ok"})
                return

            response = GAME_SERVICE.forfeit_session(session_id)
            self._write_json(response or {"status": "ok"})
            return

        self._write_json({"error": "Unknown endpoint."}, status=HTTPStatus.NOT_FOUND)

    def _origin(self) -> str:
        host = self.headers.get("Host", "127.0.0.1:8000")
        return f"http://{host}"

    def _bearer_token(self) -> str:
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return ""
        return auth_header.split(" ", 1)[1].strip()

    def _require_account(self) -> dict[str, object] | None:
        token = self._bearer_token()
        if not token:
            self._write_json({"error": "You must log in to access public multiplayer."}, status=HTTPStatus.UNAUTHORIZED)
            return None

        try:
            return AUTH_SERVICE.account_for_token(token)
        except KeyError:
            self._write_json({"error": "Your login session expired. Please log in again."}, status=HTTPStatus.UNAUTHORIZED)
            return None

    def _optional_account(self) -> dict[str, object] | None:
        token = self._bearer_token()
        if not token:
            return None
        try:
            return AUTH_SERVICE.account_for_token(token)
        except KeyError:
            return None

    def _resolve_player_identity(
        self,
        payload: dict[str, object],
        allow_account_override: bool,
        allow_reserved_name: bool = False,
    ) -> tuple[str, str | None]:
        account = self._optional_account()
        requested_name = str(payload.get("player_name", "")).strip()

        if account and not requested_name:
            requested_name = str(account["username"])

        if not requested_name:
            requested_name = "Player One"

        owner = ACCOUNT_STORE.username_owner(requested_name)
        if owner is not None and not allow_reserved_name:
            if not account or str(account["username"]) != owner:
                raise ValueError("That username belongs to an account. Log in as that account or choose another name.")

        if account and (not allow_account_override or requested_name == str(account["username"])):
            return str(account["username"]), str(account["username"])

        tracked_account_username = str(account["username"]) if account and requested_name == str(account["username"]) else None
        return requested_name, tracked_account_username

    def _with_session(self, payload: dict[str, str], callback, *extra_args):
        session_id = payload.get("session_id")
        if not session_id or session_id not in GAME_SERVICE.sessions:
            self._write_json({"error": "Invalid or expired session."}, status=HTTPStatus.BAD_REQUEST)
            return None

        return callback(session_id, *extra_args)

    def _read_json(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length) if content_length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _write_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_oauth_popup(self, status: str, payload: str) -> None:
        safe_status = json.dumps(status)
        safe_payload = json.dumps(payload)
        body = f"""<!DOCTYPE html>
<html lang="en">
<body>
<script>
const status = {safe_status};
const payload = {safe_payload};
if (window.opener) {{
    window.opener.postMessage({{ type: "discord-auth", status, payload }}, window.location.origin);
}}
window.close();
</script>
</body>
</html>""".encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run(host: str = "0.0.0.0", port: int = 8000) -> None:
    server = ThreadingHTTPServer((host, port), ActivityHandler, bind_and_activate=True)
    print("Serving server on:")
    print(f"  Local:   http://127.0.0.1:{port}")

    if host == "0.0.0.0":
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            local_ip = ""

        if local_ip and local_ip != "127.0.0.1":
            print(f"  Network: http://{local_ip}:{port}")
        print(f"  Bound to all IPv4 interfaces on port {port}")
    else:
        print(f"  Bound:   http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()

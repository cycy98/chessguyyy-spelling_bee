from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

_raw_secret = os.environ.get("AUTH_SECRET")
if _raw_secret:
    SECRET = bytes.fromhex(_raw_secret)
else:
    warnings.warn(
        "AUTH_SECRET not set — sessions won't survive restarts or scale across workers. "
        'Generate one with: python -c "import secrets; print(secrets.token_bytes(32).hex())"',
        stacklevel=1,
    )
    SECRET = secrets.token_bytes(32)

# Set COOKIE_SECURE=true in production (HTTPS). Leave unset for local HTTP dev.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes")

COOKIE_MAX_AGE = 3 * 24 * 3600


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ":" + h.hex()


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1)
        return hmac.compare_digest(h.hex(), hash_hex)
    except Exception:
        return False


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


def _session_cookie_kwargs() -> dict:
    return {"httponly": True, "samesite": "lax", "secure": COOKIE_SECURE, "path": "/"}


def set_auth_cookie(response: Response, username: str) -> None:
    # SameSite=Lax blocks cross-origin POSTs, which is sufficient CSRF protection
    # for these endpoints — no additional CSRF tokens are needed.
    response.set_cookie(
        "auth", make_token(username), max_age=COOKIE_MAX_AGE, **_session_cookie_kwargs(),
    )


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        "session_id", session_id, max_age=COOKIE_MAX_AGE, **_session_cookie_kwargs(),
    )

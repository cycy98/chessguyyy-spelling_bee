from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

SECRET = secrets.token_bytes(32)
COOKIE_MAX_AGE = 7 * 24 * 3600


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ":" + h.hex()


def verify_password(pw: str, stored: str) -> bool:
    salt_hex, hash_hex = stored.split(":")
    salt = bytes.fromhex(salt_hex)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1)
    return hmac.compare_digest(h.hex(), hash_hex)


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

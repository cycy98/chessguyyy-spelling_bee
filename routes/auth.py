"""Spelling Bee — auth routes (register, login, logout)."""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from backend import db
from backend.auth import hash_password, set_auth_cookie, verify_password
from backend.errors import HtmxError
from templating import client_ip, tpl

router = APIRouter()


@router.post("/register", response_class=HTMLResponse)
async def register(request: Request) -> HTMLResponse:
    state = request.app.state.srv
    ip = client_ip(request)
    if not state.check_rate(ip, "register"):
        raise HtmxError("Too many attempts. Try again later.", 429)

    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))

    async def _err(msg: str) -> HTMLResponse:
        return await tpl(request, "fragments/menu.html", {"auth_error": msg})

    if not re.match(r"^[A-Za-z0-9_]{3,24}$", username):
        return await _err("Username must be 3-24 characters (letters, numbers, underscore).")

    if len(password) < 8 or len(password) > 128:
        return await _err("Password must be 8-128 characters.")

    async with db.transaction() as conn:
        cursor = await conn.execute("SELECT 1 FROM users WHERE username = ?", (username,))
        if await cursor.fetchone():
            return await _err("Could not create account. Try a different username.")
        pw_hash = hash_password(password)
        await conn.execute(
            "INSERT INTO users (username, pw_hash) VALUES (?, ?)",
            (username, pw_hash),
        )

    resp = await tpl(request, "fragments/menu.html", {"user": username, "elo": 1000.0})
    set_auth_cookie(resp, username)
    resp.headers["HX-Trigger"] = json.dumps({"auth-changed": {"user": username}})
    return resp


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request) -> HTMLResponse:
    state = request.app.state.srv
    ip = client_ip(request)
    if not state.check_rate(ip, "login"):
        raise HtmxError("Too many attempts. Try again later.", 429)

    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))

    row = await db.fetchone("SELECT * FROM users WHERE username = ?", (username,))

    if not row or not verify_password(password, row["pw_hash"]):
        return await tpl(
            request,
            "fragments/menu.html",
            {"auth_error": "Unknown username or password."},
        )

    resp = await tpl(request, "fragments/menu.html", {"user": row["username"], "elo": row["elo"]})
    set_auth_cookie(resp, row["username"])
    resp.headers["HX-Trigger"] = json.dumps({"auth-changed": {"user": row["username"]}})
    return resp


@router.post("/logout", response_class=HTMLResponse)
async def logout(request: Request) -> HTMLResponse:
    resp = await tpl(request, "fragments/menu.html", {"user": None})
    resp.delete_cookie("auth", path="/")
    resp.headers["HX-Trigger"] = json.dumps({"auth-changed": {"user": None}})
    return resp

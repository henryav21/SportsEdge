"""Passcode gate for reaching the dashboard from outside this computer.

Requests made on this computer (http://127.0.0.1:8765) are never asked for anything. A request that
arrives through the tunnel or the Vercel address must carry a session cookie, which is handed out only
after the passcode is entered. Until a passcode has been set, remote access is locked completely.

Only a salted hash of the passcode is stored (history/access.json, which is not committed anywhere).
Set it with:  .\\sportsedge passcode
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

from pathlib import Path

from . import config

ROUNDS = 240_000
SESSION_DAYS = 30
COOKIE = "se_session"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
FORWARDED = ("x-forwarded-for", "x-forwarded-host", "cf-connecting-ip", "cf-ray", "x-vercel-id", "x-real-ip")


def _path() -> Path:
    d = Path(os.getenv("SPORTSEDGE_HISTORY") or (config.ROOT / "history"))  # same folder tracker.history_dir() uses
    d.mkdir(parents=True, exist_ok=True)
    return d / "access.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict):
    _path().write_text(json.dumps(d), encoding="utf-8")


def _hash(passcode: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, ROUNDS).hex()


def has_passcode() -> bool:
    d = _load()
    return bool(d.get("hash") and d.get("salt") and d.get("secret"))


def set_passcode(passcode: str):
    """Store a new passcode. Everyone who was signed in has to sign in again."""
    salt = os.urandom(16)
    _save({"salt": salt.hex(), "hash": _hash(passcode, salt), "secret": os.urandom(32).hex()})


def clear_passcode():
    """Remove the passcode: remote access is locked again."""
    try:
        _path().unlink()
    except FileNotFoundError:
        pass


def verify(passcode: str) -> bool:
    d = _load()
    if not d.get("hash"):
        return False
    return hmac.compare_digest(_hash(passcode, bytes.fromhex(d["salt"])), d["hash"])


def _sign(secret: str, exp: int) -> str:
    return hmac.new(bytes.fromhex(secret), str(exp).encode(), hashlib.sha256).hexdigest()


def new_token() -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    return f"{exp}.{_sign(_load()['secret'], exp)}"


def valid_token(token: str | None) -> bool:
    d = _load()
    if not token or not d.get("secret"):
        return False
    exp, _, sig = token.partition(".")
    if not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(sig, _sign(d["secret"], int(exp)))


def is_remote(headers, host: str | None) -> bool:
    """True for a request that did not come straight from a browser on this computer."""
    if any(h in headers for h in FORWARDED):
        return True
    host = (host or "").strip().lower()
    if host.startswith("["):
        name = host.split("]")[0] + "]"
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name not in LOCAL_HOSTS


# ---------------------------------------------------------------- wrong-passcode limit
_fails: dict[str, list[float]] = {}
MAX_FAILS, WINDOW = 8, 15 * 60


def blocked(who: str) -> bool:
    now = time.time()
    _fails[who] = [t for t in _fails.get(who, []) if now - t < WINDOW]
    return len(_fails[who]) >= MAX_FAILS


def failed(who: str):
    _fails.setdefault(who, []).append(time.time())

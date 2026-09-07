"""Per-franchise logins.

The league is eight people who already know their team name, so the account
model is deliberately thin: one login per franchise, username *is* the franchise
name, password is machine-generated once and never chosen by a human. There is
no signup, no reset-by-email and no password change flow -- resets go through
`scripts/credentials.py`, which is the only thing that ever sees a plaintext
password after seeding.

Everything here is stdlib. Adding a session/crypto dependency for eight users
would mean a rebuild for no capability we do not already have in `hmac`,
`hashlib` and `secrets`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import db

log = logging.getLogger("fantasy.auth")

SESSION_COOKIE = "fl_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

_PBKDF2_ITERATIONS = 240_000
# No 0/O/1/l/I/8/B: these are read off a screen and retyped on a phone, and a
# password nobody chose is one nobody can correct from memory.
_PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyzACDEFGHJKLMNPQRSTUVWXYZ2345679"


# --- passwords ------------------------------------------------------------


def generate_password(groups: int = 3, size: int = 4) -> str:
    """A short, unambiguous, hyphenated password -- readable aloud."""
    return "-".join(
        "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(size))
        for _ in range(groups)
    )


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return "$".join((
        "pbkdf2_sha256",
        str(_PBKDF2_ITERATIONS),
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    ))


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check. A malformed stored value is a failed login, never a
    crash -- a corrupt row must not take the login page down for everyone."""
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest_b64)
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), base64.b64decode(salt_b64), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


# --- usernames ------------------------------------------------------------


def normalize_username(name: str) -> str:
    """Fold a franchise name to its lookup key.

    ESPN team names carry stray spacing, mixed case and the odd accent, and
    people retype them from memory. Matching on a folded key means "WhitePowder's"
    and "whitepowders " are the same account without inventing separate handles.
    """
    folded = unicodedata.normalize("NFKD", name or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", folded).strip().casefold()


def _unique_key(conn, team_id: int, name: str) -> tuple[str, str]:
    """Display name plus a key no other franchise already holds.

    Two slots really can share a name in ESPN; the id suffix keeps the UNIQUE
    constraint from turning that into a startup crash.
    """
    display, key = (name or f"Team {team_id}").strip(), normalize_username(name)
    if not key:
        display, key = f"Team {team_id}", f"team {team_id}"
    owner = db.find_franchise_user(conn, key)
    if owner and int(owner["team_id"]) != team_id:
        display, key = f"{display} {team_id}", f"{key} {team_id}"
    return display, key


# --- session cookie -------------------------------------------------------


def session_secret(conn) -> bytes:
    """Signing key: SECRET_KEY if set, else one generated and stored once.

    Persisting it matters -- a key regenerated per boot would silently log
    everyone out on every deploy, which reads as "the login is broken".
    """
    env = os.environ.get("SECRET_KEY", "").strip()
    if env:
        return env.encode()
    stored = db.get_meta(conn, "session_secret")
    if not stored:
        stored = secrets.token_urlsafe(32)
        db.set_meta(conn, "session_secret", stored)
    return str(stored).encode()


def _fingerprint(password_hash: str) -> str:
    """Short digest of the stored password, carried inside the session.

    This is what makes a password reset log that franchise out everywhere.
    """
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def _sign(secret: bytes, payload: str) -> str:
    mac = hmac.new(secret, payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def make_token(conn, user: dict) -> str:
    expires = int(datetime.now(timezone.utc).timestamp()) + SESSION_MAX_AGE
    payload = f"{user['team_id']}:{expires}:{_fingerprint(user['password_hash'])}"
    return f"{payload}:{_sign(session_secret(conn), payload)}"


def read_token(conn, token: str | None) -> dict | None:
    """Return the signed-in franchise, or None for anything not currently valid."""
    if not token:
        return None
    try:
        team_id, expires, fingerprint, signature = token.rsplit(":", 3)
        payload = f"{team_id}:{expires}:{fingerprint}"
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(session_secret(conn), payload), signature):
        return None
    try:
        if int(expires) < datetime.now(timezone.utc).timestamp():
            return None
        user = db.get_franchise_user(conn, int(team_id))
    except ValueError:
        return None
    if not user or not hmac.compare_digest(_fingerprint(user["password_hash"]), fingerprint):
        return None
    return user


# --- login ----------------------------------------------------------------


def authenticate(conn, username: str, password: str) -> dict | None:
    user = db.find_franchise_user(conn, normalize_username(username))
    if user is None:
        # Hash anyway: bailing out early makes "no such franchise" measurably
        # faster than "wrong password", which is a free user list.
        verify_password(password, hash_password("no-such-user"))
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    db.touch_franchise_login(conn, int(user["team_id"]))
    return user


# --- seeding --------------------------------------------------------------


def seed_accounts(conn, franchises: dict[int, str]) -> list[dict]:
    """Give every franchise a login, and keep usernames on the current name.

    Returns only the accounts created by this call, with their plaintext
    passwords -- the single moment they exist outside a digest.
    """
    created = []
    for team_id, name in sorted(franchises.items()):
        display, key = _unique_key(conn, team_id, name)
        existing = db.get_franchise_user(conn, team_id)
        if existing is None:
            password = generate_password()
            db.create_franchise_user(conn, team_id, display, key, hash_password(password))
            created.append({"team_id": team_id, "username": display, "password": password})
        elif existing["username_key"] != key:
            # The franchise renamed. Follow it: the login people will try is
            # whatever the site now calls their team.
            db.set_franchise_username(conn, team_id, display, key)
            log.info("franchise %s login renamed %r -> %r",
                     team_id, existing["username"], display)
    return created


def write_credentials_file(path: Path, created: list[dict]) -> Path | None:
    """Append newly minted logins to a file only the owner can read.

    Passwords have to leave the process somehow to be handed out, and the
    container's stdout is the wrong place for something that lives forever in a
    log aggregator. This file sits in the bind-mounted data/ directory, which is
    already gitignored.
    """
    if not created:
        return None
    fresh = not path.exists()
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as handle:
        if fresh:
            handle.write("# Franchise logins. Hand these out, then delete this file.\n")
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for row in created:
            handle.write(f"{stamp}\t{row['username']}\t{row['password']}\n")
    return path

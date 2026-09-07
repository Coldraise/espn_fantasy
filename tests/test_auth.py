"""Franchise logins: hashing, session cookies, seeding, and the scoreboard split.

The interesting cases here are the ones that would fail quietly: a session that
survives a password reset, a rename that orphans someone's login, and a
scoreboard that shows you someone else's game first.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3

import pytest

from app import auth, db, main


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.init_db(connection)
    yield connection
    connection.close()


# --- passwords ------------------------------------------------------------

def test_password_roundtrip():
    stored = auth.hash_password("hunter2")
    assert auth.verify_password("hunter2", stored)
    assert not auth.verify_password("hunter3", stored)


def test_hash_is_salted():
    """Two franchises handed the same password must not share a digest."""
    assert auth.hash_password("same") != auth.hash_password("same")


def test_verify_rejects_garbage_instead_of_raising():
    # A corrupt row is one failed login, not a 500 on the login page.
    for junk in ["", "nonsense", "md5$1$x$y", "pbkdf2_sha256$notanint$x$y"]:
        assert auth.verify_password("x", junk) is False


def test_generated_passwords_avoid_ambiguous_characters():
    sample = "".join(auth.generate_password() for _ in range(50))
    assert not set(sample) & set("0O1lIB8")


# --- usernames ------------------------------------------------------------

def test_username_normalisation_folds_case_spacing_and_accents():
    assert auth.normalize_username("  WhitePowder's ") == "whitepowder's"
    assert auth.normalize_username("Szabyest") == auth.normalize_username("SZABYEST")
    assert auth.normalize_username("Juhász") == "juhasz"


# --- seeding --------------------------------------------------------------

def test_seed_creates_one_account_per_franchise_once(conn):
    created = auth.seed_accounts(conn, {1: "gojzi", 2: "Obsessions"})
    assert {c["username"] for c in created} == {"gojzi", "Obsessions"}
    # Re-running must not mint new passwords -- that would silently lock people out.
    assert auth.seed_accounts(conn, {1: "gojzi", 2: "Obsessions"}) == []


def test_seed_follows_a_rename_and_keeps_the_password(conn):
    password = auth.seed_accounts(conn, {5: "Kanmalacok"})[0]["password"]
    auth.seed_accounts(conn, {5: "Juhu"})
    assert auth.authenticate(conn, "Kanmalacok", password) is None
    user = auth.authenticate(conn, "juhu", password)
    assert user and int(user["team_id"]) == 5


def test_seed_disambiguates_two_franchises_sharing_a_name(conn):
    """The UNIQUE key must not turn a duplicate ESPN team name into a crash."""
    created = auth.seed_accounts(conn, {3: "Team", 4: "Team"})
    assert len({c["username"] for c in created}) == 2


# --- authentication -------------------------------------------------------

def test_authenticate_accepts_the_franchise_name_in_any_casing(conn):
    password = auth.seed_accounts(conn, {3: "MaDMAxKlein"})[0]["password"]
    assert auth.authenticate(conn, "  madmaxklein  ", password)
    assert auth.authenticate(conn, "MaDMAxKlein", "wrong") is None
    assert auth.authenticate(conn, "nobody", password) is None


# --- sessions -------------------------------------------------------------

def test_session_roundtrip(conn):
    auth.seed_accounts(conn, {1: "gojzi"})
    user = db.get_franchise_user(conn, 1)
    assert int(auth.read_token(conn, auth.make_token(conn, user))["team_id"]) == 1


def test_tampered_or_missing_token_is_rejected(conn):
    auth.seed_accounts(conn, {1: "gojzi"})
    token = auth.make_token(conn, db.get_franchise_user(conn, 1))
    team_id, expires, fingerprint, signature = token.rsplit(":", 3)
    assert auth.read_token(conn, None) is None
    assert auth.read_token(conn, "garbage") is None
    # Same signature, different franchise: the id is inside the signed payload.
    assert auth.read_token(conn, f"2:{expires}:{fingerprint}:{signature}") is None


def test_expired_token_is_rejected(conn, monkeypatch):
    auth.seed_accounts(conn, {1: "gojzi"})
    monkeypatch.setattr(auth, "SESSION_MAX_AGE", -1)
    assert auth.read_token(conn, auth.make_token(conn, db.get_franchise_user(conn, 1))) is None


def test_password_reset_invalidates_live_sessions(conn):
    """The cookie carries a fingerprint of the stored digest, so a reset is a
    logout everywhere -- otherwise a leaked password stays usable forever."""
    auth.seed_accounts(conn, {1: "gojzi"})
    token = auth.make_token(conn, db.get_franchise_user(conn, 1))
    db.set_franchise_password(conn, 1, auth.hash_password(auth.generate_password()))
    assert auth.read_token(conn, token) is None


def test_session_secret_is_stable_across_calls(conn, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    assert auth.session_secret(conn) == auth.session_secret(conn)


# --- scoreboard ordering --------------------------------------------------

GAMES = [
    {"home": {"id": 1}, "away": {"id": 2}},
    {"home": {"id": 3}, "away": {"id": 4}},
    {"home": {"id": 5}, "away": None, "bye": True},
]


@pytest.mark.parametrize("team_id, expected_home", [(3, 3), (4, 3), (5, 5)])
def test_split_pulls_out_your_game_from_either_side_or_a_bye(team_id, expected_home):
    mine, rest = main._split_games(GAMES, team_id)
    assert mine["home"]["id"] == expected_home
    assert len(rest) == 2 and mine not in rest


def test_split_without_a_login_leaves_the_order_untouched():
    mine, rest = main._split_games(GAMES, None)
    assert mine is None and rest == GAMES


def test_split_for_a_franchise_not_playing_this_week():
    mine, rest = main._split_games(GAMES, 99)
    assert mine is None and rest == GAMES


# --- redirect safety ------------------------------------------------------

@pytest.mark.parametrize("raw", ["//evil.example/x", "https://evil.example", None, "", "evil"])
def test_offsite_next_targets_are_refused(raw):
    assert main._safe_next(raw) == "/"


def test_onsite_next_target_survives():
    assert main._safe_next("/history?season=2026") == "/history?season=2026"

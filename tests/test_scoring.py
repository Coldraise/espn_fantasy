"""Touchdown events and the header ticker.

None of this can be checked against a live game until the season starts, so the
cases that would fail quietly are pinned here instead: a player joining a roster
mid-week must not announce the touchdowns he already had, a poll that changes
nothing must not re-announce, and ESPN's raw stat dict arrives with string keys.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import db, espn_client, players


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.init_db(connection)
    yield connection
    connection.close()


def roster(tds, player_id=1, team_id=7, actual=6.0):
    return [{"team_id": team_id, "player_id": player_id, "name": "Player One",
             "position": "WR", "lineup_slot": "WR", "pro_team": "PHI",
             "is_starter": True, "projected": 12.0, "actual": actual, "tds": tds}]


# --- reading touchdowns out of ESPN's stat block ---------------------------

def test_stats_dict_arrives_with_string_keys():
    """JSON object keys are strings -- "4", never 4 -- so lookups must coerce."""
    assert espn_client._touchdowns({"stats": {"4": 2.0, "25": 1.0, "3": 280.0}}) == 3


def test_integer_keys_also_work():
    assert espn_client._touchdowns({"stats": {43: 1.0}}) == 1


def test_aggregate_ids_are_not_double_counted():
    """105 is the sum of 94 and 101-104; counting both doubles every score."""
    assert espn_client._touchdowns({"stats": {"94": 1.0, "105": 1.0}}) == 1


def test_missing_and_malformed_stats_are_zero():
    assert espn_client._touchdowns({}) == 0
    assert espn_client._touchdowns({"stats": None}) == 0
    assert espn_client._touchdowns({"stats": {"abc": 3, "4": None}}) == 0


# --- turning a rising count into an event ----------------------------------

def test_first_sighting_only_seeds_the_baseline(conn):
    """A player already on two touchdowns when we first store him has not just
    scored twice -- he was signed, or this is the first poll of the week."""
    db.replace_team_week_players(conn, 2026, 1, roster(tds=2))
    assert db.recent_scoring_events(conn, 2026, 1) == []


def test_a_rising_count_is_an_event(conn):
    db.replace_team_week_players(conn, 2026, 1, roster(tds=0))
    db.replace_team_week_players(conn, 2026, 1, roster(tds=1))
    events = db.recent_scoring_events(conn, 2026, 1)
    assert len(events) == 1
    assert events[0]["name"] == "Player One"
    assert events[0]["scored"] == 1
    assert events[0]["tds"] == 1


def test_an_unchanged_count_is_not_re_announced(conn):
    """The poll runs every 45 seconds; a standing total is not news."""
    db.replace_team_week_players(conn, 2026, 1, roster(tds=0))
    db.replace_team_week_players(conn, 2026, 1, roster(tds=1))
    for _ in range(3):
        db.replace_team_week_players(conn, 2026, 1, roster(tds=1))
    assert len(db.recent_scoring_events(conn, 2026, 1)) == 1


def test_two_scores_between_polls_report_as_one_event_of_two(conn):
    db.replace_team_week_players(conn, 2026, 1, roster(tds=0))
    db.replace_team_week_players(conn, 2026, 1, roster(tds=2))
    assert db.recent_scoring_events(conn, 2026, 1)[0]["scored"] == 2


def test_a_falling_count_is_ignored(conn):
    """Stat corrections do take touchdowns away. That is not an event."""
    db.replace_team_week_players(conn, 2026, 1, roster(tds=2))
    db.replace_team_week_players(conn, 2026, 1, roster(tds=3))
    db.replace_team_week_players(conn, 2026, 1, roster(tds=1))
    assert len(db.recent_scoring_events(conn, 2026, 1)) == 1


def test_events_are_time_boxed(conn):
    """Sunday's touchdown is not a live banner on Wednesday."""
    db.replace_team_week_players(conn, 2026, 1, roster(tds=0))
    db.replace_team_week_players(conn, 2026, 1, roster(tds=1))
    stale = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat(timespec="seconds")
    with conn:
        conn.execute("UPDATE scoring_events SET at=?", (stale,))
    assert db.recent_scoring_events(conn, 2026, 1) == []
    assert db.recent_scoring_events(conn, 2026, 1, within_minutes=60 * 24) != []


def test_the_lineup_still_replaces_cleanly(conn):
    """The diff runs inside the replace; the lineup must survive it intact."""
    db.replace_team_week_players(conn, 2026, 1, roster(tds=1))
    stored = db.fetch_team_week_players(conn, 2026, 1)
    assert [p["name"] for p in stored[7]] == ["Player One"]


# --- the header ticker -----------------------------------------------------

ROWS = [
    {"name": "A", "position": "QB", "pro_team": "PHI", "projected": 20, "actual": 5},
    {"name": "B", "position": "QB", "pro_team": "BUF", "projected": 18, "actual": 31},
    {"name": "C", "position": "QB", "pro_team": "KC", "projected": 17, "actual": 9},
    {"name": "D", "position": "QB", "pro_team": "LAR", "projected": 1, "actual": 1},
    {"name": "Snapper", "position": "LS", "pro_team": "NE", "projected": 99, "actual": 99},
]


def test_ticker_ranks_on_projection_before_anyone_has_played():
    qb = players.leaders(ROWS, per=3)[0]
    assert [p["name"] for p in qb["players"]] == ["A", "B", "C"]


def test_ticker_ranks_on_the_result_once_played():
    qb = players.leaders(ROWS, per=3, by="actual")[0]
    assert [p["name"] for p in qb["players"]] == ["B", "C", "A"]


def test_ticker_drops_positions_the_league_does_not_start():
    named = [p["name"] for g in players.leaders(ROWS) for p in g["players"]]
    assert "Snapper" not in named

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


def test_a_half_hour_old_score_has_already_passed(conn):
    """The strip is an event, not a standing summary. A touchdown from earlier
    in the afternoon must not still be running across the top."""
    db.replace_team_week_players(conn, 2026, 1, roster(tds=0))
    db.replace_team_week_players(conn, 2026, 1, roster(tds=1))
    then = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
    with conn:
        conn.execute("UPDATE scoring_events SET at=?", (then,))
    assert db.recent_scoring_events(conn, 2026, 1) == []


def test_a_score_from_a_moment_ago_is_shown(conn):
    db.replace_team_week_players(conn, 2026, 1, roster(tds=0))
    db.replace_team_week_players(conn, 2026, 1, roster(tds=1))
    assert len(db.recent_scoring_events(conn, 2026, 1)) == 1


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


# --- matchup_rosters totals parsing ------------------------------------------

def test_matchup_rosters_parses_live_side_totals(monkeypatch):
    """The Live keys win because totalPoints reads 0.0 for the whole span of a
    live week -- trusting it would store a zero all Sunday, which is a bug that
    actually happened. The live projection converges as games play out."""
    payload = {
        "schedule": [
            {
                "matchupPeriodId": 1,
                "home": {
                    "teamId": 1,
                    "totalPointsLive": 72.5,
                    "totalProjectedPointsLive": 200.783,
                    "totalPoints": 0.0,
                    "totalProjectedPoints": 190.0,
                    "rosterForCurrentScoringPeriod": {"entries": []},
                },
                "away": {
                    "teamId": 2,
                    "rosterForCurrentScoringPeriod": {"entries": []},
                },
            }
        ]
    }

    monkeypatch.setattr(espn_client, "_get", lambda url, params: payload)
    monkeypatch.setattr(espn_client, "_current_url", lambda: "http://test")
    rosters, totals = espn_client.matchup_rosters(2026, 1)

    assert totals[1]["points"] == 72.5, "totalPointsLive is preferred"
    assert totals[1]["projected"] == 200.8, "totalProjectedPointsLive rounded to 1 decimal"
    assert totals[1]["live"] is True


def test_matchup_rosters_parses_settled_side_totals(monkeypatch):
    """A settled week has no Live keys. The plain totalPoints and
    totalProjectedPoints are the real values."""
    payload = {
        "schedule": [
            {
                "matchupPeriodId": 1,
                "home": {
                    "teamId": 1,
                    "totalPoints": 143.2,
                    "totalProjectedPoints": 151.0,
                    "rosterForCurrentScoringPeriod": {"entries": []},
                },
                "away": {
                    "teamId": 2,
                    "rosterForCurrentScoringPeriod": {"entries": []},
                },
            }
        ]
    }

    monkeypatch.setattr(espn_client, "_get", lambda url, params: payload)
    monkeypatch.setattr(espn_client, "_current_url", lambda: "http://test")
    rosters, totals = espn_client.matchup_rosters(2026, 1)

    assert totals[1]["points"] == 143.2
    assert totals[1]["projected"] == 151.0
    assert totals[1]["live"] is False


def test_matchup_rosters_handles_missing_totals(monkeypatch):
    """Absent keys stay None, not zero."""
    payload = {
        "schedule": [
            {
                "matchupPeriodId": 1,
                "home": {
                    "teamId": 1,
                    "totalPoints": 100.0,
                    # totalProjectedPoints absent
                    "rosterForCurrentScoringPeriod": {"entries": []},
                },
                "away": {
                    "teamId": 2,
                    "rosterForCurrentScoringPeriod": {"entries": []},
                },
            }
        ]
    }

    monkeypatch.setattr(espn_client, "_get", lambda url, params: payload)
    monkeypatch.setattr(espn_client, "_current_url", lambda: "http://test")
    rosters, totals = espn_client.matchup_rosters(2026, 1)

    assert totals[1]["points"] == 100.0
    assert totals[1]["projected"] is None


# --- upsert_team_week revision semantics ------------------------------------

def test_upsert_team_week_bumps_revision_on_points_change(conn):
    """A revision bump means ESPN corrected the RECORD. Projection changes
    are forecasts being refined, not corrections."""
    # First insert
    changed = db.upsert_team_week(conn, 2026, 1, 1, 98.5, 120.0, 2, False, "W")
    assert changed is True
    row = conn.execute(
        "SELECT points, projected, revision FROM team_weeks WHERE season=? AND week=? AND team_id=?",
        (2026, 1, 1)
    ).fetchone()
    assert row["points"] == 98.5
    assert row["projected"] == 120.0
    assert row["revision"] == 1

    # Change only projected
    changed = db.upsert_team_week(conn, 2026, 1, 1, 98.5, 125.0, 2, False, "W")
    assert changed is True, "change was written"
    row = conn.execute(
        "SELECT points, projected, revision FROM team_weeks WHERE season=? AND week=? AND team_id=?",
        (2026, 1, 1)
    ).fetchone()
    assert row["projected"] == 125.0
    assert row["revision"] == 1, "revision unchanged for projection-only change"

    # Change points
    changed = db.upsert_team_week(conn, 2026, 1, 1, 99.5, 125.0, 2, False, "W")
    assert changed is True
    row = conn.execute(
        "SELECT points, projected, revision FROM team_weeks WHERE season=? AND week=? AND team_id=?",
        (2026, 1, 1)
    ).fetchone()
    assert row["points"] == 99.5
    assert row["revision"] == 2, "revision bumped for points change"


def test_upsert_team_week_identical_data_returns_false(conn):
    """A re-poll of fully identical data writes nothing."""
    changed = db.upsert_team_week(conn, 2026, 1, 1, 98.5, 120.0, 2, False, "W")
    assert changed is True

    # Identical re-insert
    changed = db.upsert_team_week(conn, 2026, 1, 1, 98.5, 120.0, 2, False, "W")
    assert changed is False, "identical data produces no change"


def test_upsert_team_week_bumps_revision_on_result_change(conn):
    """Result changes (win/loss/tie) count as a record change."""
    db.upsert_team_week(conn, 2026, 1, 1, 98.5, 120.0, 2, False, "W")
    changed = db.upsert_team_week(conn, 2026, 1, 1, 98.5, 120.0, 2, False, "L")
    assert changed is True
    row = conn.execute(
        "SELECT result, revision FROM team_weeks WHERE season=? AND week=? AND team_id=?",
        (2026, 1, 1)
    ).fetchone()
    assert row["result"] == "L"
    assert row["revision"] == 2, "revision bumped for result change"


# --- poller._live_scores behavior -------------------------------------------

def test_live_scores_includes_teams_with_live_flag_and_points():
    """Only teams with live=True contribute to actual scores during a live week."""
    from app import poller
    from unittest.mock import patch

    totals = {
        1: {"points": 72.5, "projected": 200.8, "live": True},
        2: {"points": 65.0, "projected": 180.0, "live": False},
        3: {"points": 0.0, "projected": 150.0, "live": True},
    }

    with patch("app.espn_client.matchup_rosters", return_value=({}, totals)):
        actual, projected = poller._live_scores(2026, 1)

    assert actual == {1: 72.5, 3: 0.0}, "only live teams with points included"
    assert projected == {1: 200.8, 2: 180.0, 3: 150.0}, "all truthy projected values included"


def test_live_scores_omits_settled_weeks_from_actual():
    """Before kickoff ESPN reports 0.0. Writing that would overwrite the
    scoreboard's own value with a zero; leaving it out lets poll_week keep what
    it had."""
    from app import poller
    from unittest.mock import patch

    totals = {
        1: {"points": 143.2, "projected": 151.0, "live": False},
        2: {"points": None, "projected": 145.0, "live": False},
    }

    with patch("app.espn_client.matchup_rosters", return_value=({}, totals)):
        actual, projected = poller._live_scores(2026, 1)

    assert actual == {}, "no live=False teams in actual"
    assert projected == {1: 151.0, 2: 145.0}, "projected still included"


def test_live_scores_omits_falsy_projected():
    """Zero or None projected means no live projection available."""
    from app import poller
    from unittest.mock import patch

    totals = {
        1: {"points": 72.5, "projected": 0.0, "live": True},
        2: {"points": 65.0, "projected": None, "live": False},
        3: {"points": 85.0, "projected": 195.5, "live": True},
    }

    with patch("app.espn_client.matchup_rosters", return_value=({}, totals)):
        actual, projected = poller._live_scores(2026, 1)

    assert projected == {3: 195.5}, "only truthy projected values included"


def test_live_scores_returns_empty_dicts_on_exception():
    """Failures are best-effort. Never propagate -- it is a non-critical fetch."""
    from app import poller
    from unittest.mock import patch

    def broken(*args, **kwargs):
        raise ValueError("Network error")

    with patch("app.espn_client.matchup_rosters", side_effect=broken):
        actual, projected = poller._live_scores(2026, 1)

    assert actual == {}
    assert projected == {}

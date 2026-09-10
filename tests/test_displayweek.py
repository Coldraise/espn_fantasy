"""Which week the scoreboard names.

The regression: the pre-season sync writes every matchup period in the schedule
up front, so `MAX(week)` over team_weeks is the last playoff week from the day
the season is created. The dashboard rendered that empty week 15 all season
while week 1 was being played.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import types

import pytest

from app import db, main

CURRENT = 2026


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.init_db(connection)
    # The whole 15-week schedule, pre-written and unplayed -- the shape that
    # made MAX(week) the wrong answer.
    for week in range(1, 16):
        for team_id in (1, 2):
            db.upsert_team_week(connection, CURRENT, week, team_id,
                                None, None, 3 - team_id, False, None)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def stub_config(monkeypatch):
    monkeypatch.setattr(
        main, "get_config",
        lambda: types.SimpleNamespace(current_season=CURRENT),
    )


def test_uses_the_leagues_current_matchup_period(conn):
    db.set_meta(conn, "status", {"current_matchup_period": 4})
    assert main._display_week(conn, CURRENT) == 4


def test_falls_back_to_the_last_played_week(conn):
    # No league status, but weeks 1-2 have been played.
    for week in (1, 2):
        db.upsert_team_week(conn, CURRENT, week, 1, 88.0, None, 2, False, "W")
    assert main._display_week(conn, CURRENT) == 2


def test_missing_status_does_not_name_the_last_playoff_week(conn):
    """THE REGRESSION: a fresh deploy, or a first poll that never landed.

    Before the fix this returned 15 -- the last pre-written schedule week --
    and the dashboard rendered an empty scoreboard all season.
    """
    assert main._display_week(conn, CURRENT) == 1


def test_zero_matchup_period_is_treated_as_absent(conn):
    db.set_meta(conn, "status", {"current_matchup_period": 0})
    assert main._display_week(conn, CURRENT) == 1


def test_past_season_uses_its_last_week(conn):
    for week in (1, 2, 3):
        db.upsert_team_week(conn, 2025, week, 1, 70.0, None, 2, False, "W")
    assert main._display_week(conn, 2025) == 3


def test_season_with_no_rows_is_none(conn):
    assert main._display_week(conn, 2019) is None

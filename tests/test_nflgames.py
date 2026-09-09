"""NFL scoreboard parsing, storage, and the kickoff-label filter.

Covers the two-timezone split (game day off LEAGUE_TZ, clock time off the
reader's display_tz) and the abbreviation join that makes a fixture's teams
resolvable to a colour and, downstream, to a rostered player's pro_team.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zoneinfo import ZoneInfo

from app import db, espn_client, main
from app.players import PRO_TEAM_COLORS

# One event of each shape the parser has to handle: a pregame kickoff, a live
# game with a clock and period, and one malformed event with no competitions
# at all -- the kind of partial payload that must not cost the other games.
FIXTURE_PAYLOAD = {
    "events": [
        {
            "id": "401872656",
            "date": "2026-09-10T00:20Z",
            "status": {
                "type": {"state": "pre"},
                "period": 0,
                "displayClock": "0:00",
            },
            "competitions": [{
                "competitors": [
                    {"homeAway": "home", "team": {"abbreviation": "NE"}},
                    {"homeAway": "away", "team": {"abbreviation": "SEA"}},
                ],
            }],
        },
        {
            "id": "401872657",
            "date": "2026-09-13T17:00Z",
            "status": {
                "type": {"state": "in"},
                "period": 2,
                "displayClock": "4:12",
            },
            "competitions": [{
                "competitors": [
                    {"homeAway": "home", "team": {"abbreviation": "LAR"}},
                    {"homeAway": "away", "team": {"abbreviation": "WSH"}},
                ],
            }],
        },
        {
            "id": "401872658",
            "date": "2026-09-13T17:00Z",
            "status": {
                "type": {"state": "pre"},
                "period": 0,
                "displayClock": "0:00",
            },
            # No "competitions" at all -- the malformed event this fixture exists for.
        },
    ],
}


# --- parsing ----------------------------------------------------------------


def test_parse_scoreboard_skips_the_malformed_event():
    rows = espn_client._parse_scoreboard(FIXTURE_PAYLOAD)
    assert len(rows) == 2, "the event with no competitions is dropped, not raised"


def test_parse_scoreboard_reads_a_pregame_event():
    pre = espn_client._parse_scoreboard(FIXTURE_PAYLOAD)[0]
    assert pre == {
        "game_id": 401872656,
        "home_team": "NE",
        "away_team": "SEA",
        "kickoff_utc": "2026-09-10T00:20:00+00:00",
        "state": "pre",
        "period": 0,
        "clock": "0:00",
    }


def test_parse_scoreboard_reads_a_live_event():
    live = espn_client._parse_scoreboard(FIXTURE_PAYLOAD)[1]
    assert live["home_team"] == "LAR" and live["away_team"] == "WSH"
    assert live["state"] == "in"
    assert live["period"] == 2
    assert live["clock"] == "4:12"


# --- kickoff filter -----------------------------------------------------
# Both timestamps are UTC, straight from the user: the day comes from the
# NFL's own clock (America/New_York), the time from the reader's
# (Europe/Budapest) -- and they land on different calendar days here.
# _kickoff_label reads the reader's timezone off get_config(), so the display
# timezone is faked here rather than depending on a real config.yml.


class _FakeConfig:
    display_tz = ZoneInfo("Europe/Budapest")


def _with_budapest_display(fn):
    original = main.get_config
    main.get_config = lambda: _FakeConfig()
    try:
        return fn()
    finally:
        main.get_config = original


def test_kickoff_label_thursday_night():
    assert _with_budapest_display(
        lambda: main._kickoff_label("2026-09-11T00:35:00+00:00")
    ) == "Th 2:35"


def test_kickoff_label_sunday_afternoon():
    assert _with_budapest_display(
        lambda: main._kickoff_label("2026-09-13T17:00:00+00:00")
    ) == "Su 19:00"


def test_kickoff_label_blank_on_no_value():
    assert main._kickoff_label(None) == ""
    assert main._kickoff_label("") == ""


def test_kickoff_label_blank_on_bad_timestamp():
    """A bad timestamp must not 500 a page."""
    assert main._kickoff_label("not-a-timestamp") == ""


# --- abbreviation join --------------------------------------------------


def test_every_fixture_team_has_a_colour():
    """The game/player join is by abbreviation and fails silently on a
    mismatch, so every team this fixture uses must resolve to a colour."""
    rows = espn_client._parse_scoreboard(FIXTURE_PAYLOAD)
    teams = {row["home_team"] for row in rows} | {row["away_team"] for row in rows}
    assert teams == {"NE", "SEA", "LAR", "WSH"}
    assert teams <= set(PRO_TEAM_COLORS)


# --- storage round trip --------------------------------------------------


def _fresh(path="/tmp/_nflgames_test.db"):
    if os.path.exists(path):
        os.remove(path)
    c = db.connect(path)
    db.init_db(c)
    return c


def test_replace_then_fetch_round_trips():
    c = _fresh()
    rows = espn_client._parse_scoreboard(FIXTURE_PAYLOAD)
    written = db.replace_nfl_games(c, 2026, 1, rows)
    assert written == 2
    stored = db.fetch_nfl_games(c, 2026, 1)
    assert len(stored) == 2
    by_id = {r["game_id"]: r for r in stored}
    assert by_id[401872656]["home_team"] == "NE"
    assert by_id[401872656]["away_team"] == "SEA"
    assert by_id[401872657]["state"] == "in"
    c.close()


def test_a_second_replace_swaps_rather_than_duplicates():
    c = _fresh()
    rows = espn_client._parse_scoreboard(FIXTURE_PAYLOAD)
    db.replace_nfl_games(c, 2026, 1, rows)
    updated = [{**rows[0], "state": "post", "period": 4, "clock": "0:00"}]
    db.replace_nfl_games(c, 2026, 1, updated)
    stored = db.fetch_nfl_games(c, 2026, 1)
    assert len(stored) == 1, "the second write replaces the week wholesale"
    assert stored[0]["state"] == "post"
    c.close()

"""NFL usage analytics against a hand-computed fixture.

Same reasoning as tests/test_analytics.py: every number this module produces
looks plausible on the page whether or not it is correct, so the expected values
below are worked out by hand rather than captured from a run.

One receiver, three weeks: 10.0, 20.0, 30.0 PPR.
  mean 20.0 | floor 10 | ceiling 30 | pstdev sqrt(200/3) = 8.16
  boom (>= 1.5 x 20 = 30): the 30 -> 1/3 | bust (<= 0.5 x 20 = 10): the 10 -> 1/3
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import nflstats


def week(n, points, **extra):
    row = {"week": n, "gsis_id": "00-1", "fantasy_points_ppr": points,
           "offense_snaps": 40, "offense_pct": 0.8, "defense_pct": None,
           "targets": 8, "target_share": 0.25, "carries": 2, "receptions": 5}
    row.update(extra)
    return row


WEEKS = [week(1, 10.0), week(2, 20.0), week(3, 30.0)]


# --- one player's season --------------------------------------------------

def test_player_summary_hand_computed():
    s = nflstats.player_summary(WEEKS, "WR")
    assert s["games"] == 3
    assert s["ppr"] == 60.0 and s["ppr_avg"] == 20.0
    assert s["floor"] == 10.0 and s["ceiling"] == 30.0
    assert s["stdev"] == 8.16
    assert s["boom_rate"] == 0.33 and s["bust_rate"] == 0.33
    assert s["touches"] == 21.0        # 3 x (2 carries + 5 receptions)
    assert s["targets"] == 24.0
    assert s["snap_share"] == 0.8


def test_boom_and_bust_are_relative_to_the_player():
    """A fixed points threshold would call every kicker consistent and every
    workhorse back volatile; the cut-off has to scale with the player."""
    steady = [week(n, 20.0) for n in (1, 2, 3)]
    assert nflstats.player_summary(steady, "WR")["boom_rate"] == 0.0
    assert nflstats.player_summary(steady, "WR")["stdev"] == 0.0

    spiky = [week(1, 2.0), week(2, 2.0), week(3, 50.0)]
    assert nflstats.player_summary(spiky, "WR")["boom_rate"] == pytest.approx(0.33, abs=0.01)


def test_snap_share_reads_the_right_side_of_the_ball():
    """A linebacker plays no offensive snaps. Reading offense_pct for them would
    report the league's IDP starters as barely playing."""
    idp = [week(1, 12.0, offense_pct=0.0, defense_pct=0.97)]
    assert nflstats.player_summary(idp, "LB")["snap_share"] == 0.97
    assert nflstats.player_summary(idp, "WR")["snap_share"] == 0.0


def test_trend_compares_recent_form_to_the_season():
    """Last two of 10/20/30 average 25, which is +5 on the season's 20."""
    assert nflstats.player_summary(WEEKS, "WR", recent=2)["trend"] == 5.0


def test_a_player_with_no_weeks_does_not_divide_by_zero():
    s = nflstats.player_summary([], "WR")
    assert s["games"] == 0 and s["ppr_avg"] is None and s["stdev"] is None
    assert s["boom_rate"] is None and s["trend"] is None


def test_missing_values_are_skipped_not_counted_as_zero():
    """nflverse leaves target_share empty for players with no route run; folding
    those in as 0.0 would quietly halve a receiver's average share."""
    rows = [week(1, 10.0, target_share=0.4), week(2, 10.0, target_share=None)]
    assert nflstats.player_summary(rows, "WR")["target_share"] == 0.4


# --- team defences --------------------------------------------------------

def test_team_defence_reports_no_fantasy_points():
    """The team-week file's fantasy_points belong to the OFFENCE. Showing them
    beside a D/ST would be a confidently wrong number."""
    rows = [{"week": 1, "def_sacks": 3, "def_interceptions": 1, "def_tds": 1,
             "special_teams_tds": 0, "fantasy_points_ppr": 400.0}]
    summary = nflstats.team_defence_summary(rows)
    assert "ppr" not in summary and "fantasy_points_ppr" not in summary
    assert summary["sacks"] == 3.0 and summary["defensive_tds"] == 1.0


# --- joining a roster -----------------------------------------------------

ROSTER = [
    {"team_id": 3, "player_id": 111, "name": "A Receiver", "position": "WR",
     "lineup_slot": "WR", "pro_team": "DET"},
    {"team_id": 3, "player_id": 999, "name": "Unknown Guy", "position": "WR",
     "lineup_slot": "WR", "pro_team": "KC"},
    {"team_id": 3, "player_id": -16008, "name": "Lions D/ST", "position": "D/ST",
     "lineup_slot": "D/ST", "pro_team": "DET"},
]
ID_MAP = {111: {"gsis_id": "00-1", "pfr_id": "Aa00", "position": "WR"}}


def test_usage_rows_keeps_unmatched_players_visible():
    """Dropping them would make a 16-man roster render as 15, which reads as a
    bug rather than as missing NFL data."""
    rows = nflstats.usage_rows(ROSTER, ID_MAP, WEEKS,
                               [{"week": 1, "team": "DET", "def_sacks": 2}])
    by_name = {r["name"]: r for r in rows}
    assert len(rows) == 3
    assert by_name["A Receiver"]["matched"] is True
    assert by_name["Unknown Guy"]["matched"] is False
    assert by_name["Unknown Guy"]["ppr_avg"] is None


def test_team_defences_join_on_the_team_not_the_player_id():
    """A D/ST has no espn_id in any NFL dataset -- 100% of unmatched roster rows
    are team defences -- so they must route through the abbreviation."""
    rows = nflstats.usage_rows(ROSTER, ID_MAP, WEEKS,
                               [{"week": 1, "team": "DET", "def_sacks": 2}])
    dst = next(r for r in rows if r["is_defence"])
    assert dst["matched"] is True and dst["sacks"] == 2.0


def test_split_by_unit_separates_offence_idp_and_defence():
    roster = ROSTER + [{"team_id": 3, "player_id": 222, "name": "A Backer",
                        "position": "LB", "lineup_slot": "LB", "pro_team": "GB"}]
    groups = nflstats.split_by_unit(nflstats.usage_rows(roster, ID_MAP, WEEKS, []))
    assert [r["name"] for r in groups["idp"]] == ["A Backer"]
    assert [r["name"] for r in groups["dst"]] == ["Lions D/ST"]
    assert {r["name"] for r in groups["offence"]} == {"A Receiver", "Unknown Guy"}


def test_franchise_usage_rolls_up_per_team():
    rows = nflstats.usage_rows(ROSTER, ID_MAP, WEEKS, [])
    summary = nflstats.franchise_usage(rows)[3]
    assert summary["players"] == 3 and summary["matched"] == 1
    assert summary["ppr"] == 60.0

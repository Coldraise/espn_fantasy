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


# --- matchup ratings --------------------------------------------------------
#
# Four teams, two weeks, A-B and C-D playing each other both weeks -- so every
# team's `games` is 2 and every per-game figure below is "sum of the two weeks
# divided by 2". team_week() rows carry each team's own offence (ints,
# rush/pass yards) and own defence (sacks, fumbles, tackles); defense_ratings
# credits the DEFENCE by reading a row's `opponent`, not its `team`, for
# everything except thrown interceptions.

def team_week(team, opponent, week, ints=0, sacks=0, fumbles=0, tackles=0,
             rush_yds=0, pass_yds=0):
    return {"team": team, "opponent": opponent, "week": week,
            "passing_interceptions": ints, "def_sacks": sacks,
            "def_fumbles_forced": fumbles, "def_tackles_solo": tackles,
            "rushing_yards": rush_yds, "passing_yards": pass_yds}


TEAM_WEEKS = [
    team_week("A", "B", 1, ints=1, sacks=2, fumbles=1, tackles=40, rush_yds=100, pass_yds=200),
    team_week("A", "B", 2, ints=2, sacks=3, fumbles=0, tackles=44, rush_yds=90, pass_yds=210),
    team_week("B", "A", 1, ints=0, sacks=1, fumbles=2, tackles=50, rush_yds=80, pass_yds=180),
    team_week("B", "A", 2, ints=1, sacks=2, fumbles=1, tackles=46, rush_yds=95, pass_yds=190),
    team_week("C", "D", 1, ints=3, sacks=0, fumbles=0, tackles=60, rush_yds=150, pass_yds=250),
    team_week("C", "D", 2, ints=2, sacks=1, fumbles=1, tackles=55, rush_yds=140, pass_yds=260),
    team_week("D", "C", 1, ints=0, sacks=4, fumbles=3, tackles=30, rush_yds=60, pass_yds=100),
    team_week("D", "C", 2, ints=1, sacks=3, fumbles=2, tackles=35, rush_yds=70, pass_yds=110),
]

# defense_ratings does not itself count weeks in player_weeks -- only
# team_weeks sets the games denominator -- so each row below is the season
# total against that opponent rather than a separate line per week.
def pweek(gsis_id, opponent, position, points, **extra):
    row = {"gsis_id": gsis_id, "opponent": opponent, "position": position,
           "fantasy_points_ppr": points, "week": 1}
    row.update(extra)
    return row


PLAYER_WEEKS = [
    # opponent=A faces an RB and an FB (run_pts) and a QB, WR and TE (pass_pts).
    pweek("rb-a", "A", "RB", 6.0), pweek("fb-a", "A", "FB", 4.0),
    pweek("qb-a", "A", "QB", 20.0), pweek("wr-a", "A", "WR", 15.0), pweek("te-a", "A", "TE", 5.0),
    pweek("rb-b", "B", "RB", 20.0), pweek("qb-b", "B", "QB", 30.0),
    pweek("rb-c", "C", "RB", 30.0), pweek("qb-c", "C", "QB", 50.0),
    pweek("rb-d", "D", "RB", 6.0), pweek("qb-d", "D", "QB", 10.0),
    # Every kicker row carries fantasy_points_ppr = 0.0, same as nflverse's own
    # files -- the regression under test is that kick_pts is rebuilt from
    # fg_made/pat_made anyway, not read off that zeroed column.
    pweek("k-a", "A", "K", 0.0, fg_made=2, pat_made=1),
    pweek("k-b", "B", "K", 0.0, fg_made=4, pat_made=2),
    pweek("k-c", "C", "K", 0.0, fg_made=6, pat_made=1),
    pweek("k-d", "D", "K", 0.0, fg_made=1, pat_made=2),
]


def test_defense_ratings_counts_games_from_team_weeks():
    ratings = nflstats.defense_ratings(TEAM_WEEKS, PLAYER_WEEKS)
    assert {team: r["games"] for team, r in ratings.items()} == {"A": 2, "B": 2, "C": 2, "D": 2}


def test_kick_pts_is_rebuilt_from_made_kicks_not_fantasy_points():
    """fg_made=2, pat_made=1 against A is 3*2+1=7 across two games -> 3.5/game,
    even though fantasy_points_ppr is 0.0 on every row that feeds it."""
    ratings = nflstats.defense_ratings(TEAM_WEEKS, PLAYER_WEEKS)
    assert ratings["A"]["kick_pts"] == 3.5
    assert ratings["B"]["kick_pts"] == 7.0   # (3*4 + 2) / 2 games


def test_run_and_pass_pts_bucket_by_position():
    """Against A: an RB (6.0) and FB (4.0) sum to 10.0 -> 5.0/game of run_pts;
    a QB (20.0), WR (15.0) and TE (5.0) sum to 40.0 -> 20.0/game of pass_pts."""
    ratings = nflstats.defense_ratings(TEAM_WEEKS, PLAYER_WEEKS)
    assert ratings["A"]["run_pts"] == 5.0
    assert ratings["A"]["pass_pts"] == 20.0


def test_rank_desc_ties_share_the_better_rank():
    ranks = nflstats._rank_desc({"A": 10, "B": 8, "C": 8, "D": 5})
    assert ranks == {"A": 1, "B": 2, "C": 2, "D": 4}


def test_def_rank_orders_by_the_average_of_four_component_ranks():
    """Per-game ints/sacks/fumbles/tackles conceded, each ranked 1-4 (highest =
    most generous = rank 1), then averaged and re-ranked lowest-average-first:

    ints_pg    A 1.5  B 0.5  C 2.5  D 0.5   -> rank A2 B3 C1 D3
    sacks_pg   A 1.5  B 2.5  C 3.5  D 0.5   -> rank A3 B2 C1 D4
    fumbles_pg A 1.5  B 0.5  C 2.5  D 0.5   -> rank A2 B3 C1 D3
    tackles_pg A 48.0 B 42.0 C 32.5 D 57.5  -> rank A2 B3 C4 D1

    average    A 2.25       B 2.75        C 1.75        D 2.75
    def_rank:  C 1 (lowest avg), A 2, B and D tied for 3.
    """
    ratings = nflstats.defense_ratings(TEAM_WEEKS, PLAYER_WEEKS)
    assert ratings["C"]["def_rank"] == 1
    assert ratings["A"]["def_rank"] == 2
    assert ratings["B"]["def_rank"] == 3 and ratings["D"]["def_rank"] == 3


def test_opponent_map_covers_both_sides_and_omits_a_bye():
    games = [{"home_team": "A", "away_team": "B"}, {"home_team": "C", "away_team": "D"}]
    # E is on the bye this week -- absent from the schedule, not present with a
    # null opponent -- and must come out the same way here.
    opponents = nflstats.opponent_map(games)
    assert opponents["A"] == {"opponent": "B", "home": True}
    assert opponents["B"] == {"opponent": "A", "home": False}
    assert "E" not in opponents


def test_attach_matchups_sets_none_for_a_bye_or_unsynced_team():
    opponents = nflstats.opponent_map([{"home_team": "A", "away_team": "B"}])
    ratings = nflstats.defense_ratings(TEAM_WEEKS, PLAYER_WEEKS)
    rows = [{"pro_team": "A"}, {"pro_team": "E"}, {"pro_team": "FA"}]
    nflstats.attach_matchups(rows, ratings, opponents, "run")
    assert rows[0]["matchup"]["opponent"] == "B" and rows[0]["matchup"]["axis"] == "run"
    assert rows[1]["matchup"] is None
    assert rows[2]["matchup"] is None

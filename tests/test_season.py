"""Season-state, all-time aggregation, and the pre-draft guards.

These cover the failure modes found by probing the live league: box_scores
crashing before a draft, future weeks being written with current-week data, and
224 empty placeholder picks looking like a completed draft.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analytics, db, poller


# --- season state ---------------------------------------------------------

def test_state_pre_draft():
    # the live league on 2026-08-31: scoringPeriodId 0, draft not held
    assert poller.season_state(0, 17, drafted=False, has_scores=False) == poller.PRE_DRAFT


def test_state_drafted_but_no_games():
    assert poller.season_state(0, 17, drafted=True, has_scores=False) == poller.DRAFTED
    assert poller.season_state(1, 17, drafted=True, has_scores=False) == poller.DRAFTED


def test_state_in_season():
    assert poller.season_state(5, 17, drafted=True, has_scores=True) == poller.IN_SEASON


def test_state_complete_only_past_final():
    """Week 17 of 17 is still in season; 18 is over."""
    assert poller.season_state(17, 17, drafted=True, has_scores=True) == poller.IN_SEASON
    assert poller.season_state(18, 17, drafted=True, has_scores=True) == poller.COMPLETE


def test_undrafted_beats_every_other_signal():
    assert poller.season_state(9, 17, drafted=False, has_scores=True) == poller.PRE_DRAFT


def test_state_in_season_once_games_kick_off():
    # the week-1 case: every stored point is still 0.0 because scoreboard does
    # not settle a matchup period's totals until it closes
    assert poller.season_state(1, 17, drafted=True, has_scores=False,
                               games_started=True) == poller.IN_SEASON


def test_state_drafted_before_kickoff():
    assert poller.season_state(1, 17, drafted=True, has_scores=False,
                               games_started=False) == poller.DRAFTED


def test_undrafted_beats_kickoff():
    assert poller.season_state(1, 17, drafted=False, has_scores=True,
                               games_started=True) == poller.PRE_DRAFT


# --- storage guards -------------------------------------------------------

def _conn(tmp="/tmp/_season_test.db"):
    if os.path.exists(tmp):
        os.remove(tmp)
    c = db.connect(tmp)
    db.init_db(c)
    return c


def test_team_season_snapshot_and_revise():
    c = _conn()
    args = (2025, 1, "gojzi", 5, 10, 0, 2400.5, 2629.6, 8, 0)
    assert db.upsert_team_season(c, *args) is True
    assert db.upsert_team_season(c, *args) is False          # identical -> no-op
    revised = (2025, 1, "gojzi", 6, 9, 0, 2400.5, 2629.6, 8, 0)
    assert db.upsert_team_season(c, *revised) is True        # restated -> bump
    row = c.execute("SELECT wins, revision FROM team_seasons").fetchone()
    assert (row["wins"], row["revision"]) == (6, 2)
    c.close()


def test_franchise_names_tracks_renames():
    """A renamed franchise stays one franchise, labelled with its newest name."""
    c = _conn()
    db.upsert_team_season(c, 2019, 3, "MakerBayfieldTraktorklub", 7, 7, 0, 2174.5, 2100.0, 5, 0)
    db.upsert_team_season(c, 2025, 3, "MaDMAxKlein", 4, 11, 0, 2121.2, 2300.0, 7, 0)
    names = db.franchise_names(c)
    assert names[3]["name"] == "MaDMAxKlein"
    assert names[3]["former"] == ["MakerBayfieldTraktorklub"]
    c.close()


def test_has_week_data_false_for_unplayed_schedule():
    """A pre-season schedule is stored with 0.0 points and must not count."""
    c = _conn()
    for week in range(1, 16):
        db.upsert_team_week(c, 2026, week, 1, 0.0, None, 2, False, None)
    assert db.has_week_data(c) is False
    db.upsert_team_week(c, 2026, 1, 1, 98.5, None, 2, False, "W")
    assert db.has_week_data(c) is True
    c.close()


# --- all-time aggregation -------------------------------------------------

SEASONS = [
    {"season": 2024, "team_id": 1, "name": "Alpha", "wins": 10, "losses": 5, "ties": 0,
     "points_for": 1500.0, "points_against": 1400.0, "final_rank": 1, "playoff_seed": 1},
    {"season": 2025, "team_id": 1, "name": "Alpha", "wins": 8, "losses": 7, "ties": 0,
     "points_for": 1450.0, "points_against": 1420.0, "final_rank": 3, "playoff_seed": 3},
    {"season": 2024, "team_id": 2, "name": "Old Name", "wins": 5, "losses": 10, "ties": 0,
     "points_for": 1300.0, "points_against": 1500.0, "final_rank": 8, "playoff_seed": 0},
    {"season": 2025, "team_id": 2, "name": "New Name", "wins": 7, "losses": 8, "ties": 0,
     "points_for": 1380.0, "points_against": 1410.0, "final_rank": 5, "playoff_seed": 0},
]
NAMES = {1: {"name": "Alpha", "former": []},
         2: {"name": "New Name", "former": ["Old Name"]}}


def test_all_time_aggregates_by_franchise_not_name():
    table = analytics.all_time_standings(SEASONS, NAMES)
    assert len(table) == 2, "a rename must not split a franchise"
    renamed = next(t for t in table if t["team_id"] == 2)
    assert renamed["wins"] == 12 and renamed["losses"] == 18
    assert renamed["name"] == "New Name" and renamed["former_names"] == ["Old Name"]


def test_all_time_totals_match_season_sums():
    table = analytics.all_time_standings(SEASONS, NAMES)
    assert sum(t["wins"] for t in table) == sum(s["wins"] for s in SEASONS)
    assert sum(t["losses"] for t in table) == sum(s["losses"] for s in SEASONS)


def test_all_time_titles_and_finishes():
    alpha = next(t for t in analytics.all_time_standings(SEASONS, NAMES) if t["team_id"] == 1)
    assert alpha["titles"] == 1
    assert alpha["seasons"] == 2
    assert alpha["best_finish"] == 1 and alpha["best_finish_season"] == 2024
    assert alpha["worst_finish"] == 3
    assert alpha["playoff_appearances"] == 2
    assert alpha["pct"] == round(18 / 30, 3)


def test_all_time_skips_seasons_without_records():
    """Seasons ESPN returns as empty shells must not dilute the averages."""
    rows = SEASONS + [{"season": 2019, "team_id": 1, "name": "Alpha", "wins": None,
                       "losses": None, "ties": None, "points_for": None,
                       "points_against": None, "final_rank": None, "playoff_seed": None}]
    alpha = next(t for t in analytics.all_time_standings(rows, NAMES) if t["team_id"] == 1)
    assert alpha["seasons"] == 2


def test_season_grid_shape():
    grid = analytics.season_grid(SEASONS, NAMES)
    assert grid["seasons"] == [2025, 2024]
    assert len(grid["rows"]) == 2
    alpha = next(r for r in grid["rows"] if r["team_id"] == 1)
    assert alpha["by_season"][2024]["record"] == "10-5"
    assert alpha["by_season"][2024]["rank"] == 1


# --- franchise slots that changed hands -----------------------------------

HANDOVER = [
    {"season": 2023, "team_id": 5, "name": "OldOwner", "wins": 10, "losses": 5, "ties": 0,
     "points_for": 1600.0, "points_against": 1400.0, "final_rank": 1, "playoff_seed": 1},
    {"season": 2024, "team_id": 5, "name": "OldOwner", "wins": 7, "losses": 8, "ties": 0,
     "points_for": 1500.0, "points_against": 1520.0, "final_rank": 5, "playoff_seed": 0},
    {"season": 2025, "team_id": 5, "name": "NewOwner", "wins": 7, "losses": 8, "ties": 0,
     "points_for": 1580.0, "points_against": 1600.0, "final_rank": 6, "playoff_seed": 0},
    {"season": 2025, "team_id": 1, "name": "Untouched", "wins": 9, "losses": 6, "ties": 0,
     "points_for": 1700.0, "points_against": 1500.0, "final_rank": 2, "playoff_seed": 2},
]


def test_filter_drops_only_the_previous_owners_seasons():
    kept = analytics.filter_franchise_history(HANDOVER, {5: 2025})
    assert [(r["season"], r["team_id"]) for r in kept] == [(2025, 5), (2025, 1)]


def test_filter_is_a_noop_without_config():
    assert analytics.filter_franchise_history(HANDOVER, None) == HANDOVER
    assert analytics.filter_franchise_history(HANDOVER, {}) == HANDOVER


def test_handover_removes_the_previous_owners_title():
    """The pre-handover championship belongs to the old owner, not this one."""
    before = {t["team_id"]: t for t in analytics.all_time_standings(HANDOVER)}
    after = {t["team_id"]: t for t in analytics.all_time_standings(
        analytics.filter_franchise_history(HANDOVER, {5: 2025}))}
    assert before[5]["titles"] == 1 and before[5]["seasons"] == 3
    assert after[5]["titles"] == 0 and after[5]["seasons"] == 1
    assert after[5]["wins"] == 7 and after[5]["losses"] == 8
    assert after[1] == before[1], "other franchises are untouched"


def test_handover_hides_the_previous_name():
    c = _conn()
    db.upsert_team_season(c, 2024, 5, "Kanmalacok", 7, 8, 0, 2532.9, 2500.0, 5, 0)
    db.upsert_team_season(c, 2025, 5, "Juhu", 7, 8, 0, 2581.1, 2600.0, 6, 0)
    assert db.franchise_names(c)[5]["former"] == ["Kanmalacok"]
    scoped = db.franchise_names(c, {5: 2025})[5]
    assert scoped["name"] == "Juhu"
    assert scoped["former"] == [], "a previous owner's name is not a former name"
    c.close()

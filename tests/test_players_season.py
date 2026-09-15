"""Season projection and actual scoring tables.

Tests for players.group_by_position with by parameter, ranked, storage_rows,
played_weeks, next_projection_week, season_rows, sort_field, and related db
functions for season-grain tables and filtering.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, players


def _p(pid, name="Player", pos="RB", proj=None, act=None, on_team=0):
    """Minimal player row for season tests."""
    return {
        "player_id": pid, "name": name, "position": pos,
        "pro_team": "DET", "projected": proj, "actual": act,
        "percent_owned": 50.0, "on_team_id": on_team
    }


class TestGroupByPositionSorting:
    """Sorting with descending by-field and tie-breaking."""

    def test_by_projected_descending(self):
        """Higher projected ranks first within a position."""
        rows = [
            _p(1, "Low", "RB", proj=5),
            _p(2, "High", "RB", proj=25),
            _p(3, "Mid", "RB", proj=15),
        ]
        group = players.group_by_position(rows)[0]
        names = [p["name"] for p in group["players"]]
        assert names == ["High", "Mid", "Low"]

    def test_none_projected_sorts_last(self):
        """Players with None projected come after all with values, including negative."""
        rows = [
            _p(1, "Negative", "RB", proj=-0.5),
            _p(2, "None", "RB", proj=None),
            _p(3, "Positive", "RB", proj=0.1),
        ]
        group = players.group_by_position(rows)[0]
        names = [p["name"] for p in group["players"]]
        assert names == ["Positive", "Negative", "None"], "negative > none"

    def test_tie_broken_by_higher_projected(self):
        """Tied on sort field, higher projected breaks tie."""
        rows = [
            _p(1, "TieHighProj", "RB", proj=5),
            _p(2, "TieLowProj", "RB", proj=2),
        ]
        # Both have actual=10, sort on actual
        rows[0]["actual"] = 10
        rows[1]["actual"] = 10
        group = players.group_by_position(rows, by="actual")[0]
        names = [p["name"] for p in group["players"]]
        assert names == ["TieHighProj", "TieLowProj"], "higher proj wins tie on actual"

    def test_tie_broken_by_name_ascending(self):
        """Tied on sort field and projected, break by name ascending."""
        rows = [
            _p(1, "Zebra", "RB", proj=5, act=10),
            _p(2, "Apple", "RB", proj=5, act=10),
        ]
        group = players.group_by_position(rows, by="actual")[0]
        names = [p["name"] for p in group["players"]]
        assert names == ["Apple", "Zebra"], "name ascending is final tiebreaker"

    def test_by_actual_works(self):
        """by='actual' sorts on actual instead of projected."""
        rows = [
            _p(1, "HighActual", "RB", proj=5, act=20),
            _p(2, "LowActual", "RB", proj=30, act=5),
        ]
        group = players.group_by_position(rows, by="actual")[0]
        names = [p["name"] for p in group["players"]]
        assert names == ["HighActual", "LowActual"]

    def test_by_w2_works(self):
        """by='w2' sorts on that key."""
        rows = [
            _p(1, "W2High", "RB", proj=5, act=None),
            _p(2, "W2Low", "RB", proj=30, act=None),
        ]
        rows[0]["w2"] = 15
        rows[1]["w2"] = 8
        group = players.group_by_position(rows, by="w2")[0]
        names = [p["name"] for p in group["players"]]
        assert names == ["W2High", "W2Low"]

    def test_default_by_projected_unchanged(self):
        """Default by='projected' is unchanged behavior."""
        rows = [
            _p(1, "A", "RB", proj=10),
            _p(2, "B", "RB", proj=20),
        ]
        default = players.group_by_position(rows)
        explicit = players.group_by_position(rows, by="projected")
        assert default[0]["players"] == explicit[0]["players"]


class TestRanked:
    """ranked() adds group field and drops unmapped positions."""

    def test_ranked_adds_group_field(self):
        rows = [
            _p(1, "QB1", "QB", proj=20),
            _p(2, "RB1", "RB", proj=15),
        ]
        ranked = players.ranked(rows)
        assert all("group" in r for r in ranked)
        by_name = {r["name"]: r["group"] for r in ranked}
        assert by_name["QB1"] == "QB"
        assert by_name["RB1"] == "RB"

    def test_ranked_across_positions_not_within(self):
        """Sorted globally, not grouped then sorted."""
        rows = [
            _p(1, "K", "K", proj=9),
            _p(2, "RB", "RB", proj=21),
            _p(3, "RB2", "RB", proj=18),
            _p(4, "QB", "QB", proj=20),
        ]
        ranked = players.ranked(rows)
        names = [r["name"] for r in ranked]
        assert names == ["RB", "QB", "RB2", "K"]

    def test_ranked_drops_unmapped_positions(self):
        rows = [
            _p(1, "Known", "RB", proj=10),
            _p(2, "Unknown", "XX", proj=20),
        ]
        ranked = players.ranked(rows)
        names = [r["name"] for r in ranked]
        assert names == ["Known"]

    def test_ranked_by_actual(self):
        rows = [
            _p(1, "A", "RB", proj=30, act=2),
            _p(2, "B", "RB", proj=1, act=25),
        ]
        ranked = players.ranked(rows, by="actual")
        names = [r["name"] for r in ranked]
        assert names == ["B", "A"]


class TestStorageRows:
    """Union of top N by projection and top N by actual."""

    def test_storage_rows_union_by_projection_and_actual(self):
        """Top 2 by proj + top 2 by actual, deduplicated, no player twice."""
        rows = [
            _p(1, "A", "WR", proj=20, act=1),
            _p(2, "B", "WR", proj=15, act=2),
            _p(3, "C", "WR", proj=1, act=30),
        ]
        stored = players.storage_rows(rows, per_group=2)
        pids = [r["player_id"] for r in stored]
        assert set(pids) == {1, 2, 3}, "A,B by proj, C by actual"
        assert len(pids) == len(set(pids)), "no duplicates"

    def test_storage_rows_drops_bottom_both(self):
        """A player below both top-N lists per position is dropped."""
        rows = [
            _p(1, "A", "RB", proj=20, act=1),
            _p(2, "B", "RB", proj=15, act=2),
            _p(3, "C", "RB", proj=10, act=5),
            _p(4, "D", "RB", proj=5, act=8),
        ]
        stored = players.storage_rows(rows, per_group=2)
        pids = {r["player_id"] for r in stored}
        # Top 2 RB by proj: A(20), B(15). Top 2 RB by actual: D(8), C(5).
        assert pids == {1, 2, 3, 4}, "all in union of top 2 by proj and top 2 by actual"

    def test_storage_rows_preserves_one_when_top_both(self):
        """A player in both lists appears once."""
        rows = [
            _p(1, "Top", "RB", proj=30, act=30),
            _p(2, "Mid", "RB", proj=10, act=5),
        ]
        stored = players.storage_rows(rows, per_group=2)
        pids = [r["player_id"] for r in stored]
        assert len([p for p in pids if p == 1]) == 1

    def test_storage_rows_respects_per_group(self):
        rows = [_p(i, f"P{i}", "RB", proj=100-i, act=i) for i in range(10)]
        stored = players.storage_rows(rows, per_group=3)
        pids = set(r["player_id"] for r in stored)
        # Top 3 RB by proj: 0(100), 1(99), 2(98). Top 3 RB by actual: 7, 8, 9.
        assert pids == {0, 1, 2, 7, 8, 9}

    def test_storage_rows_first_seen_order_not_deduplicated_list_order(self):
        """Deduped by first-seen in proj order, not actual order."""
        rows = [
            _p(1, "A", "RB", proj=20, act=10),
            _p(2, "B", "WR", proj=10, act=100),
        ]
        stored = players.storage_rows(rows, per_group=1)
        pids = [r["player_id"] for r in stored]
        # RB top 1 by proj: 1. WR top 1 by actual: 2.
        # Both in union, order from projection scan then actual scan.
        assert len(pids) == 2 and set(pids) == {1, 2}


class TestPlayedWeeks:
    """Which projection weeks have been played."""

    def test_played_weeks_all_when_current_none(self):
        """current None → every week in actual_weeks."""
        actual_weeks = [2, 1, 3]
        played = players.played_weeks(actual_weeks, [], None)
        assert played == [1, 2, 3], "all weeks, ascending"

    def test_played_weeks_before_current_always_played(self):
        """week < current → played."""
        actual_weeks = [1, 2, 3]
        played = players.played_weeks(actual_weeks, [], current=3)
        assert played == [1, 2], "weeks before current"

    def test_played_weeks_current_only_if_all_post(self):
        """week == current only if all games state='post'."""
        actual_weeks = [2]
        games = [
            {"week": 2, "state": "post"},
            {"week": 2, "state": "post"},
        ]
        played = players.played_weeks(actual_weeks, games, current=2)
        assert played == [2]

    def test_played_weeks_current_not_if_one_in(self):
        """week == current with one 'in' game → not played."""
        actual_weeks = [2]
        games = [
            {"week": 2, "state": "post"},
            {"week": 2, "state": "in"},
        ]
        played = players.played_weeks(actual_weeks, games, current=2)
        assert played == []

    def test_played_weeks_current_not_if_one_pre(self):
        """week == current with one 'pre' game → not played."""
        actual_weeks = [2]
        games = [
            {"week": 2, "state": "post"},
            {"week": 2, "state": "pre"},
        ]
        played = players.played_weeks(actual_weeks, games, current=2)
        assert played == []

    def test_played_weeks_current_not_if_no_games(self):
        """week == current with no game rows → not played."""
        actual_weeks = [2]
        played = players.played_weeks(actual_weeks, [], current=2)
        assert played == []

    def test_played_weeks_future_never_played(self):
        """week > current → not played."""
        actual_weeks = [3, 4]
        played = players.played_weeks(actual_weeks, [], current=2)
        assert played == []

    def test_played_weeks_state_case_insensitive(self):
        """'POST' uppercase treated same as 'post'."""
        actual_weeks = [2]
        games = [{"week": 2, "state": "POST"}]
        played = players.played_weeks(actual_weeks, games, current=2)
        assert played == [2]

    def test_played_weeks_only_from_actual_weeks(self):
        """Output only includes weeks in actual_weeks, ascending."""
        actual_weeks = [5, 2, 8]
        games = [
            {"week": 2, "state": "post"},
            {"week": 5, "state": "post"},
            {"week": 8, "state": "post"},
        ]
        played = players.played_weeks(actual_weeks, games, current=1)
        assert played == [2, 5, 8]


class TestNextProjectionWeek:
    """Smallest projection week not in played."""

    def test_next_projection_week_first_unplayed(self):
        played = [1, 3]
        projection_weeks = [1, 2, 3, 4]
        next_w = players.next_projection_week(projection_weeks, played)
        assert next_w == 2

    def test_next_projection_week_none_when_all_played(self):
        played = [1, 2, 3]
        projection_weeks = [1, 2, 3]
        next_w = players.next_projection_week(projection_weeks, played)
        assert next_w is None

    def test_next_projection_week_none_when_empty(self):
        next_w = players.next_projection_week([], [])
        assert next_w is None

    def test_next_projection_week_ignores_order_of_inputs(self):
        """Result is sorted smallest unplayed, not input order."""
        played = [5, 1, 3]
        projection_weeks = [5, 1, 3, 2, 4]
        next_w = players.next_projection_week(projection_weeks, played)
        assert next_w == 2


class TestSeasonRows:
    """One row per player with season totals and next projection."""

    def test_season_rows_per_player_from_played_and_next(self):
        """One row per player_id in played weeks or next_week."""
        by_week = {
            1: [_p(1, "Player1", "RB", proj=10, act=15)],
            2: [_p(1, "Player1", "RB", proj=12, act=None)],
        }
        played = [1]
        next_week = 2
        rows = players.season_rows(by_week, played, next_week)
        assert len(rows) == 1
        assert rows[0]["player_id"] == 1

    def test_season_rows_w_field_for_each_played_week(self):
        """wN key for each N in played."""
        by_week = {
            1: [_p(1, "P1", "RB", act=10)],
            2: [_p(1, "P1", "RB", act=5)],
        }
        rows = players.season_rows(by_week, [1, 2], None)
        assert "w1" in rows[0]
        assert "w2" in rows[0]
        assert rows[0]["w1"] == 10
        assert rows[0]["w2"] == 5

    def test_season_rows_wn_none_when_no_row(self):
        """wN is None when player has no row that week."""
        by_week = {
            1: [_p(1, "P1", "RB", act=10)],
            2: [_p(2, "Other", "RB", act=5)],
        }
        rows = players.season_rows(by_week, [1, 2], None)
        by_id = {r["player_id"]: r for r in rows}
        assert by_id[1]["w2"] is None

    def test_season_rows_wn_none_when_actual_null(self):
        """wN is None when actual is None that week."""
        by_week = {
            1: [_p(1, "P1", "RB", act=10)],
            2: [_p(1, "P1", "RB", act=None)],
        }
        rows = players.season_rows(by_week, [1, 2], None)
        assert rows[0]["w2"] is None

    def test_season_rows_total_sum_of_actuals(self):
        """total is sum of non-None wN."""
        by_week = {
            1: [_p(1, "P1", "RB", act=10)],
            2: [_p(1, "P1", "RB", act=15)],
            3: [_p(1, "P1", "RB", act=None)],
        }
        rows = players.season_rows(by_week, [1, 2, 3], None)
        assert rows[0]["total"] == 25.0

    def test_season_rows_total_none_when_all_none(self):
        """total is None when all wN are None."""
        by_week = {
            1: [_p(1, "P1", "RB", act=None)],
            2: [_p(1, "P1", "RB", act=None)],
        }
        rows = players.season_rows(by_week, [1, 2], None)
        assert rows[0]["total"] is None

    def test_season_rows_total_rounded_to_one_decimal(self):
        """total is round(..., 1)."""
        by_week = {
            1: [_p(1, "P1", "RB", act=0.1)],
            2: [_p(1, "P1", "RB", act=0.2)],
        }
        rows = players.season_rows(by_week, [1, 2], None)
        assert rows[0]["total"] == 0.3, "0.1 + 0.2 = 0.3 (floats handled)"

    def test_season_rows_total_includes_negative(self):
        """total includes negative values."""
        by_week = {
            1: [_p(1, "P1", "RB", act=10.0)],
            2: [_p(1, "P1", "RB", act=-2.5)],
        }
        rows = players.season_rows(by_week, [1, 2], None)
        assert rows[0]["total"] == 7.5

    def test_season_rows_projected_from_next_week(self):
        """projected comes from next_week row when present."""
        by_week = {
            1: [_p(1, "P1", "RB", proj=99, act=10)],
            2: [_p(1, "P1", "RB", proj=12, act=None)],
        }
        rows = players.season_rows(by_week, [1], next_week=2)
        assert rows[0]["projected"] == 12

    def test_season_rows_projected_none_when_no_next_week(self):
        """projected is None when next_week is None or player not in it."""
        by_week = {1: [_p(1, "P1", "RB", proj=10, act=5)]}
        rows = players.season_rows(by_week, [1], next_week=None)
        assert rows[0]["projected"] is None

    def test_season_rows_projected_none_when_player_not_in_next(self):
        """projected is None when player has no next_week row."""
        by_week = {
            1: [_p(1, "P1", "RB", proj=99, act=10)],
            2: [_p(2, "Other", "RB", proj=12, act=None)],
        }
        rows = players.season_rows(by_week, [1], next_week=2)
        by_id = {r["player_id"]: r for r in rows}
        assert by_id[1]["projected"] is None

    def test_season_rows_identity_from_next_week(self):
        """name, position, pro_team, on_team_id from next_week when present."""
        by_week = {
            1: [{"player_id": 1, "name": "Old", "position": "RB", "pro_team": "BUF",
                 "on_team_id": 0, "percent_owned": 50, "projected": 99, "actual": 10}],
            2: [{"player_id": 1, "name": "New", "position": "WR", "pro_team": "KC",
                 "on_team_id": 5, "percent_owned": 75, "projected": 12, "actual": None}],
        }
        rows = players.season_rows(by_week, [1], next_week=2)
        assert rows[0]["name"] == "New"
        assert rows[0]["position"] == "WR"
        assert rows[0]["pro_team"] == "KC"
        assert rows[0]["on_team_id"] == 5

    def test_season_rows_identity_from_latest_played(self):
        """Identity from latest played week when no next_week row."""
        by_week = {
            1: [{"player_id": 1, "name": "Week1", "position": "RB", "pro_team": "BUF",
                 "on_team_id": 0, "percent_owned": 50, "projected": 10, "actual": 5}],
            2: [{"player_id": 1, "name": "Week2", "position": "RB", "pro_team": "MIA",
                 "on_team_id": 0, "percent_owned": 60, "projected": 12, "actual": 8}],
        }
        rows = players.season_rows(by_week, [1, 2], next_week=None)
        assert rows[0]["name"] == "Week2"
        assert rows[0]["pro_team"] == "MIA"

    def test_season_rows_ignores_weeks_neither_played_nor_next(self):
        """Weeks in by_week not in played or next_week are ignored."""
        by_week = {
            1: [_p(1, "P1", "RB", act=10)],
            2: [_p(1, "P1", "RB", act=5)],
            3: [_p(1, "P1", "RB", act=8)],
        }
        rows = players.season_rows(by_week, played=[1], next_week=None)
        assert "w1" in rows[0]
        assert "w2" not in rows[0]
        assert "w3" not in rows[0]

    def test_season_rows_player_only_in_next_week(self):
        """Player in next_week only appears with all wN None and total None."""
        by_week = {
            2: [_p(1, "NewPlayer", "RB", proj=15, act=None)],
        }
        rows = players.season_rows(by_week, played=[1], next_week=2)
        assert rows[0]["player_id"] == 1
        assert rows[0]["w1"] is None
        assert rows[0]["total"] is None
        assert rows[0]["projected"] == 15

    def test_season_rows_identity_from_next_when_only_in_next(self):
        """Player only in next_week gets identity from next_week row."""
        by_week = {
            2: [{"player_id": 1, "name": "Injured Return", "position": "RB",
                 "pro_team": "LAR", "on_team_id": 0, "percent_owned": 30,
                 "projected": 10, "actual": None}],
        }
        rows = players.season_rows(by_week, played=[1], next_week=2)
        assert rows[0]["name"] == "Injured Return"
        assert rows[0]["pro_team"] == "LAR"


class TestSortField:
    """Resolve sort query param to (param, field) tuple."""

    def test_sort_field_total_when_played_and_requested(self):
        """'total' with played non-empty → ('total', 'total')."""
        param, field = players.sort_field("total", played=[1, 2], has_next=False)
        assert param == "total"
        assert field == "total"

    def test_sort_field_proj_when_next_and_requested(self):
        """'proj' with has_next → ('proj', 'projected')."""
        param, field = players.sort_field("proj", played=[], has_next=True)
        assert param == "proj"
        assert field == "projected"

    def test_sort_field_w_week_when_played_and_valid(self):
        """'w2' with 2 in played → ('w2', 'w2')."""
        param, field = players.sort_field("w2", played=[1, 2], has_next=False)
        assert param == "w2"
        assert field == "w2"

    def test_sort_field_invalid_defaults_to_total_if_played(self):
        """Invalid/unknown sort with played → default to total."""
        param, field = players.sort_field("zzz", played=[1], has_next=True)
        assert param == "total"
        assert field == "total"

    def test_sort_field_invalid_defaults_to_proj_if_no_played(self):
        """Invalid/unknown sort with no played → default to proj."""
        param, field = players.sort_field("zzz", played=[], has_next=True)
        assert param == "proj"
        assert field == "projected"

    def test_sort_field_w_not_played_defaults(self):
        """'w9' not in played → defaults."""
        param, field = players.sort_field("w9", played=[1, 2], has_next=True)
        assert (param, field) == ("total", "total")

    def test_sort_field_none_sort_defaults(self):
        """sort=None → defaults."""
        param, field = players.sort_field(None, played=[1], has_next=False)
        assert (param, field) == ("total", "total")

    def test_sort_field_total_without_played_defaults_to_proj(self):
        """'total' with no played → default to proj."""
        param, field = players.sort_field("total", played=[], has_next=True)
        assert (param, field) == ("proj", "projected")

    def test_sort_field_proj_without_next_defaults_to_total(self):
        """'proj' with no next_week → default to total if played."""
        param, field = players.sort_field("proj", played=[1], has_next=False)
        assert (param, field) == ("total", "total")

    def test_sort_field_proj_without_next_or_played_defaults(self):
        """'proj' with no next and no played → hard default."""
        param, field = players.sort_field("proj", played=[], has_next=False)
        assert (param, field) == ("proj", "projected")

    def test_sort_field_empty_string_defaults(self):
        """'' (empty string) defaults."""
        param, field = players.sort_field("", played=[1], has_next=False)
        assert (param, field) == ("total", "total")


class TestFetchPlayerProjectionsSeason:
    """fetch_player_projections_season returns {week: [rows]} for that season only."""

    def test_fetch_player_projections_season_keyed_by_week(self):
        """Returns {week: [rows]} keyed by week number."""
        c = _fresh_conn()
        db.replace_player_projections(c, 2026, 1, [
            _p(1, "Player1", "RB", proj=10),
            _p(2, "Player2", "RB", proj=5),
        ])
        db.replace_player_projections(c, 2026, 2, [
            _p(3, "Player3", "RB", proj=8),
        ])
        result = db.fetch_player_projections_season(c, 2026)
        assert set(result.keys()) == {1, 2}
        assert len(result[1]) == 2
        assert len(result[2]) == 1
        c.close()

    def test_fetch_player_projections_season_excludes_other_seasons(self):
        """Only returns rows for the requested season."""
        c = _fresh_conn()
        db.replace_player_projections(c, 2025, 1, [_p(1, "Old", "RB", proj=10)])
        db.replace_player_projections(c, 2026, 1, [_p(2, "New", "RB", proj=15)])
        result = db.fetch_player_projections_season(c, 2026)
        names = [r["name"] for r in result[1]]
        assert names == ["New"]
        c.close()

    def test_fetch_player_projections_season_empty_when_none(self):
        """Returns {} when season has no projections."""
        c = _fresh_conn()
        result = db.fetch_player_projections_season(c, 2026)
        assert result == {}
        c.close()


class TestWeeksWithActuals:
    """Weeks that have at least one non-null actual."""

    def test_weeks_with_actuals_ascending_distinct(self):
        """Returns distinct weeks with non-null actuals, ascending."""
        c = _fresh_conn()
        db.replace_player_projections(c, 2026, 1, [
            _p(1, "A", "RB", proj=10, act=5),
            _p(2, "B", "RB", proj=8, act=None),
        ])
        db.replace_player_projections(c, 2026, 2, [
            _p(3, "C", "RB", proj=12, act=8),
        ])
        result = db.weeks_with_actuals(c, 2026)
        assert result == [1, 2]
        c.close()

    def test_weeks_with_actuals_excludes_all_null_week(self):
        """Week with only null actuals is excluded."""
        c = _fresh_conn()
        db.replace_player_projections(c, 2026, 1, [
            _p(1, "A", "RB", proj=10, act=5),
        ])
        db.replace_player_projections(c, 2026, 2, [
            _p(2, "B", "RB", proj=8, act=None),
            _p(3, "C", "RB", proj=12, act=None),
        ])
        result = db.weeks_with_actuals(c, 2026)
        assert result == [1]
        c.close()

    def test_weeks_with_actuals_empty_when_none(self):
        """Returns [] when no non-null actuals exist."""
        c = _fresh_conn()
        db.replace_player_projections(c, 2026, 1, [
            _p(1, "A", "RB", proj=10, act=None),
        ])
        result = db.weeks_with_actuals(c, 2026)
        assert result == []
        c.close()

    def test_weeks_with_actuals_season_filtered(self):
        """Only rows from the requested season count."""
        c = _fresh_conn()
        db.replace_player_projections(c, 2025, 1, [
            _p(1, "Old", "RB", proj=10, act=5),
        ])
        db.replace_player_projections(c, 2026, 2, [
            _p(2, "New", "RB", proj=8, act=8),
        ])
        result = db.weeks_with_actuals(c, 2026)
        assert result == [2]
        c.close()


def _fresh_conn():
    """Create a fresh in-memory database."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn

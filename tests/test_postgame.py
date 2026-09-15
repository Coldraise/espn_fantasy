"""Post-game refresh and correction sweep.

Tests for the event-driven refresh when games finish, and the periodic
correction sweep that picks up stat corrections after games close.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

import pytest

from app import db, poller


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.init_db(connection)
    yield connection
    connection.close()


# --- finished_game_weeks (pure function) ----------------------------------


class TestFinishedGameWeeks:
    """finished_game_weeks groups finished games by week, excluding already-done."""

    def test_empty_games_list_returns_empty_dict(self):
        """Nothing in, nothing out."""
        result = poller.finished_game_weeks([], set())
        assert result == {}

    def test_no_finished_games_returns_empty_dict(self):
        """Games that are not 'post' are skipped."""
        games = [
            {"week": 1, "game_id": 100, "state": "pre"},
            {"week": 1, "game_id": 101, "state": "in"},
            {"week": 1, "game_id": 102, "state": None},
        ]
        result = poller.finished_game_weeks(games, set())
        assert result == {}

    def test_post_state_lowercased_matches(self):
        """'POST', 'Post', 'post' all count as finished."""
        games = [
            {"week": 1, "game_id": 100, "state": "post"},
            {"week": 1, "game_id": 101, "state": "POST"},
            {"week": 1, "game_id": 102, "state": "Post"},
        ]
        result = poller.finished_game_weeks(games, set())
        assert result == {1: ["100", "101", "102"]}

    def test_finished_games_grouped_by_week(self):
        """Multiple weeks produce separate keys."""
        games = [
            {"week": 1, "game_id": 100, "state": "post"},
            {"week": 2, "game_id": 200, "state": "post"},
            {"week": 1, "game_id": 101, "state": "post"},
            {"week": 3, "game_id": 300, "state": "post"},
        ]
        result = poller.finished_game_weeks(games, set())
        assert result == {
            1: ["100", "101"],
            2: ["200"],
            3: ["300"],
        }

    def test_game_ids_converted_to_strings(self):
        """Integer game_ids become strings for comparison with done set."""
        games = [{"week": 1, "game_id": 100, "state": "post"}]
        result = poller.finished_game_weeks(games, set())
        assert result == {1: ["100"]}
        # Verify they're strings, not ints
        assert isinstance(result[1][0], str)

    def test_already_done_games_excluded(self):
        """Games in the done set are not included."""
        games = [
            {"week": 1, "game_id": 100, "state": "post"},
            {"week": 1, "game_id": 101, "state": "post"},
            {"week": 1, "game_id": 102, "state": "post"},
        ]
        done = {"100", "102"}
        result = poller.finished_game_weeks(games, done)
        assert result == {1: ["101"]}

    def test_done_set_comparison_is_string_based(self):
        """Int game_id 100 matches string '100' in done set."""
        games = [{"week": 1, "game_id": 100, "state": "post"}]
        done = {"100"}
        result = poller.finished_game_weeks(games, done)
        assert result == {}

    def test_games_with_missing_week_skipped(self):
        """Rows with week=None are skipped."""
        games = [
            {"week": None, "game_id": 100, "state": "post"},
            {"week": 1, "game_id": 101, "state": "post"},
        ]
        result = poller.finished_game_weeks(games, set())
        assert result == {1: ["101"]}

    def test_games_with_missing_game_id_skipped(self):
        """Rows with game_id=None are skipped."""
        games = [
            {"week": 1, "game_id": None, "state": "post"},
            {"week": 1, "game_id": 101, "state": "post"},
        ]
        result = poller.finished_game_weeks(games, set())
        assert result == {1: ["101"]}

    def test_games_with_missing_state_not_matched(self):
        """Missing state field is treated as not 'post'."""
        games = [{"week": 1, "game_id": 100}]  # no state key
        result = poller.finished_game_weeks(games, set())
        assert result == {}

    def test_state_comparison_case_insensitive(self):
        """State field is lowercased before comparison."""
        games = [
            {"week": 1, "game_id": 100, "state": "in"},
            {"week": 1, "game_id": 101, "state": "IN"},
        ]
        result = poller.finished_game_weeks(games, set())
        assert result == {}


# --- sync_finished_games ------------------------------------------------


class TestSyncFinishedGames:
    """sync_finished_games coordinates the refresh of finished weeks."""

    def test_no_finished_games_returns_zero(self, conn):
        """When no games are finished, nothing happens."""
        db.replace_nfl_games(conn, 2026, 1, [
            {"game_id": 100, "home_team": "KC", "away_team": "SF",
             "kickoff_utc": "2026-09-13T18:00:00Z", "state": "pre", "period": None, "clock": None},
        ])

        with patch("app.poller.refresh_week_stats") as mock_refresh:
            result = poller.sync_finished_games(conn, 2026)

        assert result == 0
        mock_refresh.assert_not_called()

    def test_finished_games_in_single_week_calls_refresh_once(self, conn):
        """One finished week triggers one refresh call."""
        db.replace_nfl_games(conn, 2026, 1, [
            {"game_id": 100, "home_team": "KC", "away_team": "SF",
             "kickoff_utc": "2026-09-13T18:00:00Z", "state": "post", "period": 4, "clock": "0:00"},
            {"game_id": 101, "home_team": "TB", "away_team": "NO",
             "kickoff_utc": "2026-09-13T13:00:00Z", "state": "post", "period": 4, "clock": "0:00"},
        ])

        with patch("app.poller.refresh_week_stats") as mock_refresh:
            mock_refresh.return_value = 10
            result = poller.sync_finished_games(conn, 2026)

        assert result == 10
        mock_refresh.assert_called_once_with(conn, 2026, 1)

    def test_finished_games_marked_in_meta(self, conn):
        """Game ids are stored in postgame_refreshed meta key after refresh."""
        db.replace_nfl_games(conn, 2026, 1, [
            {"game_id": 100, "home_team": "KC", "away_team": "SF",
             "kickoff_utc": "2026-09-13T18:00:00Z", "state": "post", "period": 4, "clock": "0:00"},
            {"game_id": 101, "home_team": "TB", "away_team": "NO",
             "kickoff_utc": "2026-09-13T13:00:00Z", "state": "post", "period": 4, "clock": "0:00"},
        ])

        with patch("app.poller.refresh_week_stats") as mock_refresh:
            mock_refresh.return_value = 5
            poller.sync_finished_games(conn, 2026)

        marked = db.get_meta(conn, "postgame_refreshed:2026")
        assert marked == ["100", "101"]

    def test_immediate_second_call_does_no_refresh(self, conn):
        """Already-refreshed games are not refreshed again."""
        db.replace_nfl_games(conn, 2026, 1, [
            {"game_id": 100, "home_team": "KC", "away_team": "SF",
             "kickoff_utc": "2026-09-13T18:00:00Z", "state": "post", "period": 4, "clock": "0:00"},
        ])

        with patch("app.poller.refresh_week_stats") as mock_refresh:
            mock_refresh.return_value = 5
            result1 = poller.sync_finished_games(conn, 2026)
            result2 = poller.sync_finished_games(conn, 2026)

        assert result1 == 5
        assert result2 == 0
        assert mock_refresh.call_count == 1

    def test_newly_finished_game_in_already_refreshed_week_triggers_refresh(self, conn):
        """Updating a game to post in an already-refreshed week triggers another refresh."""
        db.replace_nfl_games(conn, 2026, 1, [
            {"game_id": 100, "home_team": "KC", "away_team": "SF",
             "kickoff_utc": "2026-09-13T18:00:00Z", "state": "post", "period": 4, "clock": "0:00"},
        ])

        with patch("app.poller.refresh_week_stats") as mock_refresh:
            mock_refresh.return_value = 5
            poller.sync_finished_games(conn, 2026)

        # Now update week 1 to add another finished game
        db.replace_nfl_games(conn, 2026, 1, [
            {"game_id": 100, "home_team": "KC", "away_team": "SF",
             "kickoff_utc": "2026-09-13T18:00:00Z", "state": "post", "period": 4, "clock": "0:00"},
            {"game_id": 101, "home_team": "TB", "away_team": "NO",
             "kickoff_utc": "2026-09-13T13:00:00Z", "state": "post", "period": 4, "clock": "0:00"},
        ])

        with patch("app.poller.refresh_week_stats") as mock_refresh:
            mock_refresh.return_value = 3
            result = poller.sync_finished_games(conn, 2026)

        assert result == 3
        assert mock_refresh.call_count == 1  # Another call for the new game
        marked = db.get_meta(conn, "postgame_refreshed:2026")
        assert marked == ["100", "101"]

    def test_multiple_finished_weeks_refreshed_in_order(self, conn):
        """Multiple weeks are refreshed in ascending week order."""
        db.replace_nfl_games(conn, 2026, 2, [
            {"game_id": 200, "home_team": "KC", "away_team": "SF",
             "kickoff_utc": "2026-09-20T18:00:00Z", "state": "post", "period": 4, "clock": "0:00"},
        ])
        db.replace_nfl_games(conn, 2026, 1, [
            {"game_id": 100, "home_team": "TB", "away_team": "NO",
             "kickoff_utc": "2026-09-13T13:00:00Z", "state": "post", "period": 4, "clock": "0:00"},
        ])

        call_args = []
        def track_refresh(conn, season, week):
            call_args.append((season, week))
            return 5

        with patch("app.poller.refresh_week_stats", side_effect=track_refresh):
            poller.sync_finished_games(conn, 2026)

        # Verify weeks were refreshed in order
        assert call_args == [(2026, 1), (2026, 2)]

    def test_exception_in_one_week_does_not_block_others(self, conn):
        """If refresh fails for week 1, week 2 still refreshes."""
        db.replace_nfl_games(conn, 2026, 1, [
            {"game_id": 100, "home_team": "KC", "away_team": "SF",
             "kickoff_utc": "2026-09-13T18:00:00Z", "state": "post", "period": 4, "clock": "0:00"},
        ])
        db.replace_nfl_games(conn, 2026, 2, [
            {"game_id": 200, "home_team": "TB", "away_team": "NO",
             "kickoff_utc": "2026-09-20T13:00:00Z", "state": "post", "period": 4, "clock": "0:00"},
        ])

        def refresh_with_error(conn, season, week):
            if week == 1:
                raise ValueError("Week 1 error")
            return 5

        with patch("app.poller.refresh_week_stats", side_effect=refresh_with_error):
            result = poller.sync_finished_games(conn, 2026)

        # Week 2 should still return its count
        assert result == 5
        # Only week 2 should be marked (week 1 failed)
        marked = db.get_meta(conn, "postgame_refreshed:2026")
        assert marked == ["200"]

    def test_failed_week_retried_on_next_call(self, conn):
        """A week that failed to refresh is retried on the next sync call."""
        db.replace_nfl_games(conn, 2026, 1, [
            {"game_id": 100, "home_team": "KC", "away_team": "SF",
             "kickoff_utc": "2026-09-13T18:00:00Z", "state": "post", "period": 4, "clock": "0:00"},
        ])

        call_count = [0]
        def refresh_first_then_succeed(conn, season, week):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ValueError("First attempt fails")
            return 5

        with patch("app.poller.refresh_week_stats", side_effect=refresh_first_then_succeed):
            result1 = poller.sync_finished_games(conn, 2026)
            result2 = poller.sync_finished_games(conn, 2026)

        assert result1 == 0  # First call: failure, no rows marked
        assert result2 == 5  # Second call: success, rows marked
        assert call_count[0] == 2

    def test_markers_are_per_season(self, conn):
        """Different seasons have separate postgame_refreshed markers."""
        # Season 2025
        db.replace_nfl_games(conn, 2025, 1, [
            {"game_id": 100, "home_team": "KC", "away_team": "SF",
             "kickoff_utc": "2025-09-13T18:00:00Z", "state": "post", "period": 4, "clock": "0:00"},
        ])
        # Season 2026
        db.replace_nfl_games(conn, 2026, 1, [
            {"game_id": 200, "home_team": "TB", "away_team": "NO",
             "kickoff_utc": "2026-09-13T13:00:00Z", "state": "post", "period": 4, "clock": "0:00"},
        ])

        with patch("app.poller.refresh_week_stats") as mock_refresh:
            mock_refresh.return_value = 5
            poller.sync_finished_games(conn, 2025)
            poller.sync_finished_games(conn, 2026)

        marked_2025 = db.get_meta(conn, "postgame_refreshed:2025")
        marked_2026 = db.get_meta(conn, "postgame_refreshed:2026")
        assert marked_2025 == ["100"]
        assert marked_2026 == ["200"]


# --- refresh_week_stats ------------------------------------------------


class TestRefreshWeekStats:
    """refresh_week_stats re-syncs a week's data and returns total changes."""

    def test_calls_sync_nfl_games(self, conn):
        """sync_nfl_games is called with (conn, season, week)."""
        with patch("app.poller.sync_nfl_games") as mock_nfl:
            with patch("app.poller.sync_matchup_players") as mock_matchup:
                with patch("app.poller._projections_are_fresh", return_value=True):
                    mock_nfl.return_value = 2
                    mock_matchup.return_value = 0
                    result = poller.refresh_week_stats(conn, 2026, 1)

        mock_nfl.assert_called_once_with(conn, 2026, 1)
        assert result >= 2

    def test_calls_sync_matchup_players(self, conn):
        """sync_matchup_players is called with (conn, season, week)."""
        with patch("app.poller.sync_nfl_games") as mock_nfl:
            with patch("app.poller.sync_matchup_players") as mock_matchup:
                with patch("app.poller._projections_are_fresh", return_value=True):
                    mock_nfl.return_value = 0
                    mock_matchup.return_value = 15
                    result = poller.refresh_week_stats(conn, 2026, 1)

        mock_matchup.assert_called_once_with(conn, 2026, 1)
        assert result >= 15

    def test_returns_sum_of_all_changes(self, conn):
        """Return value is the sum of all three sync functions."""
        with patch("app.poller.sync_nfl_games") as mock_nfl:
            with patch("app.poller.sync_matchup_players") as mock_matchup:
                with patch("app.poller._sync_projection_week") as mock_proj:
                    with patch("app.poller._projections_are_fresh", return_value=False):
                        mock_nfl.return_value = 3
                        mock_matchup.return_value = 7
                        mock_proj.return_value = 5
                        result = poller.refresh_week_stats(conn, 2026, 1)

        assert result == 15

    def test_skips_projection_sync_when_fresh(self, conn):
        """_sync_projection_week is not called when _projections_are_fresh returns True."""
        with patch("app.poller.sync_nfl_games") as mock_nfl:
            with patch("app.poller.sync_matchup_players") as mock_matchup:
                with patch("app.poller._projections_are_fresh", return_value=True) as mock_fresh:
                    with patch("app.poller._sync_projection_week") as mock_proj:
                        mock_nfl.return_value = 0
                        mock_matchup.return_value = 0
                        poller.refresh_week_stats(conn, 2026, 1)

        mock_proj.assert_not_called()

    def test_calls_projection_sync_when_stale(self, conn):
        """_sync_projection_week is called when projections are not fresh."""
        with patch("app.poller.sync_nfl_games") as mock_nfl:
            with patch("app.poller.sync_matchup_players") as mock_matchup:
                with patch("app.poller._projections_are_fresh", return_value=False) as mock_fresh:
                    with patch("app.poller._sync_projection_week") as mock_proj:
                        mock_nfl.return_value = 0
                        mock_matchup.return_value = 0
                        mock_proj.return_value = 8
                        result = poller.refresh_week_stats(conn, 2026, 1)

        mock_proj.assert_called_once_with(conn, 2026, 1)
        assert result == 8

    def test_projections_are_fresh_called_with_postgame_min_age(self, conn):
        """_projections_are_fresh is checked with POSTGAME_PROJECTION_MIN_AGE_MINUTES."""
        with patch("app.poller.sync_nfl_games", return_value=0):
            with patch("app.poller.sync_matchup_players", return_value=0):
                with patch("app.poller._projections_are_fresh", return_value=True) as mock_fresh:
                    poller.refresh_week_stats(conn, 2026, 1)

        # Check that _projections_are_fresh was called with the right max_age_minutes
        assert mock_fresh.called
        call_args = mock_fresh.call_args
        # Can be positional or keyword argument
        if call_args.args:
            # Positional args: (conn, season, week, max_age_minutes)
            assert len(call_args.args) >= 4
            assert call_args.args[3] == poller.POSTGAME_PROJECTION_MIN_AGE_MINUTES
        else:
            # Keyword args
            assert call_args.kwargs["max_age_minutes"] == poller.POSTGAME_PROJECTION_MIN_AGE_MINUTES

    def test_exception_from_sync_matchup_players_propagates(self, conn):
        """An exception from sync_matchup_players is not caught."""
        with patch("app.poller.sync_nfl_games", return_value=0):
            with patch("app.poller.sync_matchup_players") as mock_matchup:
                mock_matchup.side_effect = ValueError("API error")
                with pytest.raises(ValueError, match="API error"):
                    poller.refresh_week_stats(conn, 2026, 1)

    def test_exception_from_sync_nfl_games_propagates(self, conn):
        """An exception from sync_nfl_games is not caught."""
        with patch("app.poller.sync_nfl_games") as mock_nfl:
            mock_nfl.side_effect = ValueError("API error")
            with pytest.raises(ValueError, match="API error"):
                poller.refresh_week_stats(conn, 2026, 1)


# --- correction_sweep ------------------------------------------------


class TestCorrectionSweep:
    """correction_sweep re-polls recently completed weeks."""

    def test_requires_in_season_or_complete_state(self, monkeypatch):
        """Nothing happens when state is PRE_DRAFT."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with db.session(db_path) as conn:
                db.init_db(conn)  # Initialize the database

            mock_config = MagicMock()
            mock_config.db_path = db_path

            monkeypatch.setattr("app.poller._state", poller.PRE_DRAFT)

            with patch("app.poller.get_config", return_value=mock_config):
                with patch("app.poller.poll_week") as mock_poll:
                    with patch("app.poller.refresh_week_stats") as mock_refresh:
                        with patch("app.poller.sync_nfl_seasons") as mock_seasons:
                            poller.correction_sweep()

            mock_poll.assert_not_called()
            mock_refresh.assert_not_called()
            mock_seasons.assert_not_called()

    def test_requires_in_season_or_complete_state_drafted(self, monkeypatch):
        """Nothing happens when state is DRAFTED."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with db.session(db_path) as conn:
                db.init_db(conn)  # Initialize the database

            mock_config = MagicMock()
            mock_config.db_path = db_path

            monkeypatch.setattr("app.poller._state", poller.DRAFTED)

            with patch("app.poller.get_config", return_value=mock_config):
                with patch("app.poller.poll_week") as mock_poll:
                    with patch("app.poller.refresh_week_stats") as mock_refresh:
                        with patch("app.poller.sync_nfl_seasons") as mock_seasons:
                            poller.correction_sweep()

            mock_poll.assert_not_called()
            mock_refresh.assert_not_called()
            mock_seasons.assert_not_called()

    def test_polls_and_refreshes_configured_weeks(self, monkeypatch):
        """poll_week and refresh_week_stats called for each configured week."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with db.session(db_path) as conn:
                db.init_db(conn)  # Initialize the database

            mock_config = MagicMock()
            mock_config.current_season = 2026
            mock_config.correction_weeks_back = 2
            mock_config.db_path = db_path

            mock_league = MagicMock()
            mock_league.currentMatchupPeriod = 3

            poll_calls = []
            refresh_calls = []

            def track_poll(conn, league, season, week):
                poll_calls.append((season, week))
                return 5

            def track_refresh(conn, season, week):
                refresh_calls.append((season, week))
                return 3

            monkeypatch.setattr("app.poller._state", poller.IN_SEASON)

            with patch("app.poller.get_config", return_value=mock_config):
                with patch("app.poller.espn_client.get_league", return_value=mock_league):
                    with patch("app.poller.poll_week", side_effect=track_poll):
                        with patch("app.poller.refresh_week_stats", side_effect=track_refresh):
                            with patch("app.poller.sync_nfl_seasons") as mock_seasons:
                                mock_seasons.return_value = 0
                                poller.correction_sweep()

            # With currentMatchupPeriod=3 and correction_weeks_back=2,
            # should poll weeks 1, 2, 3 and refresh weeks 1, 2, 3
            assert sorted(poll_calls) == [(2026, 1), (2026, 2), (2026, 3)]
            assert sorted(refresh_calls) == [(2026, 1), (2026, 2), (2026, 3)]

    def test_sync_nfl_seasons_called_with_current_season(self, monkeypatch):
        """sync_nfl_seasons called once with the current season."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with db.session(db_path) as conn:
                db.init_db(conn)  # Initialize the database

            mock_config = MagicMock()
            mock_config.current_season = 2026
            mock_config.correction_weeks_back = 2
            mock_config.db_path = db_path

            mock_league = MagicMock()
            mock_league.currentMatchupPeriod = 3

            season_call_args = []

            def track_seasons(conn, season):
                season_call_args.append(season)
                return 0

            monkeypatch.setattr("app.poller._state", poller.IN_SEASON)

            with patch("app.poller.get_config", return_value=mock_config):
                with patch("app.poller.espn_client.get_league", return_value=mock_league):
                    with patch("app.poller.poll_week", return_value=0):
                        with patch("app.poller.refresh_week_stats", return_value=0):
                            with patch("app.poller.sync_nfl_seasons", side_effect=track_seasons):
                                poller.correction_sweep()

            assert season_call_args == [2026]

    def test_exception_in_one_week_does_not_block_sweep(self, monkeypatch):
        """If refresh fails for week 2, weeks 1 and 3 still refresh and seasons syncs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with db.session(db_path) as conn:
                db.init_db(conn)  # Initialize the database

            mock_config = MagicMock()
            mock_config.current_season = 2026
            mock_config.correction_weeks_back = 2
            mock_config.db_path = db_path

            mock_league = MagicMock()
            mock_league.currentMatchupPeriod = 3

            refresh_calls = []

            def track_refresh(conn, season, week):
                refresh_calls.append(week)
                if week == 2:
                    raise ValueError("Week 2 error")
                return 3

            monkeypatch.setattr("app.poller._state", poller.IN_SEASON)

            with patch("app.poller.get_config", return_value=mock_config):
                with patch("app.poller.espn_client.get_league", return_value=mock_league):
                    with patch("app.poller.poll_week", return_value=0):
                        with patch("app.poller.refresh_week_stats", side_effect=track_refresh):
                            with patch("app.poller.sync_nfl_seasons", return_value=0) as mock_seasons:
                                poller.correction_sweep()

            # All weeks should have been attempted
            assert sorted(refresh_calls) == [1, 2, 3]
            # sync_nfl_seasons should still have been called
            assert mock_seasons.called
            # and the run itself is still recorded as a success
            with db.session(db_path) as conn:
                status = conn.execute(
                    "SELECT status FROM poll_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
            assert status == "ok"

    def test_works_in_complete_state(self, monkeypatch):
        """correction_sweep also runs when state is COMPLETE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with db.session(db_path) as conn:
                db.init_db(conn)  # Initialize the database

            mock_config = MagicMock()
            mock_config.current_season = 2026
            mock_config.correction_weeks_back = 2
            mock_config.db_path = db_path

            mock_league = MagicMock()
            mock_league.currentMatchupPeriod = 17

            monkeypatch.setattr("app.poller._state", poller.COMPLETE)

            with patch("app.poller.get_config", return_value=mock_config):
                with patch("app.poller.espn_client.get_league", return_value=mock_league):
                    with patch("app.poller.poll_week", return_value=0) as mock_poll:
                        with patch("app.poller.refresh_week_stats", return_value=0) as mock_refresh:
                            with patch("app.poller.sync_nfl_seasons", return_value=0) as mock_seasons:
                                poller.correction_sweep()

            # Should have been called
            assert mock_refresh.called
            assert mock_seasons.called

    def test_uses_monkeypatched_state(self, monkeypatch):
        """Module state is properly isolated via monkeypatch."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with db.session(db_path) as conn:
                db.init_db(conn)  # Initialize the database

            mock_config = MagicMock()
            mock_config.db_path = db_path

            original_state = poller._state

            # Monkeypatch via the fixture
            monkeypatch.setattr("app.poller._state", poller.PRE_DRAFT)

            with patch("app.poller.get_config", return_value=mock_config):
                with patch("app.poller.poll_week") as mock_poll:
                    poller.correction_sweep()

            # PRE_DRAFT should skip polls
            mock_poll.assert_not_called()

            # Verify original state is unchanged after test
            assert poller._state == original_state

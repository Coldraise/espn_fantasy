"""games_are_live as a pure function.

The regression this guards against: ESPN's stored `state` for a game goes
stale between polls at the idle cadence (once every six hours), so a gate that
trusted `state` alone missed a Wednesday opener entirely -- the configured
live windows only ever covered Thu/Sun/Mon.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone

from app import poller


def test_in_progress_game_is_live():
    games = [{"kickoff_utc": "2026-09-10T00:20:00+00:00", "state": "in"}]
    moment = datetime(2026, 9, 10, 0, 30, tzinfo=timezone.utc)
    assert poller.games_are_live(games, moment) is True


def test_stale_pre_state_past_kickoff_is_live():
    # THE STALE-ROW CASE: the regression that started all of this. A row
    # fetched before kickoff still says state='pre' hours later because
    # nothing has refreshed it, but the kickoff time itself is a schedule
    # fact and does not go stale.
    games = [{"kickoff_utc": "2026-09-10T00:20:00+00:00", "state": "pre"}]
    moment = datetime(2026, 9, 10, 0, 30, tzinfo=timezone.utc)  # 10 min past kickoff
    assert poller.games_are_live(games, moment) is True


def test_kickoff_soon_is_live():
    games = [{"kickoff_utc": "2026-09-10T00:20:00+00:00", "state": "pre"}]
    moment = datetime(2026, 9, 10, 0, 15, tzinfo=timezone.utc)  # 5 min before kickoff
    assert poller.games_are_live(games, moment) is True


def test_kickoff_hours_away_is_not_live():
    games = [{"kickoff_utc": "2026-09-10T00:20:00+00:00", "state": "pre"}]
    moment = datetime(2026, 9, 9, 22, 20, tzinfo=timezone.utc)  # 2 hours before kickoff
    assert poller.games_are_live(games, moment) is False


def test_post_state_is_not_live_even_moments_after_kickoff():
    games = [{"kickoff_utc": "2026-09-10T00:20:00+00:00", "state": "post"}]
    moment = datetime(2026, 9, 10, 0, 25, tzinfo=timezone.utc)
    assert poller.games_are_live(games, moment) is False


def test_stale_in_state_long_after_kickoff_is_not_live():
    games = [{"kickoff_utc": "2026-09-10T00:20:00+00:00", "state": "in"}]
    moment = datetime(2026, 9, 10, 9, 20, tzinfo=timezone.utc)  # 9 hours after kickoff
    assert poller.games_are_live(games, moment) is False


def test_empty_games_is_not_live():
    assert poller.games_are_live([], datetime.now(timezone.utc)) is False


def test_2026_opener_wednesday_night_regression():
    # A Wednesday night in ET, outside every configured Thu/Sun/Mon window --
    # exactly the gap that let a live game go unwatched.
    games = [{"kickoff_utc": "2026-09-10T00:20:00+00:00", "state": "in"}]
    moment = datetime(2026, 9, 10, 1, 35, tzinfo=timezone.utc)
    assert poller.games_are_live(games, moment) is True

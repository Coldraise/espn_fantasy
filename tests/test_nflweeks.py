"""Where an NFL week falls on the calendar.

The whole module is arithmetic off Labor Day, so the cases that matter are the
ones where the calendar shifts underneath it: seasons that open a week earlier,
and dates that fall outside the season entirely.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timezone

from app import nflweeks


# --- the anchor ------------------------------------------------------------

def test_week_one_matches_the_real_openers():
    """Checked against the kickoffs that actually happened."""
    assert nflweeks.week1_thursday(2024) == date(2024, 9, 5)
    assert nflweeks.week1_thursday(2025) == date(2025, 9, 4)


def test_week_one_follows_labor_day_when_september_starts_late():
    """2026 opens a week later than 2025: Labor Day is the 7th, not the 1st."""
    assert nflweeks.week1_thursday(2026) == date(2026, 9, 10)
    assert nflweeks.week1_thursday(2027) == date(2027, 9, 9)


def test_week_one_thursday_is_always_a_thursday():
    for season in range(2020, 2035):
        assert nflweeks.week1_thursday(season).weekday() == 3


# --- placing a date --------------------------------------------------------

def test_the_opener_is_week_one_and_the_week_runs_to_wednesday():
    start = nflweeks.week1_thursday(2026)
    assert nflweeks.week_for_date(2026, start) == 1
    assert nflweeks.week_for_date(2026, date(2026, 9, 16)) == 1   # the Wednesday
    assert nflweeks.week_for_date(2026, date(2026, 9, 17)) == 2   # next Thursday


def test_dates_outside_the_season_are_not_clamped():
    """August is not week 1 and February is not week 18 -- both are nothing."""
    assert nflweeks.week_for_date(2026, date(2026, 8, 1)) is None
    assert nflweeks.week_for_date(2026, date(2027, 2, 1)) is None


def test_a_shorter_season_ends_earlier():
    late = date(2026, 12, 31)
    assert nflweeks.week_for_date(2026, late, weeks=18) == 17
    assert nflweeks.week_for_date(2026, late, weeks=14) is None


# --- the holidays ----------------------------------------------------------

def test_thanksgiving_is_the_fourth_thursday_of_november():
    assert nflweeks.thanksgiving(2024) == date(2024, 11, 28)
    assert nflweeks.thanksgiving(2025) == date(2025, 11, 27)
    assert nflweeks.thanksgiving(2026) == date(2026, 11, 26)


def test_the_holidays_land_where_the_calendar_puts_them():
    """They move with Labor Day, so the week number is not fixed."""
    assert nflweeks.week_for_date(2025, nflweeks.halloween(2025)) == 9
    assert nflweeks.week_for_date(2025, nflweeks.thanksgiving(2025)) == 13
    assert nflweeks.week_for_date(2026, nflweeks.halloween(2026)) == 8
    assert nflweeks.week_for_date(2026, nflweeks.thanksgiving(2026)) == 12


# --- the trade deadline ----------------------------------------------------

def _ms(y, m, d, hour=12):
    return datetime(y, m, d, hour, tzinfo=timezone.utc).timestamp() * 1000


def test_the_deadline_converts_from_epoch_milliseconds():
    assert nflweeks.deadline_date(_ms(2026, 11, 18)) == date(2026, 11, 18)


def test_a_missing_or_junk_deadline_is_not_a_crash():
    assert nflweeks.deadline_date(None) is None
    assert nflweeks.deadline_date(0) is None
    assert nflweeks.deadline_date("nonsense") is None


def test_the_deadline_is_read_in_league_time_not_utc():
    """8pm Eastern on a Wednesday is already Thursday in UTC. It belongs to the
    Wednesday, or the marker lands a week late."""
    assert nflweeks.deadline_date(_ms(2026, 11, 19, hour=1)) == date(2026, 11, 18)


# --- what the schedule grid actually consumes ------------------------------

def test_markers_are_keyed_by_week():
    found = nflweeks.season_markers(2026, list(range(1, 16)))
    assert [m["label"] for m in found[8]] == ["Halloween"]
    assert [m["label"] for m in found[12]] == ["Thanksgiving"]


def test_the_deadline_joins_the_markers_when_given():
    """Wed 18 Nov 2026 is in week 10, which runs 12-18 Nov."""
    found = nflweeks.season_markers(2026, list(range(1, 16)),
                                    deadline_ms=_ms(2026, 11, 18))
    assert "Trade deadline" in [m["label"] for m in found[10]]


def test_two_markers_can_share_one_week_in_date_order():
    """A deadline the day after Thanksgiving shares week 12 with it, and must
    not evict the holiday. Both are listed, earliest first."""
    found = nflweeks.season_markers(2026, list(range(1, 16)),
                                    deadline_ms=_ms(2026, 11, 27))
    assert [m["label"] for m in found[12]] == ["Thanksgiving", "Trade deadline"]


def test_a_deadline_outside_the_season_is_dropped():
    found = nflweeks.season_markers(2026, list(range(1, 16)),
                                    deadline_ms=_ms(2026, 7, 1))
    assert all(m["label"] != "Trade deadline"
               for marks in found.values() for m in marks)

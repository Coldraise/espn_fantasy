"""Where an NFL week falls on the calendar.

Nothing in the database stores dates against weeks -- team_weeks and
nfl_player_weeks both key on a week NUMBER and stop there -- so placing anything
dated on the schedule grid means deriving the calendar ourselves.

The anchor is Labor Day: the NFL opens on the Thursday after it, and every week
runs Thursday to Wednesday from there. Checked against the real openers, this
returns 2024-09-05 and 2025-09-04, both correct.

Times are resolved in LEAGUE_TZ rather than the reader's display timezone, for
the same reason config.py keeps the two apart: when a week falls is a fact about
the NFL, not about whoever is looking at it. A trade deadline at 8pm Eastern
belongs to that Wednesday even when the reader is in Budapest and it is already
Thursday morning for them.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .config import LEAGUE_TZ

# Regular season plus the fantasy playoff weeks ESPN keeps numbering.
DEFAULT_WEEKS = 18

_MONDAY, _THURSDAY = 0, 3


def week1_thursday(season: int) -> date:
    """The Thursday the season opens: the one after the first Monday in September."""
    labor_day = date(season, 9, 1)
    while labor_day.weekday() != _MONDAY:
        labor_day += timedelta(days=1)
    return labor_day + timedelta(days=3)


def week_for_date(season: int, day: date, weeks: int = DEFAULT_WEEKS) -> int | None:
    """Which week contains `day`, or None if it falls outside the season.

    Deliberately unclamped: a date in August or in February is not week 1 or
    week 18, it is not in the season at all, and saying so lets callers drop a
    marker instead of pinning it to an edge where it would read as real.
    """
    start = week1_thursday(season)
    if day < start:
        return None
    week = (day - start).days // 7 + 1
    return week if 1 <= week <= weeks else None


def thanksgiving(season: int) -> date:
    """Fourth Thursday in November."""
    day = date(season, 11, 1)
    while day.weekday() != _THURSDAY:
        day += timedelta(days=1)
    return day + timedelta(days=21)


def halloween(season: int) -> date:
    return date(season, 10, 31)


def deadline_date(epoch_ms: int | float | None) -> date | None:
    """ESPN's tradeSettings.deadlineDate (epoch milliseconds) as a league-local day."""
    if not epoch_ms:
        return None
    try:
        moment = datetime.fromtimestamp(float(epoch_ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return moment.astimezone(LEAGUE_TZ).date()


def season_markers(season: int, weeks: list[int] | None = None,
                   deadline_ms: int | float | None = None) -> dict[int, list[dict]]:
    """Dated things worth marking, keyed by the week they land in.

    Shaped as a list per week because two can collide: a trade deadline set in
    late November shares a week with Thanksgiving often enough to matter.
    """
    span = max(weeks) if weeks else DEFAULT_WEEKS
    found: dict[int, list[dict]] = {}

    def mark(day: date | None, icon: str, label: str) -> None:
        if day is None:
            return
        week = week_for_date(season, day, span)
        if week is None:
            return
        found.setdefault(week, []).append(
            {"icon": icon, "label": label, "date": day})

    mark(deadline_date(deadline_ms), "⚑", "Trade deadline")
    mark(halloween(season), "🎃", "Halloween")
    mark(thanksgiving(season), "🦃", "Thanksgiving")
    for week in found:
        found[week].sort(key=lambda m: m["date"])
    return found

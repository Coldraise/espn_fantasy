"""Background polling.

ESPN offers no webhooks or push, so this is a poller. Cadence is decided in code
on every tick rather than by reconfiguring the scheduler: one interval job asks
what state the season is in and either polls or returns. The schedule stays
static and the decision stays testable.

All window arithmetic uses an explicit America/New_York ZoneInfo, never the
ambient container clock, so it stays correct on a UTC host.

Fetching goes through `scoreboard()`, not `box_scores()`. box_scores reads
`rosterForCurrentScoringPeriod` unguarded, so it raises KeyError for the whole
pre-draft period, and it honours its `week` argument only when
`week <= current_week` -- past that it silently returns the *current* week's
data, which would write today's scores under future week numbers. scoreboard
reads the schedule directly: no rosters needed, any week, one request instead of
three. box_scores is used only to enrich the live week with projected scores.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from . import db, espn_client, news, nflverse, players, rules
from .config import LEAGUE_TZ, get_config

log = logging.getLogger(__name__)

PRE_DRAFT = "pre_draft"
DRAFTED = "drafted"
IN_SEASON = "in_season"
COMPLETE = "complete"

_last_poll_at: datetime | None = None
_scheduler: BackgroundScheduler | None = None
_drafted: bool = False          # latches true; a completed draft never un-completes
_state: str = PRE_DRAFT
_draft_date: datetime | None = None


# --- season state ---------------------------------------------------------


def season_state(scoring_period: int, final_period: int, drafted: bool, has_scores: bool) -> str:
    """Where the season is, from the cheapest signals available.

    Pure so it can be tested without a league object.
    """
    if not drafted:
        return PRE_DRAFT
    if scoring_period > final_period:
        return COMPLETE
    if not has_scores:
        return DRAFTED
    return IN_SEASON


def current_state() -> str:
    return _state


def draft_datetime() -> datetime | None:
    return _draft_date


def is_live_window(moment: datetime | None = None) -> bool:
    cfg = get_config()
    moment = (moment or datetime.now(timezone.utc)).astimezone(LEAGUE_TZ)
    return any(window.contains(moment) for window in cfg.live_windows)


def _near_draft(now: datetime, hours: int = 6) -> bool:
    """Within a few hours of draft time -- worth watching for completion."""
    if _draft_date is None:
        return False
    return abs((now - _draft_date).total_seconds()) <= hours * 3600


def _should_poll(now: datetime) -> str | None:
    """The poll kind, or None to skip this tick.

    Polling every 45s for the ten days before a draft would be pure waste, so
    only an actually-running season earns the live cadence.
    """
    cfg = get_config()
    if _state in (PRE_DRAFT, DRAFTED):
        if _near_draft(now):
            if _last_poll_at is None or (now - _last_poll_at).total_seconds() >= 300:
                return "draft_watch"
            return None
        if _last_poll_at is None or (now - _last_poll_at).total_seconds() >= cfg.idle_interval:
            return "preseason"
        return None

    if is_live_window(now):
        return "live"
    if _last_poll_at is None or (now - _last_poll_at).total_seconds() >= cfg.idle_interval:
        return "idle"
    return None


# --- espn -> rows ---------------------------------------------------------


def _team_id(value) -> int | None:
    """Bye weeks and unmatched sides come back as 0 or None."""
    if hasattr(value, "team_id"):
        value = value.team_id
    try:
        return int(value) or None
    except (TypeError, ValueError):
        return None


def _owner_name(team) -> str | None:
    owners = getattr(team, "owners", None)
    if isinstance(owners, list) and owners:
        first = owners[0]
        if isinstance(first, dict):
            name = f"{first.get('firstName', '')} {first.get('lastName', '')}".strip()
            return name or first.get("displayName")
        return str(first)
    return getattr(team, "owner", None)


def schedule_periods(season: int) -> list[int]:
    """Matchup periods actually present in the schedule.

    Not max(settings.matchup_periods): that reports 17 while the schedule only
    holds 1-15, because playoff rounds are not generated until seeding exists.
    """
    payload = espn_client.raw_view(season, "mMatchupScore")
    schedule = payload.get("schedule") or []
    return sorted({m["matchupPeriodId"] for m in schedule if m.get("matchupPeriodId")})


def _completed_through(league, season: int) -> int:
    """Last matchup period whose result can be treated as final."""
    cfg = get_config()
    if season < cfg.current_season:
        return 10_000
    current = getattr(league, "currentMatchupPeriod", None) or 1
    return max(int(current) - 1, 0)


def sync_teams(conn, league, season: int) -> None:
    for team in getattr(league, "teams", []) or []:
        team_id = _team_id(team)
        if team_id is None:
            continue
        db.upsert_team(
            conn, season, team_id,
            getattr(team, "team_name", None) or f"Team {team_id}",
            _owner_name(team),
            getattr(team, "team_abbrev", None),
        )


def poll_week(conn, league, season: int, week: int, projected: dict | None = None) -> int:
    """Store one matchup period. Returns the number of rows that changed."""
    matchups = espn_client.call(league.scoreboard, week)
    final = week <= _completed_through(league, season)
    projected = projected or {}
    changed = 0

    for matchup in matchups or []:
        # Read the private id fields: League.scoreboard only assigns .home_team /
        # .away_team when a team matches, so the public attribute can be absent.
        home_id = _team_id(getattr(matchup, "_home_team_id", None))
        away_id = _team_id(getattr(matchup, "_away_team_id", None))
        playoff = bool(getattr(matchup, "is_playoff", False))
        home_pts = getattr(matchup, "home_score", None)
        away_pts = getattr(matchup, "away_score", None)

        for team_id, opp_id, mine, theirs in (
            (home_id, away_id, home_pts, away_pts),
            (away_id, home_id, away_pts, home_pts),
        ):
            if team_id is None:
                continue
            result = None
            if final and opp_id is not None and mine is not None and theirs is not None:
                result = "W" if mine > theirs else "L" if mine < theirs else "T"
            if db.upsert_team_week(
                conn, season, week, team_id, mine,
                projected.get(team_id), opp_id, playoff, result,
            ):
                changed += 1
    return changed


def _live_projections(league, week: int) -> dict[int, float]:
    """Projected scores for the live week only.

    box_scores is the only source for these and it needs rosters, so this is
    best-effort: any failure simply means no projections this tick.
    """
    out: dict[int, float] = {}
    try:
        for box in espn_client.call(league.box_scores, week, attempts=1) or []:
            for team, value in ((box.home_team, getattr(box, "home_projected", None)),
                                (box.away_team, getattr(box, "away_projected", None))):
                team_id = _team_id(team)
                if team_id is not None and value and value > 0:
                    out[team_id] = value
    except Exception as exc:  # noqa: BLE001
        log.debug("no projections for week %s: %s", week, exc)
    return out


def poll_season(conn, season: int, weeks: list[int] | None = None, refresh: bool = False) -> int:
    league = espn_client.get_league(season, refresh=refresh)
    sync_teams(conn, league, season)
    target = weeks if weeks is not None else schedule_periods(season)
    changed = 0
    for week in target:
        try:
            changed += poll_week(conn, league, season, week)
        except espn_client.AuthInvalid:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("season %s week %s failed: %s", season, week, exc)
    return changed


# --- history and draft ----------------------------------------------------


def sync_history(conn) -> int:
    """Pull every past season's totals in a single request."""
    changed = 0
    for season, payload in espn_client.league_history().items():
        for team in payload.get("teams") or []:
            overall = (team.get("record") or {}).get("overall") or {}
            if overall.get("wins") is None:
                continue
            if db.upsert_team_season(
                conn, season, int(team["id"]), team.get("name"),
                overall.get("wins"), overall.get("losses"), overall.get("ties"),
                overall.get("pointsFor"), overall.get("pointsAgainst"),
                team.get("rankCalculatedFinal"), team.get("playoffSeed"),
            ):
                changed += 1
    return changed


PROJECTION_MAX_AGE_MINUTES = 30


def _projections_are_fresh(conn, season: int, week: int) -> bool:
    stamp = db.projections_synced_at(conn, season, week)
    if not stamp:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
    except ValueError:
        return False
    return age.total_seconds() < PROJECTION_MAX_AGE_MINUTES * 60


def sync_projections(conn, season: int, week: int, per_group: int = 60,
                     force: bool = False) -> int:
    """Store the top players per position for one week, with headroom above the
    50 the page shows.

    The previous week is refreshed too: its actual results only exist once the
    games have been played, so the row written before kickoff has a null actual
    that never fills in on its own. That is what feeds the "last week" column.
    """
    written = 0
    for target in ([week - 1, week] if week > 1 else [week]):
        # Each fetch is several MB and projections do not move minute to
        # minute, so this is throttled: tick() runs every 45s during games and
        # would otherwise pull ~12MB per tick.
        if not force and _projections_are_fresh(conn, season, target):
            continue
        rows = espn_client.player_projections(season, target)
        if not rows:
            continue
        keep = [p for group in players.group_by_position(rows, per_group=per_group)
                for p in group["players"]]
        written += db.replace_player_projections(conn, season, target, keep)
    return written


PLAYER_SYNC_MAX_AGE_HOURS = 24


def sync_players(conn, force: bool = False) -> int:
    """Refresh NFL player reference data (experience -> rookie).

    Rookie status cannot change mid-season, so this runs at most daily rather
    than every poll: it costs 32 requests, which is not a per-cycle price.
    """
    if not force:
        stamp = db.players_synced_at(conn)
        if stamp:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
                if age.total_seconds() < PLAYER_SYNC_MAX_AGE_HOURS * 3600:
                    return 0
            except ValueError:
                pass
    rows = espn_client.nfl_experience()
    if not rows:
        return 0
    written = db.replace_players(conn, rows)
    log.info("player reference synced: %d athletes, %d rookies",
             written, sum(1 for v in rows.values() if v.get("years") == 0))
    return written


def sync_nflverse(conn, season: int, force: bool = False) -> int:
    """Pull nflverse weekly NFL stats for one season.

    Three files, ~4MB compressed in total, and almost every call is a no-op: the
    per-release `timestamp.json` tells us whether nflverse has rebuilt anything
    since last time, and a season that is already final never will. That check
    costs a few bytes, which is what makes it safe to hang off the ordinary
    poll loop instead of a separate schedule.

    Needs no ESPN credentials, so it keeps working when the cookies expire --
    which is the whole point of it being a second source.
    """
    stamp = nflverse.release_timestamp("stats_player")
    known = db.get_nflverse_sync(conn, f"stats_player:{season}")
    if not force and stamp and known and known.get("last_updated") == stamp:
        return 0

    try:
        rows = nflverse.weekly_player_stats(season)
    except nflverse.NotPublished:
        # Normal before a season's week 1 has been played. Recording the
        # timestamp anyway would claim we have data we do not.
        log.info("nflverse has no weekly stats for %s yet", season)
        return 0

    # Snap share is a separate release keyed on Pro Football Reference ids, so
    # it is merged in here rather than stored apart -- every consumer wants it
    # on the same row as the targets it explains.
    by_pfr = {v["gsis_id"]: v["pfr_id"]
              for v in db.nfl_id_map(conn).values()
              if v.get("gsis_id") and v.get("pfr_id")}
    try:
        snaps = nflverse.snap_counts(season)
    except nflverse.NotPublished:
        snaps = {}
    for row in rows:
        pfr_id = by_pfr.get(row["gsis_id"])
        if pfr_id:
            row.update(snaps.get((pfr_id, row["week"]), {}))

    written = db.replace_nfl_player_weeks(conn, season, rows)
    db.set_nflverse_sync(conn, f"stats_player:{season}", stamp, written)

    try:
        team_rows = nflverse.weekly_team_stats(season)
        written += db.replace_nfl_team_weeks(conn, season, team_rows)
    except nflverse.NotPublished:
        pass

    log.info("nflverse %s: %d player-weeks, %d with snap counts",
             season, len(rows), sum(1 for r in rows if r.get("offense_pct") is not None))
    return written


def sync_nfl_seasons(conn, current_season: int, force: bool = False) -> int:
    """Keep the current NFL season fresh, plus the last completed one.

    The previous season is what the usage pages show until the current one has
    games -- and once it is final it never changes again, so it is fetched once
    and then skipped rather than re-downloaded every day for a year.
    """
    written = sync_nflverse(conn, current_season, force=force)
    previous = current_season - 1
    if force or previous not in db.nfl_seasons(conn):
        written += sync_nflverse(conn, previous, force=force)
    return written


def sync_nfl_ids(conn, force: bool = False) -> int:
    """Refresh the ESPN -> gsis/pfr player id map.

    Separate from the stats sync and on its own release timestamp: this file
    changes when players sign, not when games are played, and every other
    nflverse join depends on it being present first.
    """
    stamp = nflverse.release_timestamp("players")
    known = db.get_nflverse_sync(conn, "players")
    if not force and stamp and known and known.get("last_updated") == stamp:
        return 0
    rows = nflverse.player_ids()
    if not rows:
        return 0
    written = db.replace_nfl_player_ids(conn, rows)
    db.set_nflverse_sync(conn, "players", stamp, written)
    log.info("nflverse player ids synced: %d espn ids mapped", written)
    return written


def sync_matchup_players(conn, season: int, week: int, per_team: int | None = None) -> int:
    """Store each team's full roster for a week, starters flagged, best first.

    Bench players are kept as well as starters, but flagged: a benched player
    can carry the highest projection on a roster, so billing them as the team's
    best player for a week they do not play would be wrong. Every read defaults
    to starters only, which is why the flag rather than a separate table.

    Keeping the bench is what makes "points left on the bench" answerable at
    all -- you cannot know the lineup someone should have started without
    knowing who was available. ~30 rows per team-week is nothing on disk, and
    re-fetching rosters at render time would put ESPN back on the page path.
    `per_team` caps the starters for callers that want less.
    """
    rosters = espn_client.matchup_rosters(season, week)
    if not rosters:
        return 0

    def score(person):
        return -((person.get("actual") if person.get("actual") else person.get("projected")) or 0)

    keep: list[dict] = []
    for team_id, people in rosters.items():
        starters, bench = [], []
        for person in people:
            target = bench if person.get("lineup_slot") in rules.NON_SCORING_SLOTS else starters
            target.append(person)
        starters.sort(key=score)
        bench.sort(key=score)
        for person in (starters[:per_team] if per_team else starters):
            keep.append({**person, "team_id": team_id, "is_starter": True})
        for person in bench:
            keep.append({**person, "team_id": team_id, "is_starter": False})
    return db.replace_team_week_players(conn, season, week, keep)


def sync_activity(conn, season: int, size: int = 100) -> int:
    """Store the league's transaction feed. Returns only genuinely new rows.

    Nothing happens here before the draft, and the feed is cheap, so this runs
    on the ordinary poll rather than on its own schedule.
    """
    rows = espn_client.recent_activity(size=size)
    if not rows:
        return 0
    written = db.upsert_transactions(conn, season, rows, db.player_name_map(conn))
    if written:
        log.info("transactions: %d new", written)
    return written


NEWS_MAX_AGE_MINUTES = 10


def _news_are_fresh(conn) -> bool:
    stamp = db.get_meta(conn, "news_synced_at")
    if not stamp:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
    except ValueError:
        return False
    return age.total_seconds() < NEWS_MAX_AGE_MINUTES * 60


def sync_news(conn, reporters, keep_days, force: bool = False) -> int:
    """Store ESPN's public NFL news feed. Returns only genuinely new rows.

    Throttled like projections: tick() runs every 45s inside a live window, and
    the feed is a rolling ~36-hour window that does not move that fast.

    Every article is stored, not just ones bylined by `reporters` -- fifty rows
    is nothing, and the roster join needs bylineless injury items that a
    reporter filter would drop. The page does the separating, not the fetch.
    """
    if not force and _news_are_fresh(conn):
        return 0
    rows = news.parse(news.fetch())
    written = db.upsert_news(conn, rows)
    db.prune_news(conn, keep_days)
    db.set_meta(conn, "news_synced_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if written:
        log.info("news: %d new", written)
    return written


def sync_draft(conn, season: int | None = None) -> int:
    """Store the draft board once ESPN reports the draft complete.

    Gated on the `drafted` flag, never on len(picks): ESPN returns a full array
    of empty placeholder picks before the draft (224 of them for an 8-team,
    28-round league), so a non-empty picks list proves nothing.
    """
    global _drafted
    cfg = get_config()
    season = season or cfg.current_season

    detail = espn_client.draft_detail(season)
    if not detail["drafted"]:
        return 0
    _drafted = True

    if db.fetch_draft_picks(conn, season):
        return 0  # already stored; the board does not change after the draft

    league = espn_client.get_league(season, refresh=True)
    espn_client.call(league.refresh_draft, refresh_players=True)

    rows = []
    for index, pick in enumerate(getattr(league, "draft", []) or [], start=1):
        rows.append({
            "overall_pick": index,
            "round": getattr(pick, "round_num", None),
            "round_pick": getattr(pick, "round_pick", None),
            "team_id": _team_id(getattr(pick, "team", None)),
            "player_id": getattr(pick, "playerId", None),
            "player_name": getattr(pick, "playerName", None) or None,
            "keeper": getattr(pick, "keeper_status", False),
            "bid_amount": getattr(pick, "bid_amount", None),
        })
    if not rows:
        return 0
    written = db.replace_draft_picks(conn, season, rows)
    log.info("stored %d draft picks for %s", written, season)
    return written


# --- jobs -----------------------------------------------------------------


def refresh_season_state(conn, league=None) -> str:
    """Recompute the cached season state. One extra request at most."""
    global _state, _drafted, _draft_date
    cfg = get_config()

    if not _drafted:
        try:
            _drafted = espn_client.draft_detail(cfg.current_season)["drafted"]
        except Exception as exc:  # noqa: BLE001
            log.debug("draft status unavailable: %s", exc)

    if _draft_date is None:
        try:
            ms = espn_client.season_status(cfg.current_season).get("draft_date_ms")
            if ms:
                _draft_date = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        except Exception as exc:  # noqa: BLE001
            log.debug("draft date unavailable: %s", exc)

    if league is None:
        league = espn_client.get_league(cfg.current_season)
    scoring = int(getattr(league, "scoringPeriodId", 0) or 0)
    final = int(getattr(league, "finalScoringPeriod", 17) or 17)
    _state = season_state(scoring, final, _drafted, db.has_week_data(conn))

    # Snapshot for the web layer, which never calls ESPN itself.
    try:
        status = espn_client.season_status(cfg.current_season)
    except Exception:  # noqa: BLE001
        status = db.get_meta(conn, "status") or {}
    status["state"] = _state
    status["drafted"] = _drafted
    status["draft_date_utc"] = _draft_date.isoformat() if _draft_date else None
    db.set_meta(conn, "status", status)

    # League rules, rendered once here so page renders never touch ESPN.
    try:
        raw = espn_client.raw_view(cfg.current_season, "mSettings").get("settings") or {}
        if raw:
            db.set_meta(conn, "rules", rules.build(raw, cfg.display_tz_name))
            # The raw payload as well as the rendered rules: lineup efficiency
            # needs the actual starting-slot counts, and re-deriving those from
            # display strings like "1xQB, 2xRB" would be parsing our own prose.
            db.set_meta(conn, "settings", raw)
    except Exception as exc:  # noqa: BLE001
        log.debug("settings unavailable: %s", exc)
    return _state


def _run(kind: str, work) -> None:
    global _last_poll_at
    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        run_id = db.start_poll_run(conn, kind)
        try:
            changed = work(conn, cfg)
            db.finish_poll_run(conn, run_id, "ok", changed)
            _last_poll_at = datetime.now(timezone.utc)
            if changed:
                log.info("%s poll: %d row(s) changed", kind, changed)
        except espn_client.AuthInvalid as exc:
            db.finish_poll_run(conn, run_id, "error", 0, f"auth: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("%s poll failed", kind)
            db.finish_poll_run(conn, run_id, "error", 0, str(exc))


def tick() -> None:
    now = datetime.now(timezone.utc)
    kind = _should_poll(now)
    if kind is None:
        return

    def work(conn, cfg):
        # News is a public ESPN endpoint fetched without the fantasy session
        # cookies, so it has nothing to do with AuthInvalid below -- it runs
        # first so expired cookies do not blank a panel that never needed them.
        try:
            sync_news(conn, cfg.news_reporters, cfg.news_keep_days)
        except Exception as exc:  # noqa: BLE001 - a panel must not cost the scores
            log.warning("news sync failed: %s", exc)

        league = espn_client.get_league(cfg.current_season, refresh=True)
        sync_teams(conn, league, cfg.current_season)
        state = refresh_season_state(conn, league)
        changed = sync_history(conn)

        # Projections for the upcoming week. Best-effort: a failure here must
        # not cost us the scores.
        upcoming = max(1, int(getattr(league, "currentMatchupPeriod", None) or 1))
        # Each of these feeds one panel. None of them may cost us the scores --
        # nflverse in particular is a whole second origin that can be down while
        # ESPN is fine. The week is only named for the syncs that have one.
        for label, week_scoped, work_fn in (
            ("player reference", False, lambda: sync_players(conn)),
            ("nflverse ids", False, lambda: sync_nfl_ids(conn)),
            ("nflverse stats", False, lambda: sync_nfl_seasons(conn, cfg.current_season)),
            ("transactions", False, lambda: sync_activity(conn, cfg.current_season)),
            ("projections", True, lambda: sync_projections(conn, cfg.current_season, upcoming)),
            ("matchup lineups", True, lambda: sync_matchup_players(conn, cfg.current_season, upcoming)),
        ):
            try:
                work_fn()
            except Exception as exc:  # noqa: BLE001 - a panel must not cost the scores
                if week_scoped:
                    log.warning("%s sync failed for week %s: %s", label, upcoming, exc)
                else:
                    log.warning("%s sync failed: %s", label, exc)

        if state in (PRE_DRAFT, DRAFTED):
            # No games yet: keep the schedule fresh so the pre-season hub has
            # matchups to show, and watch for the draft landing.
            changed += poll_season(conn, cfg.current_season,
                                   weeks=schedule_periods(cfg.current_season))
            changed += sync_draft(conn, cfg.current_season)
            return changed

        week = int(getattr(league, "currentMatchupPeriod", None) or 1)
        projected = _live_projections(league, week) if state == IN_SEASON else {}
        changed += poll_week(conn, league, cfg.current_season, week, projected)
        return changed

    _run(kind, work)


def correction_sweep() -> None:
    """Re-poll recently completed weeks; this is where stat corrections land."""

    def work(conn, cfg):
        if _state not in (IN_SEASON, COMPLETE):
            return 0
        league = espn_client.get_league(cfg.current_season, refresh=True)
        current = int(getattr(league, "currentMatchupPeriod", None) or 1)
        weeks = [w for w in range(current - cfg.correction_weeks_back, current + 1) if w >= 1]
        changed = sum(poll_week(conn, league, cfg.current_season, w) for w in weeks)
        if changed:
            log.info("correction sweep revised %d row(s)", changed)
        return changed

    _run("correction", work)


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    cfg = get_config()
    scheduler = BackgroundScheduler(timezone=LEAGUE_TZ)
    scheduler.add_job(
        tick, "interval", seconds=cfg.live_interval,
        id="tick", max_instances=1, coalesce=True,
        next_run_time=datetime.now(LEAGUE_TZ) + timedelta(seconds=5),
    )
    scheduler.add_job(
        correction_sweep, "cron", hour=cfg.correction_sweep_hour, minute=15,
        id="corrections", max_instances=1, coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    log.info(
        "scheduler started: tick=%ss idle=%ss corrections=%02d:15 ET",
        cfg.live_interval, cfg.idle_interval, cfg.correction_sweep_hour,
    )
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None

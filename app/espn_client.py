"""ESPN API access -- every quirk of the unofficial API is contained here.

Nothing outside this module should know that ESPN's API is undocumented,
cookie-authenticated, or prone to changing shape mid-season. When ESPN breaks
something, it breaks here.

Failure policy: never raise into a page render. The database already holds the
last known-good data, so a failed poll degrades to stale-but-correct rather than
a 500.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import datetime, timezone

import requests

from .config import get_config

log = logging.getLogger(__name__)

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"

# espn-api's exception names have moved between releases; degrade to duck-typing
# on the class name rather than hard-failing on import.
try:  # pragma: no cover - depends on installed version
    from espn_api.requests.espn_requests import (
        ESPNAccessDenied,
        ESPNInvalidLeague,
        ESPNUnknownError,
    )
except ImportError:  # pragma: no cover
    class ESPNAccessDenied(Exception): ...
    class ESPNInvalidLeague(Exception): ...
    class ESPNUnknownError(Exception): ...


class AuthInvalid(RuntimeError):
    """Cookies are expired or rejected. Needs human action, not a retry."""


_lock = threading.Lock()
_leagues: dict[int, object] = {}
_auth_invalid_since: datetime | None = None
_last_success: datetime | None = None
_last_error: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mark_auth_invalid(message: str) -> None:
    global _auth_invalid_since, _last_error
    with _lock:
        if _auth_invalid_since is None:
            _auth_invalid_since = _now()
            log.error("ESPN auth rejected -- refresh cookies in config/secrets.env: %s", message)
        _last_error = message
        _leagues.clear()  # force rebuild once new cookies land


def _mark_success() -> None:
    global _auth_invalid_since, _last_success, _last_error
    with _lock:
        _auth_invalid_since = None
        _last_success = _now()
        _last_error = None


def auth_status() -> dict:
    cfg = get_config()
    return {
        "credentials_present": cfg.has_credentials,
        "auth_invalid": _auth_invalid_since is not None,
        "auth_invalid_since": _auth_invalid_since.isoformat() if _auth_invalid_since else None,
        "last_success": _last_success.isoformat() if _last_success else None,
        "last_error": _last_error,
    }


def _is_auth_error(exc: Exception) -> bool:
    if isinstance(exc, ESPNAccessDenied):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in (401, 403)
    return "denied" in str(exc).lower() or "401" in str(exc) or "403" in str(exc)


def _retry_after(exc: Exception) -> int | None:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        if exc.response.status_code == 429:
            try:
                return int(exc.response.headers.get("Retry-After", 0)) or None
            except (TypeError, ValueError):
                return None
    return None


def _is_rate_limited(exc: Exception) -> bool:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code == 429
    return "429" in str(exc) or "too many requests" in str(exc).lower()


def call(fn, *args, attempts: int = 4, **kwargs):
    """Invoke an ESPN-touching callable with backoff and auth tracking.

    Auth errors are not retried -- expired cookies stay expired, and hammering
    the endpoint is how an IP earns a longer block.
    """
    delay = 2.0
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = fn(*args, **kwargs)
            _mark_success()
            return result
        except Exception as exc:  # noqa: BLE001 - third-party raises broadly
            last = exc
            if _is_auth_error(exc):
                _mark_auth_invalid(str(exc))
                raise AuthInvalid(str(exc)) from exc
            if attempt == attempts:
                break
            wait = _retry_after(exc) or delay
            if _is_rate_limited(exc):
                wait = max(wait, delay * 2)
                log.warning("ESPN rate limited; backing off %.1fs", wait)
            # Jitter so a restart loop cannot synchronise into a thundering herd.
            time.sleep(wait + random.uniform(0, 1.5))
            delay *= 2
    global _last_error
    _last_error = str(last)
    log.error("ESPN call failed after %d attempts: %s", attempts, last)
    raise last  # type: ignore[misc]


def get_league(season: int, refresh: bool = False):
    """Cached espn_api League for a season."""
    from espn_api.football import League

    if not refresh and season in _leagues:
        return _leagues[season]

    cfg = get_config()
    if not cfg.has_credentials:
        raise AuthInvalid("ESPN_S2 / SWID are not set -- see config/secrets.env")

    league = call(
        League,
        league_id=cfg.league_id,
        year=season,
        espn_s2=cfg.espn_s2,
        swid=cfg.swid,
    )
    with _lock:
        _leagues[season] = league
    return league


def _cookies() -> dict:
    cfg = get_config()
    return {"espn_s2": cfg.espn_s2, "SWID": cfg.swid}


def _get(url: str, params: dict, headers: dict | None = None):
    def _fetch():
        response = requests.get(
            url, params=params, headers=headers or {},
            cookies=_cookies(), timeout=25,
        )
        response.raise_for_status()
        return response.json()

    return call(_fetch)


def _current_url() -> str:
    cfg = get_config()
    return f"{BASE}/seasons/{cfg.current_season}/segments/0/leagues/{cfg.league_id}"


def _history_url() -> str:
    return f"{BASE}/leagueHistory/{get_config().league_id}"


def league_history(views: list[str] | None = None) -> dict[int, dict]:
    """Every past season in one request, keyed by season.

    The `seasonId` parameter must be OMITTED. With it, ESPN answers 200 with an
    empty shell of the *current* league and no records -- which reads exactly
    like "this league has no history" and is why the history looked absent at
    first. Without it, the endpoint returns a list of every past season.

    Season grain only: four ways of asking for weekly schedules here
    (scoringPeriodId, an x-fantasy-filter matchup filter, mScoreboard,
    mBoxscore) all come back with zero schedule rows. Season totals are the
    ceiling for historical data.
    """
    views = views or ["mTeam", "mSettings", "mStandings"]
    body = _get(_history_url(), {"view": views})
    if isinstance(body, dict):
        body = [body]
    return {
        int(entry["seasonId"]): entry
        for entry in body
        if entry.get("seasonId") is not None
    }


def raw_view(season: int, view: str | list[str]) -> dict:
    """Escape hatch for data espn-api does not expose.

    Past seasons 404 on the per-season path regardless of year, so they are
    served out of the history payload instead.
    """
    cfg = get_config()
    views = [view] if isinstance(view, str) else list(view)
    if season == cfg.current_season:
        return _get(_current_url(), {"view": views})
    return league_history(views).get(season, {})


def player_projections(season: int, week: int, limit: int = 2000) -> list[dict]:
    """Every rankable player with their projection for one week.

    Works before a draft: pre-draft the whole universe is FREEAGENT. ONTEAM is
    in the status filter deliberately -- without it this page would silently
    become free-agents-only the moment the draft happens.

    ESPN rejects a `limit` with no `sort`, and its own
    sortAppliedStatTotalForScoringPeriodId returns nonsense ordering, so we sort
    by ownership to get a sensible pool and rank by projection ourselves.

    The limit is 2000 rather than 1000 so every position group can fill 50 rows:
    at 1000 the pool held only 45 QBs and 37 kickers. D/ST tops out at 32 by
    definition -- there are 32 NFL defenses.

    Both the projection (statSourceId 1) and the actual result (statSourceId 0)
    are returned for the week; the actual is None until the games are played.
    """
    from .players import position_for

    try:
        from espn_api.football.constant import PRO_TEAM_MAP
    except ImportError:  # pragma: no cover
        PRO_TEAM_MAP = {}

    cfg = get_config()
    url = f"{BASE}/seasons/{season}/segments/0/leagues/{cfg.league_id}"
    filters = {
        "players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
            "limit": limit,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        }
    }
    payload = _get(
        url,
        {"view": "kona_player_info", "scoringPeriodId": week},
        {"x-fantasy-filter": json.dumps(filters)},
    )

    rows: list[dict] = []
    for entry in payload.get("players") or []:
        player = entry.get("player") or {}
        projected = actual = None
        for stat in player.get("stats") or []:
            if stat.get("scoringPeriodId") != week:
                continue
            if stat.get("statSourceId") == 1:
                projected = stat.get("appliedTotal")
            elif stat.get("statSourceId") == 0:
                actual = stat.get("appliedTotal")
        if projected is None and actual is None:
            continue
        position = position_for(player.get("defaultPositionId"))
        if position is None:
            continue
        rows.append({
            "player_id": player.get("id"),
            "name": player.get("fullName"),
            "position": position,
            "pro_team": _pro_team(PRO_TEAM_MAP, player) or "FA",
            "projected": round(projected, 1) if projected is not None else None,
            "actual": round(actual, 1) if actual is not None else None,
            "percent_owned": round((player.get("ownership") or {}).get("percentOwned") or 0, 1),
            "on_team_id": entry.get("onTeamId") or 0,
        })
    return rows


NFL_ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{abbr}/roster"


def nfl_experience() -> dict[int, dict]:
    """Every active NFL player's years of experience, keyed by athlete id.

    The fantasy API carries no rookie field, and inferring one from missing
    prior-season stats would misread anyone who sat out a season. `experience`
    on the NFL team rosters is the real thing.

    Athlete ids here are the same id space as fantasy player ids (Jahmyr Gibbs
    is 4429795 in both), so the join is direct -- no name matching.

    This endpoint is public: it is fetched WITHOUT the ESPN session cookies.
    There is no reason to hand a fantasy session to a different host.
    """
    try:
        from espn_api.football.constant import PRO_TEAM_MAP
    except ImportError:  # pragma: no cover
        return {}

    out: dict[int, dict] = {}
    for abbr in sorted({v for v in PRO_TEAM_MAP.values() if v and v != "None"}):
        try:
            response = requests.get(NFL_ROSTER_URL.format(abbr=abbr), timeout=25)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - one team must not cost the sync
            log.warning("NFL roster for %s unavailable: %s", abbr, exc)
            continue
        for group in payload.get("athletes") or []:
            for athlete in group.get("items") or []:
                try:
                    athlete_id = int(athlete["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                years = (athlete.get("experience") or {}).get("years")
                out[athlete_id] = {
                    "name": athlete.get("displayName"),
                    "pro_team": abbr,
                    "years": years,
                }
    return out


def _pro_team(pro_team_map: dict, player: dict) -> str | None:
    """NFL team abbreviation for a player, or None if they are unsigned.

    espn-api maps proTeamId 0 to the *string* `"None"`, not to `None`. It is
    truthy, so every `or` fallback in sight silently lets it through and a free
    agent ends up playing for a team called None.
    """
    abbrev = pro_team_map.get(player.get("proTeamId"))
    return abbrev if abbrev and abbrev != "None" else None


def matchup_rosters(season: int, week: int) -> dict[int, list[dict]]:
    """Each team's roster for one week, keyed by team id.

    Clamped to week >= 1 deliberately: at scoringPeriodId 0 the payload has no
    `rosterForCurrentScoringPeriod` key at all, which is the exact KeyError that
    made box_scores unusable before the draft.
    """
    from .players import position_for

    try:
        from espn_api.football.constant import POSITION_MAP, PRO_TEAM_MAP
    except ImportError:  # pragma: no cover
        POSITION_MAP = PRO_TEAM_MAP = {}

    scoring_period = max(1, int(week or 1))
    payload = _get(
        _current_url(),
        {"view": ["mMatchupScore", "mScoreboard"], "scoringPeriodId": scoring_period},
    )

    out: dict[int, list[dict]] = {}
    for matchup in payload.get("schedule") or []:
        if matchup.get("matchupPeriodId") != week:
            continue
        for side in ("home", "away"):
            entry_side = matchup.get(side) or {}
            team_id = entry_side.get("teamId")
            if not team_id:
                continue
            roster = (entry_side.get("rosterForCurrentScoringPeriod") or {}).get("entries") or []
            people = []
            for entry in roster:
                player = (entry.get("playerPoolEntry") or {}).get("player") or {}
                projected = actual = None
                for stat in player.get("stats") or []:
                    if stat.get("scoringPeriodId") != scoring_period:
                        continue
                    if stat.get("statSourceId") == 1:
                        projected = stat.get("appliedTotal")
                    elif stat.get("statSourceId") == 0:
                        actual = stat.get("appliedTotal")
                people.append({
                    "player_id": player.get("id") or entry.get("playerId"),
                    "name": player.get("fullName"),
                    "position": position_for(player.get("defaultPositionId")),
                    # lineupSlotId IS the slot enum -- POSITION_MAP is correct here,
                    # unlike for defaultPositionId.
                    "lineup_slot": POSITION_MAP.get(entry.get("lineupSlotId"), ""),
                    # Taken from this payload rather than joined from the daily
                    # roster sync: that sync only knows players on an NFL roster
                    # at the time it ran, so it has no row at all for a team
                    # defence and misses anyone signed since. proTeamId is on
                    # every entry here, D/ST included.
                    "pro_team": _pro_team(PRO_TEAM_MAP, player),
                    # Both already ride along on every entry; ESPN's own site
                    # shows them and we were dropping them on the floor.
                    # injuryStatus is the report ("QUESTIONABLE", "OUT"),
                    # player.injured the blunter boolean behind it.
                    "injury_status": entry.get("injuryStatus"),
                    "injured": bool(player.get("injured")),
                    "projected": round(projected, 1) if projected is not None else None,
                    "actual": round(actual, 1) if actual is not None else None,
                })
            out[int(team_id)] = people
    return out


# ESPN message-type ids for the transaction feed. Note 179/181/239 all mean
# "dropped" -- they differ only in how the drop happened (manual, roster-limit,
# failed waiver), a distinction nothing downstream cares about.
ACTIVITY_TYPES = {
    178: "added", 180: "claimed", 244: "traded",
    179: "dropped", 181: "dropped", 239: "dropped",
}


def recent_activity(size: int = 100, offset: int = 0) -> list[dict]:
    """League transactions: adds, waiver claims, drops and trades.

    Deliberately not espn-api's `recent_activity()`. That helper resolves every
    player it cannot find on a current roster with a separate `player_info`
    request -- and a dropped player is by definition not on a roster, so a feed
    of drops costs one extra round trip each. Here the ids come back raw and are
    named from our own players table.

    The `to`/`from`/`for` split is ESPN's, not ours: a trade names the team the
    player left, a failed-waiver drop names the team it was for, and everything
    else names the team that received them.
    """
    filters = {"topics": {
        "filterType": {"value": ["ACTIVITY_TRANSACTIONS"]},
        "limit": size,
        "limitPerMessageSet": {"value": 25},
        "offset": offset,
        "sortMessageDate": {"sortPriority": 1, "sortAsc": False},
        "sortFor": {"sortPriority": 2, "sortAsc": False},
        "filterIncludeMessageTypeIds": {"value": sorted(ACTIVITY_TYPES)},
    }}
    payload = _get(
        f"{_current_url()}/communication/",
        {"view": "kona_league_communication"},
        headers={"x-fantasy-filter": json.dumps(filters)},
    )

    out = []
    for topic in payload.get("topics") or []:
        for message in topic.get("messages") or []:
            type_id = message.get("messageTypeId")
            action = ACTIVITY_TYPES.get(type_id)
            if not action:
                continue
            if type_id == 244:
                team_id = message.get("from")
            elif type_id == 239:
                team_id = message.get("for")
            else:
                team_id = message.get("to")
            out.append({
                "message_id": message.get("id"),
                "date_ms": topic.get("date"),
                "action": action,
                "team_id": team_id,
                "player_id": message.get("targetId"),
                # Only a waiver claim carries a bid, and it arrives in `from`,
                # the same key a trade uses for the team id.
                "bid": message.get("from") if action == "claimed" else None,
            })
    return out


def draft_detail(season: int | None = None) -> dict:
    """draftDetail for a season.

    `drafted` is the only trustworthy signal: before the draft ESPN already
    returns a full array of empty placeholder picks (224 of them for this
    league), so a truthy `picks` proves nothing.
    """
    cfg = get_config()
    season = season or cfg.current_season
    payload = raw_view(season, "mDraftDetail")
    detail = payload.get("draftDetail") or {}
    picks = detail.get("picks") or []
    return {
        "drafted": bool(detail.get("drafted")),
        "in_progress": bool(detail.get("inProgress")),
        "picks": picks if detail.get("drafted") else [],
        "pick_slots": len(picks),
    }


def season_status(season: int | None = None) -> dict:
    """Status and draft settings driving the state machine and the countdown."""
    cfg = get_config()
    season = season or cfg.current_season
    payload = raw_view(season, ["mSettings", "mNav"])
    status = payload.get("status") or {}
    settings = payload.get("settings") or {}
    draft = settings.get("draftSettings") or {}
    schedule = settings.get("scheduleSettings") or {}
    return {
        "season": season,
        "name": settings.get("name"),
        "size": settings.get("size"),
        "teams_joined": status.get("teamsJoined"),
        "is_full": status.get("isFull"),
        "scoring_period": payload.get("scoringPeriodId"),
        "current_matchup_period": status.get("currentMatchupPeriod"),
        "final_scoring_period": status.get("finalScoringPeriod"),
        "latest_scoring_period": status.get("latestScoringPeriod"),
        "previous_seasons": sorted(status.get("previousSeasons") or []),
        "draft_type": draft.get("type"),
        "draft_date_ms": draft.get("date"),
        "keeper_count": draft.get("keeperCount"),
        "matchup_period_count": schedule.get("matchupPeriodCount"),
        "playoff_team_count": schedule.get("playoffTeamCount"),
    }

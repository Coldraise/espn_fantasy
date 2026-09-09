"""FastAPI application.

Every page reads from SQLite, never from ESPN. The poller is the only thing that
talks to ESPN, so a page render cannot be slowed by ESPN's latency or broken by
its outage -- it just shows the last known-good data with a staleness note.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.templating import Jinja2Templates

from . import (analytics, auth, db, espn_client, nflstats, nflweeks, players,
               poller, rules)
from .config import LEAGUE_TZ, ConfigError, get_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("fantasy")

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
# Lineup rows are too narrow on a phone for a full name and an NFL team both.
templates.env.filters["short_name"] = players.short_name
# The card prints a lineup slot once down its middle, which needs both
# franchises' starters aligned on that slot rather than each sorted alone.
templates.env.globals["pair_lineups"] = players.pair_lineups

_DAY_ABBR = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")


def _kickoff_label(value: str | None) -> str:
    """Kickoff as e.g. "Th 2:35".

    The day and the time deliberately read off two different clocks, which is
    the same split `config.display_tz` and `nflweeks` already draw: the day is
    the NFL's own, in LEAGUE_TZ, because Thursday Night Football is a Thursday
    game to everyone who talks about it -- while the time is whatever the
    reader's clock says, in display_tz, because 02:35 is when they would have
    to be awake for it.
    """
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return ""
    day = _DAY_ABBR[moment.astimezone(LEAGUE_TZ).weekday()]
    local = moment.astimezone(get_config().display_tz)
    return f"{day} {local.hour}:{local.minute:02d}"


# Kickoff time in the reader's own clock, day-labelled off the NFL's clock.
templates.env.filters["kickoff"] = _kickoff_label


def _asset_version() -> str:
    """Content hash of the stylesheet, appended to its URL.

    This is load-bearing rather than housekeeping. What a matchup card shows --
    a top three or the whole lineup, a shortened name or a full one -- is
    decided in CSS now, not in the template, so a browser holding an old
    stylesheet does not merely look dated, it shows the wrong thing entirely.
    A hashed URL means a changed stylesheet is a different file to fetch.
    """
    try:
        return hashlib.sha256((HERE / "static" / "style.css").read_bytes()).hexdigest()[:10]
    except OSError:
        return "dev"


templates.env.globals["asset_v"] = _asset_version()


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()  # raises ConfigError before the server ever binds
    with db.session(cfg.db_path) as conn:
        db.init_db(conn)
        # Mint the session-signing key here rather than lazily in a request:
        # two concurrent first requests would otherwise each generate one and
        # the loser's cookies would be dead on arrival.
        auth.session_secret(conn)
        _ensure_accounts(conn, cfg)
    log.info("league %s, seasons %s, db %s", cfg.league_id, list(cfg.seasons), cfg.db_path)
    if not cfg.has_credentials:
        log.warning("ESPN_S2 / SWID missing -- polling disabled until set in config/secrets.env")
    poller.start_scheduler()
    try:
        yield
    finally:
        poller.shutdown_scheduler()


app = FastAPI(title="League Monitor", lifespan=lifespan)
class _RevalidatingStatic(StaticFiles):
    """Static files a browser must not sit on.

    The stylesheet link carries a content hash, so a changed stylesheet is a
    changed URL and can never be served stale. The files the CSS itself pulls
    in -- the pitch and the stadium -- keep fixed URLs, so they get this
    instead: cache freely, but check with the ETag before reusing. A 304 is a
    few bytes, and the alternative is a browser holding a stale asset for as
    long as its heuristics feel like it.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# The players page renders every player twice -- once grouped for a wide
# screen, once ranked flat for a narrow one. Uncompressed that is a few hundred
# kilobytes; gzipped it is a small fraction of that.
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.mount("/static", _RevalidatingStatic(directory=str(HERE / "static")), name="static")


# --- authentication -------------------------------------------------------

# Everything else is behind a franchise login. The health endpoint stays open
# because Compose probes it with no cookie jar, and static assets are needed to
# render the login page itself.
PUBLIC_PATHS = {"/health", "/login", "/logout", "/favicon.ico"}


def _ensure_accounts(conn, cfg) -> None:
    """Seed a login for every franchise in the current season.

    Called at startup and again from the login page: on a first run the teams
    table is empty until the poller has been round once, and an admin should not
    have to restart the container to get the accounts that appear after it.
    """
    franchises = {int(t["team_id"]): t["name"] for t in db.fetch_teams(conn, cfg.current_season)}
    if not franchises:
        return
    created = auth.seed_accounts(conn, franchises)
    if created:
        path = auth.write_credentials_file(Path(cfg.db_path).parent / "credentials.txt", created)
        log.warning("created %d franchise login(s): %s -- passwords written to %s",
                    len(created), ", ".join(c["username"] for c in created), path)


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        request.state.user = None
        return await call_next(request)

    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        user = auth.read_token(conn, request.cookies.get(auth.SESSION_COOKIE))
    if user is None:
        nxt = request.url.path
        if request.url.query:
            nxt = f"{nxt}?{request.url.query}"
        return RedirectResponse(f"/login?next={quote(nxt, safe='')}", status_code=303)
    request.state.user = user
    return await call_next(request)


def _current_user(request: Request) -> dict | None:
    return getattr(request.state, "user", None)


def _safe_next(raw: str | None) -> str:
    """Only ever redirect back to a path on this site -- '//evil.example' and
    'https://…' are both rejected, which is the whole open-redirect class."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


# --- helpers --------------------------------------------------------------


def _base_context(request: Request, conn) -> dict:
    cfg = get_config()
    status = espn_client.auth_status()
    last_ok = db.last_successful_poll(conn)
    latest = db.latest_poll(conn)
    poll_error = None
    if latest and latest.get("status") == "error" and not status["auth_invalid"]:
        poll_error = latest.get("error")
    stale_minutes = None
    if last_ok and last_ok.get("finished_at"):
        finished = datetime.fromisoformat(last_ok["finished_at"])
        stale_minutes = int((datetime.now(timezone.utc) - finished).total_seconds() // 60)
    meta = db.get_meta(conn, "status") or {}
    draft_at = None
    draft_iso = meta.get("draft_date_utc")
    if draft_iso:
        try:
            draft_at = datetime.fromisoformat(draft_iso).astimezone(cfg.display_tz)
        except ValueError:
            draft_at = None

    user = _current_user(request)
    me_id = int(user["team_id"]) if user else None
    names = db.franchise_names(conn, cfg.franchise_since)

    # The header strip: best three in every lineup slot, and any touchdown a
    # rostered player has just scored. Both are on every page because the
    # header is.
    ticker: list[dict] = []
    td_events: list[dict] = []
    ticker_played = False
    weeks = db.projection_weeks(conn, cfg.current_season)
    if weeks:
        ticker_week = weeks[0]
        rows = db.fetch_player_projections(conn, cfg.current_season, ticker_week)
        # Before anyone has played, a projection is the only ranking there is.
        ticker_played = any((r.get("actual") or 0) for r in rows)
        ticker = players.leaders(
            rows, per=3, by="actual" if ticker_played else "projected")
        for event in db.recent_scoring_events(conn, cfg.current_season, ticker_week):
            event["owner"] = names.get(int(event["team_id"]), {}).get("name")
            td_events.append(event)

    return {
        "request": request,
        "auth": status,
        "me": user,
        "me_id": me_id,
        "me_name": names.get(me_id, {}).get("name")
                   or (user["username"] if user else None),
        "ticker": ticker,
        "ticker_live": ticker_played,
        "td_events": td_events,
        "cfg": cfg,
        "seasons": db.available_seasons(conn) or list(cfg.seasons),
        "history_seasons": db.history_seasons(conn),
        "stale_minutes": stale_minutes,
        "poll_error": poll_error,
        "live": poller.is_live_window(),
        "meta": meta,
        "state": meta.get("state") or poller.current_state(),
        "preseason": (meta.get("state") or poller.current_state()) in (poller.PRE_DRAFT, poller.DRAFTED),
        "drafted": bool(meta.get("drafted")),
        "draft_at": draft_at,
        "draft_iso": draft_iso,
        "has_weeks": db.has_week_data(conn),
        "tz_label": cfg.display_tz_name,
    }


def _schedule_grid(conn, season: int, names: dict) -> dict:
    """Every team's opponent per week -- the only 'what happens next' view that
    exists before any games are played."""
    rows = [dict(r) for r in conn.execute(
        "SELECT week, team_id, opponent_id FROM team_weeks WHERE season=? ORDER BY week", (season,)
    )]
    if not rows:
        return {"weeks": [], "rows": []}
    weeks = sorted({r["week"] for r in rows})
    by_team: dict[int, dict[int, int]] = {}
    for row in rows:
        by_team.setdefault(row["team_id"], {})[row["week"]] = row["opponent_id"]
    out = []
    for team_id, opponents in by_team.items():
        out.append({
            "team_id": team_id,
            "name": names.get(team_id, {}).get("name") or f"Team {team_id}",
            # Opponent id travels with the name so the grid can mark the
            # signed-in franchise's own column without matching on strings.
            "opponents": {
                w: {"id": opponents.get(w),
                    "name": names.get(opponents.get(w), {}).get("name")}
                for w in weeks
            },
        })
    out.sort(key=lambda r: r["name"].lower())
    # The grid stores week numbers only, so anything dated -- the trade
    # deadline, the two holidays -- has to be placed by deriving the calendar.
    settings = db.get_meta(conn, "settings") or {}
    deadline = (settings.get("tradeSettings") or {}).get("deadlineDate")
    return {"weeks": weeks, "rows": out,
            "markers": nflweeks.season_markers(season, weeks, deadline)}


def _scoreboard(conn, season: int) -> tuple[int | None, list[dict]]:
    row = conn.execute(
        "SELECT MAX(week) AS week FROM team_weeks WHERE season=?", (season,)
    ).fetchone()
    week = row["week"] if row else None
    if week is None:
        return None, []

    names = db.team_name_map(conn)
    rows = [
        dict(r) for r in conn.execute(
            "SELECT * FROM team_weeks WHERE season=? AND week=?", (season, week)
        )
    ]
    by_team = {r["team_id"]: r for r in rows}

    games, seen = [], set()
    for row in rows:
        team_id, opp_id = row["team_id"], row["opponent_id"]
        if team_id in seen:
            continue
        seen.add(team_id)
        if opp_id is None:
            games.append({
                "home": {"id": team_id, "name": names.get(team_id, f"Team {team_id}"),
                         "points": row["points"], "projected": row["projected"]},
                "away": None, "bye": True, "revision": row["revision"],
            })
            continue
        seen.add(opp_id)
        opponent = by_team.get(opp_id, {})
        games.append({
            "home": {"id": team_id, "name": names.get(team_id, f"Team {team_id}"),
                     "points": row["points"], "projected": row["projected"]},
            "away": {"id": opp_id, "name": names.get(opp_id, f"Team {opp_id}"),
                     "points": opponent.get("points"), "projected": opponent.get("projected")},
            "bye": False,
            "revision": max(row["revision"], opponent.get("revision", 1)),
            "final": row["result"] is not None,
        })
    games.sort(key=lambda g: -((g["home"]["points"] or 0) + ((g["away"] or {}).get("points") or 0)))
    return week, games


def _split_games(games: list[dict], team_id: int | None) -> tuple[dict | None, list[dict]]:
    """Pull the signed-in franchise's own matchup out of the week's games.

    Returned separately rather than just sorted first, because the two are
    rendered at different sizes -- your game full-detail, the rest compact.
    """
    if team_id is None:
        return None, games
    for index, game in enumerate(games):
        away = game.get("away") or {}
        if game["home"]["id"] == team_id or away.get("id") == team_id:
            return game, games[:index] + games[index + 1:]
    return None, games


def _card_context(conn, season: int, week: int | None) -> dict:
    """Lineups, managers and all-time records for the matchup cards.

    Records come from the cutoff-filtered view so a franchise that changed hands
    does not inherit the previous owner's trophies.
    """
    cfg = get_config()
    names = db.franchise_names(conn, cfg.franchise_since)
    season_rows = analytics.filter_franchise_history(
        db.fetch_team_seasons(conn), cfg.franchise_since)
    records = {}
    for row in analytics.all_time_standings(season_rows, names):
        titles = row["titles"]
        records[row["team_id"]] = {
            "wins": row["wins"], "losses": row["losses"], "ties": row["ties"],
            "pct": row["pct"], "titles": titles, "seasons": row["seasons"],
            "avg_finish": row["avg_finish"],
            "tip": f"{row['wins']}-{row['losses']} all-time"
                   + (f" · {titles} title{'s' if titles != 1 else ''}" if titles else ""),
        }
    # One query for the whole roster, starters derived from it in Python --
    # the modal needs the bench too, and a second query for the same rows
    # would be pointless.
    rosters = db.fetch_team_week_players(conn, season, week, starters_only=False) if week else {}
    lineups = {tid: [p for p in ps if p.get("is_starter")] for tid, ps in rosters.items()}

    # Keyed on both sides of each game so a player's pro_team looks it up
    # directly -- the join has no other id in common.
    games: dict[str, dict] = {}
    if week:
        for row in db.fetch_nfl_games(conn, season, week):
            for team in (row.get("home_team"), row.get("away_team")):
                if team:
                    games[team] = row

    return {
        "lineups": lineups,
        "rosters": rosters,
        "managers": {t["team_id"]: t.get("owner")
                     for t in db.fetch_teams(conn, cfg.current_season)},
        "records": records,
        "team_colors": players.PRO_TEAM_COLORS,
        "games": games,
    }


def _lineup_efficiency(conn, season: int) -> list[dict]:
    """Season-long lineup efficiency: points scored vs points startable.

    Needs the bench, which is only stored from the week the roster sync started
    keeping it, so this is honest about covering fewer weeks than the standings.
    """
    starters, _ = rules.split_slots(db.get_meta(conn, "settings") or {})
    if not starters:
        return []
    names = db.team_name_map(conn)
    weeks = [int(r[0]) for r in conn.execute(
        "SELECT DISTINCT week FROM team_week_players WHERE season=? ORDER BY week", (season,))]

    totals: dict[int, dict] = {}
    for week in weeks:
        lineups = db.fetch_team_week_players(conn, season, week, starters_only=False)
        for team_id, entry in analytics.points_left_on_bench(lineups, starters).items():
            running = totals.setdefault(
                team_id, {"team_id": team_id, "weeks": 0, "actual": 0.0,
                          "optimal": 0.0, "left_on_bench": 0.0})
            running["weeks"] += 1
            running["actual"] += entry["actual"]
            running["optimal"] += entry["optimal"]
            running["left_on_bench"] += entry["left_on_bench"]

    out = []
    for team_id, entry in totals.items():
        entry["name"] = names.get(team_id, f"Team {team_id}")
        entry["actual"] = round(entry["actual"], 1)
        entry["optimal"] = round(entry["optimal"], 1)
        entry["left_on_bench"] = round(entry["left_on_bench"], 1)
        entry["efficiency"] = (round(entry["actual"] / entry["optimal"], 3)
                               if entry["optimal"] else None)
        out.append(entry)
    out.sort(key=lambda e: -(e["efficiency"] or 0))
    return out


def _season_param(request: Request, conn) -> int:
    cfg = get_config()
    raw = request.query_params.get("season")
    available = db.available_seasons(conn)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return available[0] if available else cfg.current_season


# --- routes ---------------------------------------------------------------


@app.get("/health")
def health():
    """Must answer even when ESPN is unreachable -- Compose healthchecks it."""
    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        latest = db.latest_poll(conn)
        last_ok = db.last_successful_poll(conn)
        seasons = db.available_seasons(conn)
        weeks = conn.execute("SELECT COUNT(*) FROM team_weeks").fetchone()[0]
    status = espn_client.auth_status()
    # Polling can fail with perfectly valid cookies -- a wrong league id, an
    # ESPN outage, a shape change. Reporting "ok" in that state would hide the
    # exact failure this service exists to notice.
    poll_ok = latest is None or latest.get("status") != "error"
    healthy = status["credentials_present"] and not status["auth_invalid"] and poll_ok

    reasons = []
    if not status["credentials_present"]:
        reasons.append("no credentials")
    if status["auth_invalid"]:
        reasons.append("cookies rejected")
    if not poll_ok:
        reasons.append(f"last poll failed: {latest.get('error')}")

    return JSONResponse(
        # Deliberately 200 even when degraded: restarting the container cannot
        # fix expired cookies, so flapping the healthcheck would only add noise.
        status_code=200,
        content={
            "status": "ok" if healthy else "degraded",
            "reasons": reasons,
            "auth": status,
            "poll_ok": poll_ok,
            "live_window": poller.is_live_window(),
            "latest_poll": latest,
            "last_successful_poll": last_ok,
            "seasons_stored": seasons,
            "team_week_rows": weeks,
        },
    )


@app.get("/login")
def login_form(request: Request, error: str | None = None):
    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        _ensure_accounts(conn, cfg)
        meta = db.get_meta(conn, "status") or {}
        # Already signed in: skip the form rather than showing a logged-in
        # person a login page.
        if auth.read_token(conn, request.cookies.get(auth.SESSION_COOKIE)):
            return RedirectResponse(_safe_next(request.query_params.get("next")), status_code=303)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "meta": meta,
        "cfg": cfg,
        "error": request.query_params.get("error"),
        "next": _safe_next(request.query_params.get("next")),
    })


@app.post("/login")
def login_submit(request: Request,
                 username: str = Form(""),
                 password: str = Form(""),
                 next: str = Form("/")):
    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        user = auth.authenticate(conn, username, password)
        if user is None:
            log.info("failed login for %r", username[:40])
            return RedirectResponse(
                f"/login?error=1&next={quote(_safe_next(next), safe='')}", status_code=303)
        token = auth.make_token(conn, user)
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE, token,
        max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax",
        # The site runs behind plain HTTP on a LAN for some of the league, so a
        # Secure cookie would lock those people out entirely.
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@app.get("/")
def dashboard(request: Request):
    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        context = _base_context(request, conn)
        names = db.franchise_names(conn, cfg.franchise_since)

        if context["preseason"]:
            season = cfg.current_season
            _, games = _scoreboard(conn, season)
            my_game, other_games = _split_games(games, context["me_id"])
            context |= _card_context(conn, season, 1) | {
                "season": season,
                "names": names,
                "week": 1,
                "my_game": my_game,
                "other_games": other_games,
                "grid": _schedule_grid(conn, season, names),
            }
            return templates.TemplateResponse("preseason.html", context)

        season = _season_param(request, conn)
        week, games = _scoreboard(conn, season)
        my_game, other_games = _split_games(games, context["me_id"])
        context |= _card_context(conn, season, week) | {
            "season": season, "week": week,
            "my_game": my_game, "other_games": other_games,
            "corrections": db.recent_corrections(conn, 8),
            "names": db.team_name_map(conn),
            # The schedule used to vanish at the draft, which is exactly when a
            # trade-deadline marker starts being worth looking at.
            "grid": _schedule_grid(conn, season, names),
        }
    return templates.TemplateResponse("dashboard.html", context)


@app.get("/usage")
def usage(request: Request):
    """What a franchise's players actually do on an NFL field.

    Fantasy points say a player scored; snap share, target share and tackles say
    whether that was volume or a fluke. ESPN sends none of it, so this is the
    one page built on nflverse rather than on the league.
    """
    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        context = _base_context(request, conn)
        seasons = db.nfl_seasons(conn)
        try:
            season = int(request.query_params.get("season") or 0)
        except ValueError:
            season = 0
        if season not in seasons:
            season = seasons[0] if seasons else cfg.current_season

        # Rosters come from the newest week we have stored, which before the
        # season starts is week 1's provisional lineups.
        roster_weeks = [int(r[0]) for r in conn.execute(
            "SELECT DISTINCT week FROM team_week_players WHERE season=? ORDER BY week DESC",
            (cfg.current_season,))]
        roster_week = roster_weeks[0] if roster_weeks else None
        roster = [p for players_ in
                  (db.fetch_team_week_players(conn, cfg.current_season, roster_week)
                   if roster_week else {}).values() for p in players_]

        rows = nflstats.usage_rows(
            roster, db.nfl_id_map(conn),
            db.fetch_nfl_player_weeks(conn, season),
            db.fetch_nfl_team_weeks(conn, season),
        )
        names = db.franchise_names(conn, cfg.franchise_since)
        summary = nflstats.franchise_usage(rows)
        table = sorted(
            ({"team_id": tid, "name": names.get(tid, {}).get("name") or f"Team {tid}", **vals}
             for tid, vals in summary.items()),
            key=lambda r: -(r["ppr"] or 0))

        # Default to your own franchise -- the roster you actually care about.
        try:
            selected = int(request.query_params.get("team") or 0)
        except ValueError:
            selected = 0
        if selected not in summary:
            selected = context["me_id"] if context["me_id"] in summary else 0

        picked = [r for r in rows if r["team_id"] == selected] if selected else rows
        context |= {
            "season": season, "nfl_seasons": seasons,
            "roster_week": roster_week,
            "reference": season < cfg.current_season,
            "table": table, "names": names,
            "selected": selected,
            "groups": nflstats.split_by_unit(picked),
            "team_colors": players.PRO_TEAM_COLORS,
        }
    return templates.TemplateResponse("usage.html", context)


@app.get("/history")
def history(request: Request):
    """All-time records. The only page with real content before kickoff."""
    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        names = db.franchise_names(conn, cfg.franchise_since)
        season_rows = analytics.filter_franchise_history(
            db.fetch_team_seasons(conn), cfg.franchise_since)
        # Managers are only known for the current season: ESPN returns
        # owners: [] for every historical season.
        managers = {t["team_id"]: t.get("owner")
                    for t in db.fetch_teams(conn, cfg.current_season)}
        table = analytics.all_time_standings(season_rows, names)
        for row in table:
            row["manager"] = managers.get(row["team_id"])
        context = _base_context(request, conn) | {
            "table": table,
            "grid": analytics.season_grid(season_rows, names),
        }
    return templates.TemplateResponse("history.html", context)


@app.get("/players")
def player_rankings(request: Request):
    """Best projected players per lineup position for one week."""
    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        weeks = db.projection_weeks(conn, cfg.current_season)
        try:
            week = int(request.query_params.get("week") or 0)
        except ValueError:
            week = 0
        if week not in weeks:
            week = weeks[0] if weeks else 1
        available_only = request.query_params.get("available") == "1"
        pos = request.query_params.get("pos") or ""
        rows = db.fetch_player_projections(conn, cfg.current_season, week)
        rookies = db.rookie_ids(conn)
        # Last week's actual result, shown beside this week's projection.
        last_week = week - 1
        last = db.week_actuals(conn, cfg.current_season, last_week)
        for row in rows:
            row["rookie"] = row["player_id"] in rookies
            row["last"] = last.get(row["player_id"])
        # One pass: group_by_position always returns every group that has
        # players, so the tab list and the shown group come out of the same
        # call. A single position gets a deeper list -- there is only one card
        # to fill, so 50 would cut a ranking short for no reason.
        # No cap: "show all the players" means all of them. It is one render
        # and scrolling costs nothing, where a cap hides the deep end of the
        # pool, which is the part worth searching.
        all_groups = players.group_by_position(
            rows, per_group=None, available_only=available_only)
        positions = [g["label"] for g in all_groups]
        if pos not in positions:
            pos = ""
        # A phone shows one list ranked across every position rather than nine
        # stacked cards. It cannot be built by concatenating the groups, whose
        # order is per-group, so it is ranked here and rendered alongside them.
        flat = [] if pos else players.ranked(
            [r for r in rows
             if not (available_only and (r.get("on_team_id") or 0))],
            by="actual" if any((r.get("actual") or 0) for r in rows) else "projected")
        context = _base_context(request, conn) | {
            "week": week,
            "weeks": weeks,
            "available_only": available_only,
            "pos": pos,
            "positions": positions,
            "flat": flat,
            "groups": [g for g in all_groups if g["label"] == pos] if pos
                      else all_groups,
            "last_week": last_week if last_week >= 1 else None,
            "has_last": bool(last),
            "team_names": db.franchise_names(conn, cfg.franchise_since),
            "team_colors": players.PRO_TEAM_COLORS,
            "total": len(rows),
        }
    return templates.TemplateResponse("players.html", context)


@app.get("/activity")
def activity(request: Request):
    """Adds, drops, waiver claims and trades, newest first."""
    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        season = _season_param(request, conn)
        rows = db.fetch_transactions(conn, season)
        names = db.franchise_names(conn, cfg.franchise_since)
        churn: dict[int, dict] = {}
        for row in rows:
            entry = churn.setdefault(row["team_id"], {
                "team_id": row["team_id"], "added": 0, "claimed": 0,
                "dropped": 0, "traded": 0, "spent": 0})
            if row["action"] in entry:
                entry[row["action"]] += 1
            entry["spent"] += row["bid"] or 0
        for team_id, entry in churn.items():
            entry["name"] = names.get(team_id, {}).get("name") or f"Team {team_id}"
            entry["moves"] = entry["added"] + entry["claimed"] + entry["traded"]
        context = _base_context(request, conn) | {
            "season": season, "rows": rows, "names": names,
            "churn": sorted(churn.values(), key=lambda c: -c["moves"]),
        }
    return templates.TemplateResponse("activity.html", context)


@app.get("/news")
def news_page(request: Request):
    """ESPN's public NFL news feed, joined onto rosters by athlete id.

    Reporter chips filter client-side over the full list, same precedent as
    /rules -- a couple hundred rows means typing and toggling stay instant with
    no round trip.
    """
    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        context = _base_context(request, conn)
        rows = db.fetch_news(conn, limit=200)
        names = db.franchise_names(conn, cfg.franchise_since)

        # Rosters come from the newest week we have stored, which before the
        # season starts is week 1's provisional lineups -- same as /usage.
        roster_weeks = [int(r[0]) for r in conn.execute(
            "SELECT DISTINCT week FROM team_week_players WHERE season=? ORDER BY week DESC",
            (cfg.current_season,))]
        roster_week = roster_weeks[0] if roster_weeks else None
        by_team = (db.fetch_team_week_players(conn, cfg.current_season, roster_week,
                                              starters_only=False)
                   if roster_week else {})
        rostered: dict[int, dict] = {
            int(player["player_id"]): {"team_id": team_id, "name": player.get("name")}
            for team_id, roster in by_team.items() for player in roster
        }

        me_id = context["me_id"]
        mine, league_wide, everything_else = [], [], []
        for row in rows:
            row["published_at"] = None
            if row["published"]:
                try:
                    parsed = datetime.fromisoformat(row["published"])
                    # ESPN's own timestamps carry a Z, but guard the case they
                    # do not rather than silently reading them as local time.
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    row["published_at"] = parsed.astimezone(cfg.display_tz)
                except ValueError:
                    pass
            hits = [rostered[pid] for pid in row["athlete_ids"] if pid in rostered]
            row["players"] = [
                {**hit, "franchise": names.get(hit["team_id"], {}).get("name")}
                for hit in hits
            ]
            if any(hit["team_id"] == me_id for hit in hits):
                mine.append(row)
            elif hits:
                league_wide.append(row)
            else:
                everything_else.append(row)

        context |= {
            "mine": mine,
            "league_wide": league_wide,
            "everything_else": everything_else,
            "reporters": cfg.news_reporters,
        }
    return templates.TemplateResponse("news.html", context)


@app.get("/rules")
def league_rules(request: Request):
    """Every league setting, searchable. Filtering is client-side over the full
    list so typing stays instant and works with no network round trip."""
    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        categories = db.get_meta(conn, "rules") or []
        context = _base_context(request, conn) | {
            "categories": categories,
            "flat": rules.flatten(categories),
        }
    return templates.TemplateResponse("rules.html", context)


@app.get("/draft")
def draft(request: Request):
    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        season = cfg.current_season
        picks = db.fetch_draft_picks(conn, season)
        names = db.franchise_names(conn, cfg.franchise_since)
        # Index each round by team, not by pick order: a snake draft reverses on
        # even rounds, so rendering picks sequentially under fixed team columns
        # would attribute every even round's picks to the wrong manager.
        rounds: dict[int, dict[int, dict]] = {}
        for pick in picks:
            rounds.setdefault(pick["round"] or 0, {})[pick["team_id"]] = pick
        # Season points per player, for judging what each pick returned. Comes
        # from nflverse via the id map, so it works for past seasons too.
        id_map = db.nfl_id_map(conn)
        gsis_of = {espn_id: v["gsis_id"] for espn_id, v in id_map.items() if v.get("gsis_id")}
        scored: dict[str, float] = {}
        for row in db.fetch_nfl_player_weeks(conn, season):
            scored[row["gsis_id"]] = scored.get(row["gsis_id"], 0.0) + (row["fantasy_points_ppr"] or 0)
        points = {espn_id: scored[gsis] for espn_id, gsis in gsis_of.items() if gsis in scored}
        positions = {espn_id: v.get("position") for espn_id, v in id_map.items()}

        value = analytics.draft_value(picks, points, positions)
        context = _base_context(request, conn) | {
            "season": season, "picks": picks, "rounds": sorted(rounds.items()),
            "names": names, "teams": db.fetch_teams(conn, season),
            "value": value[:10], "flops": value[-10:][::-1] if value else [],
            "by_team": analytics.draft_value_by_team(value),
            "runs": analytics.positional_runs(picks, positions),
        }
    return templates.TemplateResponse("draft.html", context)


@app.get("/fragment/scoreboard")
def scoreboard_fragment(request: Request):
    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        context = _base_context(request, conn)
        season = _season_param(request, conn)
        week, games = _scoreboard(conn, season)
        my_game, other_games = _split_games(games, context["me_id"])
        context |= _card_context(conn, season, week) | {
            "season": season, "week": week,
            "my_game": my_game, "other_games": other_games,
        }
    return templates.TemplateResponse("_scoreboard.html", context)


@app.get("/standings")
def standings(request: Request):
    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        season = _season_param(request, conn)
        all_time = request.query_params.get("scope") == "all"
        rows = db.fetch_team_weeks(conn, None if all_time else [season])
        names = db.team_name_map(conn)
        table = analytics.standings_table(rows, names)

        # Joined on rather than folded into standings_table: these answer a
        # different question (how good is this team) than the standings do
        # (what is its record), and the page shows them as separate tables.
        spread = analytics.consistency(rows)
        accuracy = analytics.projection_accuracy(rows)
        power = analytics.power_rankings(rows)
        for entry in power:
            entry["name"] = names.get(entry["team_id"], f"Team {entry['team_id']}")
            entry |= {"spread": spread.get(entry["team_id"], {}),
                      "accuracy": accuracy.get(entry["team_id"], {})}

        context = _base_context(request, conn) | {
            "season": season, "all_time": all_time, "table": table,
            "power": power,
            "efficiency": _lineup_efficiency(conn, season),
            "names": names,
        }
    return templates.TemplateResponse("standings.html", context)


@app.get("/h2h")
def head_to_head(request: Request):
    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        all_time = request.query_params.get("scope") != "season"
        season = _season_param(request, conn)
        rows = db.fetch_team_weeks(conn, None if all_time else [season])
        matrix = analytics.head_to_head(rows)
        names = {tid: n["name"] for tid, n in db.franchise_names(conn, cfg.franchise_since).items()}
        teams = sorted(matrix.keys(), key=lambda t: names.get(t, str(t)).lower())
        context = _base_context(request, conn) | {
            "season": season, "all_time": all_time,
            "matrix": matrix, "teams": teams, "names": names,
        }
    return templates.TemplateResponse("h2h.html", context)

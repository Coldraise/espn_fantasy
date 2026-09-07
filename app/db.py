"""SQLite storage.

The grain is one row per team per week (`team_weeks`), not one row per matchup.
That shape is what makes the all-play and schedule-swap math fall out without
special-casing byes or odd team counts.

Scores are mutable: ESPN issues stat corrections for days after games finish.
Writes are therefore snapshot-and-revise -- an unchanged score is a no-op, and a
changed one bumps `revision` so corrections stay visible instead of silently
rewriting history.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import nflverse as _nflverse

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    season      INTEGER NOT NULL,
    team_id     INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    owner       TEXT,
    abbrev      TEXT,
    PRIMARY KEY (season, team_id)
);

CREATE TABLE IF NOT EXISTS team_weeks (
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    team_id     INTEGER NOT NULL,
    points      REAL,
    projected   REAL,
    opponent_id INTEGER,
    is_playoff  INTEGER NOT NULL DEFAULT 0,
    result      TEXT,                      -- 'W' | 'L' | 'T' | NULL while in progress
    revision    INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (season, week, team_id)
);

CREATE INDEX IF NOT EXISTS idx_team_weeks_season_week ON team_weeks (season, week);
CREATE INDEX IF NOT EXISTS idx_team_weeks_team        ON team_weeks (team_id);

-- Season-grain history. ESPN serves 2019-2025 as season totals only -- no
-- weekly schedule is retrievable -- so this cannot be folded into team_weeks.
-- Head-to-head, all-play and luck all need team_weeks and are
-- therefore 2026-onward only.
CREATE TABLE IF NOT EXISTS team_seasons (
    season         INTEGER NOT NULL,
    team_id        INTEGER NOT NULL,
    name           TEXT,
    wins           INTEGER,
    losses         INTEGER,
    ties           INTEGER,
    points_for     REAL,
    points_against REAL,
    final_rank     INTEGER,
    playoff_seed   INTEGER,
    revision       INTEGER NOT NULL DEFAULT 1,
    updated_at     TEXT    NOT NULL,
    PRIMARY KEY (season, team_id)
);

CREATE TABLE IF NOT EXISTS draft_picks (
    season       INTEGER NOT NULL,
    overall_pick INTEGER NOT NULL,
    round        INTEGER,
    round_pick   INTEGER,
    team_id      INTEGER,
    player_id    INTEGER,
    player_name  TEXT,
    keeper       INTEGER NOT NULL DEFAULT 0,
    bid_amount   INTEGER,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (season, overall_pick)
);

-- NFL player reference data, from the public team-roster endpoint. Rookie
-- status is a real field (experience.years == 0), not inferred from missing
-- stats -- that would misread anyone who sat out a season. Athlete ids share
-- the fantasy player id space, so this joins directly.
CREATE TABLE IF NOT EXISTS players (
    player_id        INTEGER PRIMARY KEY,
    name             TEXT,
    pro_team         TEXT,
    experience_years INTEGER,
    rookie           INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL
);

-- Top starters per team per week, for the matchup cards.
CREATE TABLE IF NOT EXISTS team_week_players (
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    team_id     INTEGER NOT NULL,
    player_id   INTEGER NOT NULL,
    name        TEXT,
    position    TEXT,
    lineup_slot TEXT,
    pro_team    TEXT,
    is_starter  INTEGER NOT NULL DEFAULT 1,
    injury_status TEXT,
    injured     INTEGER NOT NULL DEFAULT 0,
    projected   REAL,
    actual      REAL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (season, week, team_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_twp_season_week ON team_week_players (season, week);

-- Weekly player projections. Keyed on player_id, never name: ESPN's universe
-- contains distinct players who share a name (a linebacker named Justin
-- Jefferson, a cornerback named Lamar Jackson), and a name key merges them.
CREATE TABLE IF NOT EXISTS player_projections (
    season        INTEGER NOT NULL,
    week          INTEGER NOT NULL,
    player_id     INTEGER NOT NULL,
    name          TEXT,
    position      TEXT,
    pro_team      TEXT,
    projected     REAL,
    actual        REAL,
    percent_owned REAL,
    on_team_id    INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (season, week, player_id)
);

CREATE INDEX IF NOT EXISTS idx_proj_season_week ON player_projections (season, week);

-- Snapshot of league metadata (settings, draft date, season state) written by
-- the poller. Pages read this instead of calling ESPN, so a render can never be
-- slowed by ESPN's latency or broken by its outage.
CREATE TABLE IF NOT EXISTS league_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- One login per franchise. Usernames track the franchise's current name (people
-- know their team, not an account), so the unique key is a normalised copy --
-- names differ in case and spacing across ESPN seasons. Passwords are random,
-- generated once at seed time, and only ever stored as a PBKDF2 digest.
CREATE TABLE IF NOT EXISTS franchise_users (
    team_id       INTEGER PRIMARY KEY,
    username      TEXT NOT NULL,
    username_key  TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_login_at TEXT
);

-- League transactions: adds, waiver claims, drops, trades. Keyed on ESPN's own
-- message id so a re-poll of an overlapping window is idempotent -- the feed is
-- returned newest-first with no cursor, so overlap is the normal case.
CREATE TABLE IF NOT EXISTS transactions (
    message_id  INTEGER PRIMARY KEY,
    season      INTEGER NOT NULL,
    occurred_at TEXT,
    action      TEXT    NOT NULL,
    team_id     INTEGER,
    player_id   INTEGER,
    player_name TEXT,
    bid         INTEGER,
    updated_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transactions_season ON transactions (season, occurred_at DESC);

-- ESPN's public NFL news feed. Not season-scoped, unlike everything else in
-- this file -- the feed itself carries no season, and news is not season data.
-- Retention is by age (see prune_news) rather than by season boundary.
CREATE TABLE IF NOT EXISTS news_items (
    article_id  INTEGER PRIMARY KEY,
    source      TEXT    NOT NULL DEFAULT 'espn',
    published   TEXT,
    byline      TEXT,
    headline    TEXT    NOT NULL,
    description TEXT,
    url         TEXT,
    type        TEXT,
    premium     INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_news_published ON news_items (published DESC);

-- The roster join: athlete ids off each article's `categories` live in their
-- own table rather than a column, since one article can name several players.
CREATE TABLE IF NOT EXISTS news_athletes (
    article_id INTEGER NOT NULL,
    player_id  INTEGER NOT NULL,
    PRIMARY KEY (article_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_news_athletes_player ON news_athletes (player_id);

-- nflverse NFL data. A second source entirely: ESPN says what a player scored,
-- these say why -- snaps, targets, tackles. Written wholesale rather than
-- snapshot-and-revise like team_weeks, because nflverse restates its own files
-- as official stats settle and there is no revision history worth keeping.

-- The join that makes any of it usable. ESPN fantasy player ids are our key
-- everywhere; nflverse keys on gsis_id and Pro Football Reference on pfr_id.
CREATE TABLE IF NOT EXISTS nfl_player_ids (
    espn_id      INTEGER PRIMARY KEY,
    gsis_id      TEXT,
    pfr_id       TEXT,
    name         TEXT,
    position     TEXT,
    team         TEXT,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nfl_ids_gsis ON nfl_player_ids (gsis_id);

CREATE TABLE IF NOT EXISTS nfl_player_weeks (
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    gsis_id      TEXT    NOT NULL,
    name         TEXT,
    position     TEXT,
    team         TEXT,
    opponent     TEXT,
    season_type  TEXT,
    completions              REAL,
    attempts                 REAL,
    passing_yards            REAL,
    passing_tds              REAL,
    passing_interceptions    REAL,
    passing_air_yards        REAL,
    passing_first_downs      REAL,
    passing_epa              REAL,
    carries                  REAL,
    rushing_yards            REAL,
    rushing_tds              REAL,
    rushing_first_downs      REAL,
    rushing_epa              REAL,
    targets                  REAL,
    receptions               REAL,
    receiving_yards          REAL,
    receiving_tds            REAL,
    receiving_air_yards      REAL,
    receiving_first_downs    REAL,
    receiving_epa            REAL,
    target_share             REAL,
    air_yards_share          REAL,
    wopr                     REAL,
    racr                     REAL,
    def_tackles_solo         REAL,
    def_tackle_assists       REAL,
    def_tackles_for_loss     REAL,
    def_sacks                REAL,
    def_qb_hits              REAL,
    def_interceptions        REAL,
    def_pass_defended        REAL,
    def_fumbles_forced       REAL,
    def_tds                  REAL,
    fg_made                  REAL,
    fg_att                   REAL,
    fg_long                  REAL,
    pat_made                 REAL,
    pat_att                  REAL,
    fantasy_points           REAL,
    fantasy_points_ppr       REAL,
    offense_snaps            REAL,
    offense_pct              REAL,
    defense_snaps            REAL,
    defense_pct              REAL,
    st_snaps                 REAL,
    st_pct                   REAL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (season, week, gsis_id)
);

CREATE INDEX IF NOT EXISTS idx_nfl_weeks_gsis ON nfl_player_weeks (gsis_id);

-- Team-grain rows, for fantasy D/ST. Team defences carry no espn_id and no
-- gsis_id, so they can only ever join on the team abbreviation.
CREATE TABLE IF NOT EXISTS nfl_team_weeks (
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    team         TEXT    NOT NULL,
    opponent     TEXT,
    season_type  TEXT,
    completions              REAL,
    attempts                 REAL,
    passing_yards            REAL,
    passing_tds              REAL,
    passing_interceptions    REAL,
    carries                  REAL,
    rushing_yards            REAL,
    rushing_tds              REAL,
    receptions               REAL,
    targets                  REAL,
    receiving_yards          REAL,
    receiving_tds            REAL,
    def_tackles_solo         REAL,
    def_sacks                REAL,
    def_interceptions        REAL,
    def_pass_defended        REAL,
    def_fumbles_forced       REAL,
    def_tds                  REAL,
    special_teams_tds        REAL,
    fantasy_points           REAL,
    fantasy_points_ppr       REAL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (season, week, team)
);

-- What each nflverse release said when we last read it, so an unchanged
-- release costs one timestamp.json request instead of a download.
-- Touchdowns as they land. A row is written only when a rostered player's TD
-- count goes UP between two polls, which is what makes this a feed of events
-- rather than a standing total: the count itself lives on team_week_players.
-- Only rostered players are ever polled into that table, so "owned by someone
-- in the league" is structural here rather than a filter.
CREATE TABLE IF NOT EXISTS scoring_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    season    INTEGER NOT NULL,
    week      INTEGER NOT NULL,
    team_id   INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    name      TEXT,
    position  TEXT,
    pro_team  TEXT,
    tds       INTEGER NOT NULL,
    scored    INTEGER NOT NULL,
    at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_week ON scoring_events (season, week, at);

CREATE TABLE IF NOT EXISTS nflverse_sync (
    tag          TEXT PRIMARY KEY,
    last_updated TEXT,
    fetched_at   TEXT NOT NULL,
    rows         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS poll_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,            -- 'running' | 'ok' | 'error'
    kind         TEXT,                     -- 'live' | 'idle' | 'correction' | 'backfill'
    error        TEXT,
    rows_changed INTEGER NOT NULL DEFAULT 0
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL lets the web request path read while the poller writes. Its sidecar
    # -wal/-shm files live beside the db, which is why the whole data/ directory
    # is bind-mounted rather than the single db file.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS will not
# add them to an existing database, so they are applied explicitly.
_ADDED_COLUMNS = [
    ("player_projections", "actual", "REAL"),
    ("team_week_players", "pro_team", "TEXT"),
    # Bench players are stored now too, so every row has to say which it is.
    # Defaults to 1 so rows written before this column existed keep reading as
    # the starters they were.
    ("team_week_players", "is_starter", "INTEGER NOT NULL DEFAULT 1"),
    ("team_week_players", "injury_status", "TEXT"),
    ("team_week_players", "injured", "INTEGER NOT NULL DEFAULT 0"),
    # Touchdowns so far this week. The running total is what lets the next poll
    # tell a new score from one it has already announced.
    ("team_week_players", "tds", "INTEGER NOT NULL DEFAULT 0"),
]


def init_db(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)
        for table, column, coltype in _ADDED_COLUMNS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


@contextmanager
def session(db_path: Path | str):
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


# --- writes ---------------------------------------------------------------


def upsert_team(conn, season: int, team_id: int, name: str, owner: str | None, abbrev: str | None) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO teams (season, team_id, name, owner, abbrev)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(season, team_id) DO UPDATE SET
                name = excluded.name,
                owner = excluded.owner,
                abbrev = excluded.abbrev
            """,
            (season, team_id, name, owner, abbrev),
        )


def upsert_team_week(
    conn,
    season: int,
    week: int,
    team_id: int,
    points: float | None,
    projected: float | None,
    opponent_id: int | None,
    is_playoff: bool,
    result: str | None,
) -> bool:
    """Insert or revise one team-week. Returns True if anything actually changed.

    The WHERE clause on the upsert is what keeps `revision` honest: re-polling
    identical data touches nothing, so a bumped revision always means ESPN
    changed its mind (a stat correction).
    """
    with conn:
        cur = conn.execute(
            """
            INSERT INTO team_weeks
                (season, week, team_id, points, projected, opponent_id,
                 is_playoff, result, revision, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(season, week, team_id) DO UPDATE SET
                points      = excluded.points,
                projected   = excluded.projected,
                opponent_id = excluded.opponent_id,
                is_playoff  = excluded.is_playoff,
                result      = excluded.result,
                revision    = team_weeks.revision + 1,
                updated_at  = excluded.updated_at
            WHERE team_weeks.points      IS NOT excluded.points
               OR team_weeks.result      IS NOT excluded.result
               OR team_weeks.opponent_id IS NOT excluded.opponent_id
               OR team_weeks.projected   IS NOT excluded.projected
            """,
            (season, week, team_id, points, projected, opponent_id,
             1 if is_playoff else 0, result, _now()),
        )
        return cur.rowcount > 0


def upsert_team_season(
    conn, season: int, team_id: int, name: str | None,
    wins: int | None, losses: int | None, ties: int | None,
    points_for: float | None, points_against: float | None,
    final_rank: int | None, playoff_seed: int | None,
) -> bool:
    """Insert or revise one team-season. Returns True if anything changed.

    Same snapshot-and-revise contract as upsert_team_week: ESPN restates old
    seasons occasionally, and a bumped revision is the record that it did.
    """
    with conn:
        cur = conn.execute(
            """
            INSERT INTO team_seasons
                (season, team_id, name, wins, losses, ties, points_for,
                 points_against, final_rank, playoff_seed, revision, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(season, team_id) DO UPDATE SET
                name           = excluded.name,
                wins           = excluded.wins,
                losses         = excluded.losses,
                ties           = excluded.ties,
                points_for     = excluded.points_for,
                points_against = excluded.points_against,
                final_rank     = excluded.final_rank,
                playoff_seed   = excluded.playoff_seed,
                revision       = team_seasons.revision + 1,
                updated_at     = excluded.updated_at
            WHERE team_seasons.wins           IS NOT excluded.wins
               OR team_seasons.losses         IS NOT excluded.losses
               OR team_seasons.ties           IS NOT excluded.ties
               OR team_seasons.points_for     IS NOT excluded.points_for
               OR team_seasons.points_against IS NOT excluded.points_against
               OR team_seasons.final_rank     IS NOT excluded.final_rank
               OR team_seasons.name           IS NOT excluded.name
            """,
            (season, team_id, name, wins, losses, ties, points_for,
             points_against, final_rank, playoff_seed, _now()),
        )
        return cur.rowcount > 0


def replace_draft_picks(conn, season: int, picks: list[dict]) -> int:
    """Store a completed draft. Returns the number of picks written."""
    with conn:
        conn.execute("DELETE FROM draft_picks WHERE season=?", (season,))
        conn.executemany(
            """
            INSERT INTO draft_picks
                (season, overall_pick, round, round_pick, team_id,
                 player_id, player_name, keeper, bid_amount, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(season, p["overall_pick"], p.get("round"), p.get("round_pick"),
              p.get("team_id"), p.get("player_id"), p.get("player_name"),
              1 if p.get("keeper") else 0, p.get("bid_amount"), _now())
             for p in picks],
        )
    return len(picks)


def set_meta(conn, key: str, value) -> None:
    with conn:
        conn.execute(
            "INSERT INTO league_meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), _now()),
        )


def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM league_meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return default


def replace_player_projections(conn, season: int, week: int, rows: list[dict]) -> int:
    """Replace one week's projections wholesale.

    Projections are a forecast, not a record: ESPN revises them continuously and
    there is no history worth keeping, so this replaces rather than revising in
    place the way scores do.
    """
    with conn:
        conn.execute("DELETE FROM player_projections WHERE season=? AND week=?", (season, week))
        conn.executemany(
            """
            INSERT INTO player_projections
                (season, week, player_id, name, position, pro_team,
                 projected, actual, percent_owned, on_team_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(season, week, r["player_id"], r.get("name"), r.get("position"),
              r.get("pro_team"), r.get("projected"), r.get("actual"),
              r.get("percent_owned"), r.get("on_team_id") or 0, _now()) for r in rows],
        )
    return len(rows)


def fetch_player_projections(conn, season: int, week: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM player_projections WHERE season=? AND week=? ORDER BY projected DESC",
        (season, week),
    )]


def projections_synced_at(conn, season: int, week: int) -> str | None:
    row = conn.execute(
        "SELECT MAX(updated_at) FROM player_projections WHERE season=? AND week=?",
        (season, week),
    ).fetchone()
    return row[0] if row else None


def week_actuals(conn, season: int, week: int) -> dict[int, float]:
    """{player_id: actual points} for one week -- used to show last week's
    result beside this week's projection."""
    if week < 1:
        return {}
    return {
        int(r["player_id"]): r["actual"]
        for r in conn.execute(
            "SELECT player_id, actual FROM player_projections "
            "WHERE season=? AND week=? AND actual IS NOT NULL",
            (season, week),
        )
    }


def projection_weeks(conn, season: int) -> list[int]:
    return [int(r[0]) for r in conn.execute(
        "SELECT DISTINCT week FROM player_projections WHERE season=? ORDER BY week", (season,)
    )]


def replace_players(conn, rows: dict[int, dict]) -> int:
    """Upsert NFL player reference data. Returns rows written."""
    with conn:
        conn.executemany(
            """
            INSERT INTO players (player_id, name, pro_team, experience_years, rookie, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_id) DO UPDATE SET
                name = excluded.name, pro_team = excluded.pro_team,
                experience_years = excluded.experience_years,
                rookie = excluded.rookie, updated_at = excluded.updated_at
            """,
            [(pid, v.get("name"), v.get("pro_team"), v.get("years"),
              1 if v.get("years") == 0 else 0, _now())
             for pid, v in rows.items()],
        )
    return len(rows)


def rookie_ids(conn) -> set[int]:
    return {int(r[0]) for r in conn.execute("SELECT player_id FROM players WHERE rookie=1")}


def players_synced_at(conn) -> str | None:
    row = conn.execute("SELECT MAX(updated_at) FROM players").fetchone()
    return row[0] if row else None


def replace_team_week_players(conn, season: int, week: int, rows: list[dict]) -> int:
    """Replace one week's lineups. A lineup is current state, not a record.

    The touchdown diff happens here rather than in the poller because this is
    the only moment both states exist: the write is a DELETE followed by an
    INSERT, so once it has run the previous counts are gone. Doing it inside
    the same transaction also means a crash between the two cannot announce a
    touchdown that was never stored.
    """
    with conn:
        before = {
            int(r["player_id"]): int(r["tds"] or 0)
            for r in conn.execute(
                "SELECT player_id, tds FROM team_week_players WHERE season=? AND week=?",
                (season, week))
        }
        now = _now()
        events = []
        for r in rows:
            pid = int(r["player_id"])
            tds = int(r.get("tds") or 0)
            was = before.get(pid)
            # A player with no previous row is new to the roster mid-week, not
            # someone who just scored: announcing their season so far as it
            # happening now would be wrong, so they only seed the baseline.
            if was is None or tds <= was:
                continue
            events.append((season, week, r["team_id"], pid, r.get("name"),
                           r.get("position"), r.get("pro_team"), tds, tds - was, now))
        if events:
            conn.executemany(
                """INSERT INTO scoring_events
                   (season, week, team_id, player_id, name, position, pro_team,
                    tds, scored, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", events)

        conn.execute("DELETE FROM team_week_players WHERE season=? AND week=?", (season, week))
        conn.executemany(
            """
            INSERT INTO team_week_players
                (season, week, team_id, player_id, name, position,
                 lineup_slot, pro_team, is_starter, injury_status, injured,
                 projected, actual, tds, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(season, week, r["team_id"], r["player_id"], r.get("name"), r.get("position"),
              r.get("lineup_slot"), r.get("pro_team"),
              0 if r.get("is_starter") is False else 1,
              r.get("injury_status"), 1 if r.get("injured") else 0,
              r.get("projected"), r.get("actual"), int(r.get("tds") or 0), now)
             for r in rows],
        )
    return len(rows)


def recent_scoring_events(conn, season: int, week: int,
                          within_minutes: int = 150, limit: int = 8) -> list[dict]:
    """Touchdowns scored in the last few minutes, newest first.

    Time-boxed because this is a live strip: a touchdown from Sunday is not
    news on Wednesday. Every row is a rostered player by construction, and a
    row only exists because the count moved while we were polling, so "owned
    and playing" needs no extra test.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=within_minutes)).isoformat(timespec="seconds")
    return [dict(r) for r in conn.execute(
        """SELECT * FROM scoring_events
           WHERE season=? AND week=? AND at >= ?
           ORDER BY at DESC, id DESC LIMIT ?""",
        (season, week, cutoff, limit))]


def fetch_team_week_players(conn, season: int, week: int,
                            starters_only: bool = True) -> dict[int, list[dict]]:
    """Each team's stored lineup for a week, best first, with the rookie flag
    joined on. pro_team comes from the matchup payload, falling back to the daily
    roster sync for rows written before that column existed."""
    rows = conn.execute(
        """
        SELECT twp.*, COALESCE(p.rookie, 0) AS rookie,
               COALESCE(twp.pro_team, p.pro_team) AS pro_team
        FROM team_week_players twp
        LEFT JOIN players p ON p.player_id = twp.player_id
        WHERE twp.season=? AND twp.week=? AND (twp.is_starter=1 OR ?=0)
        ORDER BY COALESCE(twp.actual, twp.projected) DESC
        """,
        (season, week, 1 if starters_only else 0),
    )
    out: dict[int, list[dict]] = {}
    for row in rows:
        out.setdefault(int(row["team_id"]), []).append(dict(row))
    return out


def start_poll_run(conn, kind: str) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO poll_runs (started_at, status, kind) VALUES (?, 'running', ?)",
            (_now(), kind),
        )
        return int(cur.lastrowid)


def finish_poll_run(conn, run_id: int, status: str, rows_changed: int = 0, error: str | None = None) -> None:
    with conn:
        conn.execute(
            "UPDATE poll_runs SET finished_at=?, status=?, rows_changed=?, error=? WHERE id=?",
            (_now(), status, rows_changed, error, run_id),
        )


# --- reads ----------------------------------------------------------------


def fetch_team_weeks(conn, seasons: list[int] | None = None) -> list[dict]:
    sql = "SELECT * FROM team_weeks"
    params: list = []
    if seasons:
        sql += f" WHERE season IN ({','.join('?' * len(seasons))})"
        params = list(seasons)
    sql += " ORDER BY season, week, team_id"
    return [dict(r) for r in conn.execute(sql, params)]


def fetch_teams(conn, season: int | None = None) -> list[dict]:
    if season is None:
        return [dict(r) for r in conn.execute("SELECT * FROM teams ORDER BY season DESC, team_id")]
    return [dict(r) for r in conn.execute("SELECT * FROM teams WHERE season=? ORDER BY team_id", (season,))]


def team_name_map(conn) -> dict[int, str]:
    """Most recent known name for each team_id, for labelling all-time views."""
    rows = conn.execute("SELECT team_id, name FROM teams ORDER BY season ASC")
    return {int(r["team_id"]): r["name"] for r in rows}


def latest_poll(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM poll_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def last_successful_poll(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM poll_runs WHERE status='ok' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def recent_corrections(conn, limit: int = 20) -> list[dict]:
    """Team-weeks ESPN has revised since first capture -- the stat corrections."""
    rows = conn.execute(
        "SELECT * FROM team_weeks WHERE revision > 1 ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


def fetch_team_seasons(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM team_seasons ORDER BY season DESC, final_rank, team_id"
    )]


def fetch_draft_picks(conn, season: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM draft_picks WHERE season=? ORDER BY overall_pick", (season,)
    )]


def franchise_names(conn, since: dict[int, int] | None = None) -> dict[int, dict]:
    """Current display name per franchise, plus any former names.

    Franchise ids are stable across seasons but names are not -- four of the
    eight teams here have renamed at least once -- so all-time aggregation keys
    on the id and labels with the newest name.
    """
    rows = conn.execute(
        "SELECT season, team_id, name FROM team_seasons WHERE name IS NOT NULL "
        "UNION ALL SELECT season, team_id, name FROM teams WHERE name IS NOT NULL "
        "ORDER BY season ASC"
    )
    since = since or {}
    seen: dict[int, list[str]] = {}
    for row in rows:
        team_id = int(row["team_id"])
        # A name used only before the franchise changed hands is not a former
        # name of the current franchise -- it belonged to someone else.
        if int(row["season"]) < since.get(team_id, 0):
            continue
        seen.setdefault(team_id, []).append(row["name"])
    out: dict[int, dict] = {}
    for team_id, names in seen.items():
        current = names[-1]
        former = [n for n in dict.fromkeys(names) if n != current]
        out[team_id] = {"name": current, "former": former}
    return out


def has_week_data(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM team_weeks WHERE points IS NOT NULL AND points > 0 LIMIT 1"
    ).fetchone()
    return row is not None


def history_seasons(conn) -> list[int]:
    return [int(r[0]) for r in conn.execute(
        "SELECT DISTINCT season FROM team_seasons ORDER BY season DESC"
    )]


def available_seasons(conn) -> list[int]:
    return [int(r[0]) for r in conn.execute(
        "SELECT DISTINCT season FROM team_weeks ORDER BY season DESC"
    )]


# --- franchise logins -----------------------------------------------------


def fetch_franchise_users(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM franchise_users ORDER BY username_key"
    )]


def get_franchise_user(conn, team_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM franchise_users WHERE team_id=?", (team_id,)
    ).fetchone()
    return dict(row) if row else None


def find_franchise_user(conn, username_key: str) -> dict | None:
    """Look up by the normalised name, so 'Juhu' and ' juhu ' are one account."""
    row = conn.execute(
        "SELECT * FROM franchise_users WHERE username_key=?", (username_key,)
    ).fetchone()
    return dict(row) if row else None


def create_franchise_user(conn, team_id: int, username: str,
                          username_key: str, password_hash: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO franchise_users "
            "(team_id, username, username_key, password_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (team_id, username, username_key, password_hash, _now(), _now()),
        )


def set_franchise_username(conn, team_id: int, username: str, username_key: str) -> None:
    with conn:
        conn.execute(
            "UPDATE franchise_users SET username=?, username_key=?, updated_at=? WHERE team_id=?",
            (username, username_key, _now(), team_id),
        )


def set_franchise_password(conn, team_id: int, password_hash: str) -> None:
    """Resetting a password also invalidates that franchise's live sessions --
    the session signature covers a fingerprint of the stored digest."""
    with conn:
        conn.execute(
            "UPDATE franchise_users SET password_hash=?, updated_at=? WHERE team_id=?",
            (password_hash, _now(), team_id),
        )


def touch_franchise_login(conn, team_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE franchise_users SET last_login_at=? WHERE team_id=?", (_now(), team_id)
        )


# --- nflverse ---------------------------------------------------------------
#
# Column lists are derived from the tuples in app/nflverse.py rather than spelled
# out again here, so adding a stat is one edit instead of three that must agree.
# tests/test_nflverse.py asserts the table and the tuples still match.

_NFL_PLAYER_WEEK_COLUMNS = (
    ("season", "week", "gsis_id", "name", "position", "team", "opponent", "season_type")
    + _nflverse.PLAYER_WEEK_STATS + _nflverse.SNAP_STATS
)
_NFL_TEAM_WEEK_COLUMNS = (
    ("season", "week", "team", "opponent", "season_type") + _nflverse.TEAM_WEEK_STATS
)


def replace_nfl_player_ids(conn, rows: dict[int, dict]) -> int:
    """Upsert the ESPN -> gsis/pfr id map.

    Upsert rather than replace: nflverse occasionally drops a player from the
    file, and losing the mapping would orphan every stat row we already hold.
    """
    with conn:
        conn.executemany(
            """
            INSERT INTO nfl_player_ids
                (espn_id, gsis_id, pfr_id, name, position, team, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(espn_id) DO UPDATE SET
                gsis_id = excluded.gsis_id, pfr_id = excluded.pfr_id,
                name = excluded.name, position = excluded.position,
                team = excluded.team, updated_at = excluded.updated_at
            """,
            [(espn_id, v.get("gsis_id"), v.get("pfr_id"), v.get("name"),
              v.get("position"), v.get("team"), _now()) for espn_id, v in rows.items()],
        )
    return len(rows)


def _replace_rows(conn, table: str, columns: tuple, season: int, rows: list[dict]) -> int:
    placeholders = ", ".join("?" * (len(columns) + 1))
    with conn:
        conn.execute(f"DELETE FROM {table} WHERE season=?", (season,))
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}, updated_at) VALUES ({placeholders})",
            [tuple(row.get(c) for c in columns) + (_now(),) for row in rows],
        )
    return len(rows)


def replace_nfl_player_weeks(conn, season: int, rows: list[dict]) -> int:
    return _replace_rows(conn, "nfl_player_weeks", _NFL_PLAYER_WEEK_COLUMNS, season, rows)


def replace_nfl_team_weeks(conn, season: int, rows: list[dict]) -> int:
    return _replace_rows(conn, "nfl_team_weeks", _NFL_TEAM_WEEK_COLUMNS, season, rows)


def set_nflverse_sync(conn, tag: str, last_updated: str | None, rows: int) -> None:
    with conn:
        conn.execute(
            "INSERT INTO nflverse_sync (tag, last_updated, fetched_at, rows) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(tag) DO UPDATE SET "
            "last_updated=excluded.last_updated, fetched_at=excluded.fetched_at, "
            "rows=excluded.rows",
            (tag, last_updated, _now(), rows),
        )


def get_nflverse_sync(conn, tag: str) -> dict | None:
    row = conn.execute("SELECT * FROM nflverse_sync WHERE tag=?", (tag,)).fetchone()
    return dict(row) if row else None


def nfl_id_map(conn) -> dict[int, dict]:
    """{espn_player_id: {gsis_id, pfr_id, ...}} -- the roster join, from storage."""
    return {int(r["espn_id"]): dict(r) for r in conn.execute("SELECT * FROM nfl_player_ids")}


def fetch_nfl_player_weeks(conn, season: int, gsis_ids: list[str] | None = None) -> list[dict]:
    """Weekly NFL stat lines, optionally narrowed to one set of players.

    Regular season only: a January playoff line would otherwise be averaged into
    a player's usage rate alongside seventeen regular-season weeks.
    """
    sql = "SELECT * FROM nfl_player_weeks WHERE season=? AND season_type='REG'"
    params: list = [season]
    if gsis_ids is not None:
        if not gsis_ids:
            return []
        sql += f" AND gsis_id IN ({','.join('?' * len(gsis_ids))})"
        params += list(gsis_ids)
    return [dict(r) for r in conn.execute(sql + " ORDER BY week, gsis_id", params)]


def fetch_nfl_team_weeks(conn, season: int, teams: list[str] | None = None) -> list[dict]:
    sql = "SELECT * FROM nfl_team_weeks WHERE season=? AND season_type='REG'"
    params: list = [season]
    if teams is not None:
        if not teams:
            return []
        sql += f" AND team IN ({','.join('?' * len(teams))})"
        params += list(teams)
    return [dict(r) for r in conn.execute(sql + " ORDER BY week, team", params)]


def nfl_seasons(conn) -> list[int]:
    return [int(r[0]) for r in conn.execute(
        "SELECT DISTINCT season FROM nfl_player_weeks ORDER BY season DESC"
    )]


# --- transactions -----------------------------------------------------------


def upsert_transactions(conn, season: int, rows: list[dict],
                        names: dict[int, str] | None = None) -> int:
    """Store transactions, ignoring ones already seen.

    ESPN's feed has no cursor -- you ask for the newest N -- so every poll
    re-reads rows already stored. DO NOTHING on conflict makes that free, and
    the return value counts only what was genuinely new.
    """
    names = names or {}
    written = 0
    with conn:
        for row in rows:
            if row.get("message_id") is None:
                continue
            occurred = None
            if row.get("date_ms"):
                occurred = datetime.fromtimestamp(
                    row["date_ms"] / 1000, timezone.utc).isoformat(timespec="seconds")
            cur = conn.execute(
                """
                INSERT INTO transactions
                    (message_id, season, occurred_at, action, team_id,
                     player_id, player_name, bid, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO NOTHING
                """,
                (row["message_id"], season, occurred, row.get("action"),
                 row.get("team_id"), row.get("player_id"),
                 names.get(row.get("player_id")), row.get("bid"), _now()),
            )
            written += cur.rowcount
    return written


def fetch_transactions(conn, season: int, limit: int = 200) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE season=? ORDER BY occurred_at DESC, message_id DESC "
        "LIMIT ?", (season, limit))]


def player_name_map(conn) -> dict[int, str]:
    """{player_id: name} across every player we have ever seen.

    Transactions arrive as bare ids; ESPN would name them one extra request at a
    time, so they are named from what we already store instead.
    """
    out: dict[int, str] = {}
    for sql in ("SELECT player_id, name FROM players WHERE name IS NOT NULL",
                "SELECT player_id, name FROM player_projections WHERE name IS NOT NULL",
                "SELECT player_id, name FROM team_week_players WHERE name IS NOT NULL"):
        for row in conn.execute(sql):
            out.setdefault(int(row["player_id"]), row["name"])
    return out


# --- news -------------------------------------------------------------------


def upsert_news(conn, rows: list[dict]) -> int:
    """Store the feed. Returns only genuinely new article ids.

    ESPN *does* retitle articles after publishing, so unlike upsert_transactions
    this updates headline/description/byline/premium on conflict rather than
    doing nothing -- the return value still counts only what was genuinely new,
    so `_run`'s "N rows changed" log stays honest about how much moved.
    """
    if not rows:
        return 0
    ids = [row["article_id"] for row in rows]
    existing = {
        int(r[0]) for r in conn.execute(
            f"SELECT article_id FROM news_items WHERE article_id IN ({','.join('?' * len(ids))})",
            ids,
        )
    }
    with conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO news_items
                    (article_id, source, published, byline, headline,
                     description, url, type, premium, updated_at)
                VALUES (?, 'espn', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_id) DO UPDATE SET
                    headline    = excluded.headline,
                    description = excluded.description,
                    byline      = excluded.byline,
                    premium     = excluded.premium,
                    updated_at  = excluded.updated_at
                """,
                (row["article_id"], row.get("published"), row.get("byline"),
                 row.get("headline") or "", row.get("description"), row.get("url"),
                 row.get("type"), 1 if row.get("premium") else 0, _now()),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO news_athletes (article_id, player_id) VALUES (?, ?)",
                [(row["article_id"], player_id) for player_id in row.get("athlete_ids") or []],
            )
    return len(set(ids) - existing)


def prune_news(conn, keep_days: int) -> int:
    """Drop items older than keep_days, and their news_athletes rows with them.

    Age is read off `published`, not `updated_at` -- a retitle must not reset an
    article's clock and keep it around forever. Day precision is enough for a
    21-day default and sidesteps any mismatch between ESPN's timestamp format
    and ours.

    An article with no `published` at all falls back to `updated_at`. Skipping
    those instead would leak: they sort last under `published DESC` so the page
    never shows them, and nothing else would ever delete them.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).date().isoformat()
    with conn:
        conn.execute(
            "DELETE FROM news_athletes WHERE article_id IN "
            "(SELECT article_id FROM news_items WHERE COALESCE(published, updated_at) < ?)",
            (cutoff,),
        )
        cur = conn.execute(
            "DELETE FROM news_items WHERE COALESCE(published, updated_at) < ?",
            (cutoff,),
        )
    return cur.rowcount


def fetch_news(conn, limit: int = 200) -> list[dict]:
    """Newest first, with athlete_ids aggregated per article."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM news_items ORDER BY published DESC, article_id DESC LIMIT ?",
        (limit,),
    )]
    ids = [row["article_id"] for row in rows]
    athletes: dict[int, list[int]] = {}
    if ids:
        placeholders = ",".join("?" * len(ids))
        for r in conn.execute(
            f"SELECT article_id, player_id FROM news_athletes WHERE article_id IN ({placeholders})",
            ids,
        ):
            athletes.setdefault(int(r["article_id"]), []).append(int(r["player_id"]))
    for row in rows:
        row["athlete_ids"] = athletes.get(row["article_id"], [])
        row["premium"] = bool(row["premium"])
    return rows

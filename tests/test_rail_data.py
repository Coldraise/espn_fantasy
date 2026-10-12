"""Data for the home rail and the players-page filters: standings on the
dashboard context, and injury/lineup/experience/game keys on player rows."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import db, players


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_db(c)
    yield c
    c.close()


def _twp(conn, week, team, pid, injury=None, slot="RB", starter=1, season=2026):
    with conn:
        conn.execute(
            """INSERT INTO team_week_players (season, week, team_id, player_id, name,
               lineup_slot, is_starter, injury_status, updated_at)
               VALUES (?, ?, ?, ?, 'P', ?, ?, ?, 'now')""",
            (season, week, team, pid, slot, starter, injury))


def _game(conn, week, home, away, season=2026):
    with conn:
        conn.execute(
            """INSERT INTO nfl_games (season, week, game_id, home_team, away_team,
               kickoff_utc, state, period, clock, updated_at)
               VALUES (?, ?, ?, ?, ?, '2026-10-18T17:00:00Z', 'pre', NULL, NULL, 'now')""",
            (season, week, week * 100 + len(home), home, away))


def test_latest_player_row_picks_highest_week(conn):
    _twp(conn, 3, 1, 10, injury="QUESTIONABLE", slot="BE", starter=0)
    _twp(conn, 5, 2, 10, injury="OUT", slot="RB", starter=1)
    _twp(conn, 4, 1, 11, injury=None)
    latest = db.fetch_latest_player_rows(conn, 2026)
    assert latest[10]["injury_status"] == "OUT"
    assert latest[10]["is_starter"] == 1
    assert latest[11]["injury_status"] is None
    assert 12 not in latest


def test_latest_player_row_ignores_other_seasons(conn):
    _twp(conn, 9, 1, 10, injury="OUT", season=2025)
    assert db.fetch_latest_player_rows(conn, 2026) == {}


def test_experience_years_skips_unknown(conn):
    db.replace_players(conn, {1: {"name": "A", "pro_team": "DET", "years": 0},
                              2: {"name": "B", "pro_team": "DET", "years": None},
                              3: {"name": "C", "pro_team": "DET", "years": 7}})
    assert db.fetch_experience_years(conn) == {1: 0, 3: 7}


def test_attach_row_context_fills_every_key(conn):
    _twp(conn, 5, 2, 10, injury="OUT", slot="WR", starter=0)
    _game(conn, 6, "KC", "DET")
    db.replace_players(conn, {10: {"name": "A", "pro_team": "DET", "years": 3}})
    rows = [{"player_id": 10, "pro_team": "DET", "on_team_id": 2},
            {"player_id": 99, "pro_team": "FA", "on_team_id": 0}]
    players.attach_row_context(
        rows, db.fetch_latest_team_player_rows(conn, 2026), db.fetch_experience_years(conn),
        players.game_by_team(db.fetch_nfl_games(conn, 2026, 6)))
    a, b = rows
    assert (a["injury_status"], a["lineup_slot"], a["is_starter"], a["experience_years"]) \
        == ("OUT", "WR", 0, 3)
    assert a["game"] == {"opp": "KC", "home": False, "kickoff_utc": "2026-10-18T17:00:00Z",
                         "state": "pre", "period": None, "clock": None}
    assert b == {"player_id": 99, "pro_team": "FA", "on_team_id": 0, "injury_status": None, "lineup_slot": None,
                 "is_starter": None, "experience_years": None, "game": None}


def test_bye_week_game_is_none():
    rows = [{"player_id": 1, "pro_team": "DET"}, {"player_id": 2, "pro_team": None}]
    games = players.game_by_team([{"home_team": "KC", "away_team": "BUF"}])
    players.attach_row_context(rows, {}, {}, games)
    assert [r["game"] for r in rows] == [None, None]
    assert games["KC"]["home"] is True and games["BUF"]["opp"] == "KC"


def test_game_by_team_skips_incomplete_rows():
    assert players.game_by_team([{"home_team": "KC", "away_team": None}]) == {}


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setenv("CONFIG_PATH", "config/config.yml")
    from fastapi.testclient import TestClient
    from app import auth, config, main
    config.get_config.cache_clear() if hasattr(config.get_config, "cache_clear") else None
    with db.session(tmp_path / "r.db") as c:
        db.init_db(c)
        db.set_meta(c, "status", {"state": "in_season"})
        auth.seed_accounts(c, {1: "Team A"})
        token = auth.make_token(c, db.get_franchise_user(c, 1))
    from app import config as _config
    _config.get_config(reload=True)
    client = TestClient(main.app)
    client.cookies.set(auth.SESSION_COOKIE, token)
    return client, main


def test_routes_render_and_expose_new_context(tmp_path, monkeypatch):
    client, main = _client(tmp_path, monkeypatch)
    seen = {}
    real = main.templates.TemplateResponse

    def spy(name, context, *a, **k):
        seen[name] = context
        return real(name, context, *a, **k)

    monkeypatch.setattr(main.templates, "TemplateResponse", spy)
    for path in ("/", "/players"):
        assert client.get(path).status_code == 200
    assert seen["dashboard.html"]["standings"] == []
    assert seen["players.html"]["rows"] == []


# -- M1: stale roster data ------------------------------------------------

def _attach(conn, row):
    players.attach_row_context([row], db.fetch_latest_team_player_rows(conn, 2026), {}, {})
    return row


def test_dropped_player_gets_no_stale_roster_data(conn):
    _twp(conn, 3, 1, 10, injury="OUT", slot="RB", starter=1)
    row = _attach(conn, {"player_id": 10, "pro_team": "DET", "on_team_id": 0})
    assert (row["injury_status"], row["lineup_slot"], row["is_starter"]) == (None, None, None)


def test_traded_player_ignores_old_teams_row(conn):
    _twp(conn, 3, 1, 10, injury="OUT", slot="RB", starter=1)   # old team
    row = _attach(conn, {"player_id": 10, "pro_team": "DET", "on_team_id": 2})
    assert (row["injury_status"], row["lineup_slot"], row["is_starter"]) == (None, None, None)


def test_same_player_on_two_teams_same_week_prefers_current_team(conn):
    _twp(conn, 5, 1, 10, injury="OUT", slot="RB", starter=1)
    _twp(conn, 5, 2, 10, injury=None, slot="BE", starter=0)
    for team, want in ((1, ("OUT", "RB", 1)), (2, (None, "BE", 0))):
        row = _attach(conn, {"player_id": 10, "pro_team": "DET", "on_team_id": team})
        assert (row["injury_status"], row["lineup_slot"], row["is_starter"]) == want


def test_starter_and_injury_filters_do_not_match_free_agents(conn):
    _twp(conn, 3, 1, 10, injury="OUT", slot="RB", starter=1)
    rows = [{"player_id": 10, "pro_team": "DET", "on_team_id": 0}]
    players.attach_row_context(rows, db.fetch_latest_team_player_rows(conn, 2026), {}, {})
    assert players.apply_filters(rows, {"starter": 1}) == []
    assert players.apply_filters(rows, {"inj": ["any"]}) == []


# -- M2/M3: seeded data through the real routes -----------------------------

def _seed(path):
    with db.session(path) as c:
        db.init_db(c)
        db.set_meta(c, "status", {"state": "in_season", "current_matchup_period": 1})
        for tid, name in ((1, "Alpha"), (2, "Beta")):
            c.execute("INSERT INTO teams (season, team_id, name, owner, abbrev) "
                      "VALUES (2026, ?, ?, 'o', 'x')", (tid, name))
        db.upsert_team_week(c, 2026, 1, 1, 120.5, 100.0, 2, False, "W")
        db.upsert_team_week(c, 2026, 1, 2, 90.0, 100.0, 1, False, "L")
        _twp(c, 1, 1, 10, injury="OUT", slot="RB", starter=1)     # rostered, injured starter
        _twp(c, 1, 2, 11, injury="QUESTIONABLE", slot="WR", starter=1)  # dropped later
        _twp(c, 1, 1, 12, injury=None, slot="WR", starter=1)      # on a bye
        with c:
            for gid, home, away, kick in ((1, "KC", "BUF", None),
                                          (2, "DET", "GB", "2026-09-10T17:00:00Z")):
                c.execute("INSERT INTO nfl_games (season, week, game_id, home_team, away_team, "
                          "kickoff_utc, state, updated_at) VALUES (2026, 1, ?, ?, ?, ?, 'pre', 'n')",
                          (gid, home, away, kick))
        db.replace_player_projections(c, 2026, 2, [
            {"player_id": 10, "name": "Hurt Back", "position": "RB", "pro_team": "KC",
             "projected": 11.0, "on_team_id": 1, "percent_owned": 80},
            {"player_id": 11, "name": "Dropped Wr", "position": "WR", "pro_team": "DET",
             "projected": 7.0, "on_team_id": 0, "percent_owned": 20},
            {"player_id": 12, "name": "Bye Guy", "position": "WR", "pro_team": "MIA",
             "projected": 9.0, "on_team_id": 1, "percent_owned": 50},
        ])
        db.replace_player_projections(c, 2026, 1, [
            {"player_id": 10, "name": "Hurt Back", "position": "RB", "pro_team": "KC",
             "projected": 11.0, "actual": 3.0, "on_team_id": 1},
        ])
        with c:   # week 1 played: lets week 2 be "next"
            c.execute("UPDATE nfl_games SET state='post' WHERE week=1")
        db.set_meta(c, "status", {"state": "in_season", "current_matchup_period": 2})
        with c:
            c.execute("INSERT INTO nfl_games (season, week, game_id, home_team, away_team, "
                      "kickoff_utc, state, updated_at) VALUES (2026, 2, 5, 'KC', 'BUF', "
                      "'2026-09-17T17:00:00Z', 'pre', 'n')")
            c.execute("INSERT INTO nfl_games (season, week, game_id, home_team, away_team, "
                      "kickoff_utc, state, updated_at) VALUES (2026, 2, 6, 'DET', 'GB', "
                      "NULL, 'pre', 'n')")


def _data_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import auth, config, main
    path = tmp_path / "seed.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("CONFIG_PATH", "config/config.yml")
    _seed(path)
    with db.session(path) as c:
        auth.seed_accounts(c, {1: "Alpha"})
        token = auth.make_token(c, db.get_franchise_user(c, 1))
    config.get_config(reload=True)
    client = TestClient(main.app)
    client.cookies.set(auth.SESSION_COOKIE, token)
    seen = {}
    real = main.templates.TemplateResponse

    def spy(name, context, *a, **k):
        seen[name] = context
        return real(name, context, *a, **k)

    monkeypatch.setattr(main.templates, "TemplateResponse", spy)
    return client, seen


def test_routes_with_data(tmp_path, monkeypatch):
    client, seen = _data_client(tmp_path, monkeypatch)
    for path in ("/", "/players", "/standings"):
        assert client.get(path).status_code == 200, path

    st = seen["dashboard.html"]["standings"]
    assert [r["team_id"] for r in st] == [1, 2]
    assert (st[0]["wins"], st[0]["losses"], st[0]["pf"], st[0]["pa"]) == (1, 0, 120.5, 90.0)
    assert (st[1]["wins"], st[1]["losses"], st[1]["pf"]) == (0, 1, 90.0)
    assert {"team_id", "name", "wins", "losses", "ties", "pf", "pa", "avg", "streak"} <= set(st[0])
    assert st[0]["name"] == "Alpha"
    assert seen["dashboard.html"]["short_teams"] == {"Alpha": "Alpha", "Beta": "Beta"}
    assert seen["players.html"]["short_teams"] == {"Alpha": "Alpha", "Beta": "Beta"}

    # rail: deduplicated, null kickoff last, rendered without raising
    rail = seen["dashboard.html"]["rail_games"]
    assert [g["game_id"] for g in rail] == [5, 6]

    rows = {r["player_id"]: r for r in seen["players.html"]["rows"]}
    assert (rows[10]["injury_status"], rows[10]["lineup_slot"], rows[10]["is_starter"]) \
        == ("OUT", "RB", 1)
    assert rows[10]["game"]["opp"] == "BUF" and rows[10]["game"]["home"] is True
    assert (rows[11]["injury_status"], rows[11]["lineup_slot"], rows[11]["is_starter"]) \
        == (None, None, None)
    assert rows[12]["game"] is None            # MIA is on a bye
    assert rows[11]["game"]["kickoff_utc"] is None


def test_dashboard_renders_tbd_for_missing_kickoff(tmp_path, monkeypatch):
    client, _ = _data_client(tmp_path, monkeypatch)
    html = client.get("/").text
    assert "TBD" in html and "DET" in html

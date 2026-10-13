import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import db, main


def _g(h, a=None):
    return {"home": {"id": h}, "away": {"id": a} if a else None, "bye": a is None}


GAMES = lambda: [_g(1, 2), _g(3, 7), _g(5)]


def test_absent_selects_mine_first():
    r = main._select_games(GAMES(), 3, None)
    assert [g["gid"] for g in r["games"]] == ["3-7", "1-2", "5-bye"]
    assert r["selected_gid"] == "3-7" and r["games"][0]["mine"] is True
    assert not any(g["mine"] for g in r["games"][1:])
    assert r["my_game"]["gid"] == "3-7" and len(r["other_games"]) == 2


def test_other_and_bye_selected():
    assert main._select_games(GAMES(), 3, "1-2")["selected_gid"] == "1-2"
    assert main._select_games(GAMES(), 3, "5-bye")["selected_gid"] == "5-bye"


@pytest.mark.parametrize("m", ["", "garbage", "9-9", "1-2-3", "--", None])
def test_bad_m_falls_back(m):
    assert main._select_games(GAMES(), 3, m)["selected_gid"] == "3-7"


def test_not_in_game_uses_first_and_empty_week():
    r = main._select_games(GAMES(), None, "zzz")
    assert r["selected_gid"] == "1-2" and r["my_game"] is None
    r = main._select_games([], 3, "1-2")
    assert r["selected_game"] is None and r["selected_gid"] is None and r["games"] == []


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_db(c)
    yield c
    c.close()


def test_tds_exclude_passing_and_unmatched_zero(conn):
    with conn:
        for espn, gsis in ((10, "g10"), (11, "g11")):
            conn.execute("INSERT INTO nfl_player_ids (espn_id, gsis_id, updated_at) VALUES (?,?,'n')",
                         (espn, gsis))
        conn.execute("""INSERT INTO nfl_player_weeks (season, week, gsis_id, season_type, updated_at,
            passing_tds, rushing_tds, receiving_tds, def_tds) VALUES (2026,3,'g10','REG','n',4,1,2,1)""")
        conn.execute("""INSERT INTO nfl_player_weeks (season, week, gsis_id, season_type, updated_at,
            passing_tds, rushing_tds, receiving_tds) VALUES (2026,3,'g11','REG','n',3,NULL,NULL)""")
        conn.execute("""INSERT INTO nfl_player_weeks (season, week, gsis_id, season_type, updated_at,
            rushing_tds) VALUES (2026,2,'g10','REG','n',5)""")
    rosters = {1: [{"player_id": 10}, {"player_id": 11}, {"player_id": 99}]}
    main._attach_tds(conn, 2026, 3, rosters)
    assert [r["tds"] for r in rosters[1]] == [4, 0, 0]
    r2 = {1: [{"player_id": 10}]}
    main._attach_tds(conn, 2026, None, r2)
    assert r2[1][0]["tds"] == 0


def test_routes_expose_keys_and_fragment_honours_m(tmp_path, monkeypatch):
    from tests.test_rail_data import _client
    client, m = _client(tmp_path, monkeypatch)
    seen = {}
    real = m.templates.TemplateResponse

    def spy(name, context, *a, **k):
        seen[name] = context
        return real(name, context, *a, **k)

    monkeypatch.setattr(m.templates, "TemplateResponse", spy)
    for path, tpl in (("/?m=nope", "dashboard.html"),
                      ("/fragment/scoreboard?m=1-2", "_scoreboard.html")):
        assert client.get(path).status_code == 200
        for k in ("games", "selected_game", "selected_gid", "my_game", "other_games"):
            assert k in seen[tpl]


def test_card_context_has_nfl_games_not_games(conn):
    ctx = main._card_context(conn, 2026, 1)
    assert isinstance(ctx["nfl_games"], dict) and "games" not in ctx
    merged = ctx | main._select_games([_g(1, 2)], 1, None)
    assert isinstance(merged["nfl_games"], dict) and isinstance(merged["games"], list)


# -- rendered pages ---------------------------------------------------------

import re

WEEK = 4
# player_id -> (rushing, receiving, passing); expected tds = rush + rec
TD_PLAYERS = {101: (0, 0, 0), 102: (1, 0, 0), 103: (1, 2, 0), 104: (2, 3, 0), 105: (0, 0, 4)}


def _seed_week(tmp_path, monkeypatch, state="in_season"):
    from tests.test_rail_data import _client
    client, m = _client(tmp_path, monkeypatch)
    from app.config import get_config
    season = get_config().current_season
    with db.session(tmp_path / "r.db") as c:
        db.set_meta(c, "status", {"state": state, "current_matchup_period": WEEK})
        for t in range(1, 6):
            db.upsert_team(c, season, t, f"Team {chr(64 + t)}", f"Owner {t}", f"T{t}")
        # 1 v 2, 3 v 4, 5 bye. Team 1 (signed in) plays 1-2.
        for h, a in ((1, 2), (3, 4), (5, None)):
            db.upsert_team_week(c, season, WEEK, h, 100.0, 110.0, a, False, None)
            if a:
                db.upsert_team_week(c, season, WEEK, a, 90.0, 105.0, h, False, None)
        rows, ids, nfl = [], {}, []
        pid = 100
        for t in range(1, 6):
            for slot, starter in (("RB", True), ("WR", True), ("BE", False)):
                pid += 1
                rows.append({"team_id": t, "player_id": pid, "name": f"Player{pid}",
                             "position": "RB", "lineup_slot": slot, "pro_team": "KC",
                             "is_starter": starter, "projected": 10.0, "actual": 5.0, "tds": 9})
        # Team 3 (not mine) carries the TD players, so selecting 3-4 shows them.
        for i, pid_ in enumerate(TD_PLAYERS):
            rows[6 + i] = {**rows[6 + i], "player_id": pid_, "name": f"Scorer{pid_}",
                           "lineup_slot": "BE" if i == 5 else "RB", "is_starter": i < 5}
        db.replace_team_week_players(c, season, WEEK, rows, quiet=True)
        for pid_, (ru, rc, pa) in TD_PLAYERS.items():
            ids[pid_] = {"gsis_id": f"g{pid_}", "name": f"Scorer{pid_}", "position": "RB", "team": "KC"}
            nfl.append({"season": season, "week": WEEK, "gsis_id": f"g{pid_}", "season_type": "REG",
                        "rushing_tds": ru, "receiving_tds": rc, "passing_tds": pa})
        db.replace_nfl_player_ids(c, ids)
        db.replace_nfl_player_weeks(c, season, nfl)
    return client


def _labels(html):
    return re.findall(r'aria-label="(\d+) touchdowns?"', html)


@pytest.mark.parametrize("path", ["/", "/fragment/scoreboard"])
def test_rendered_selection_and_tds(tmp_path, monkeypatch, path):
    client = _seed_week(tmp_path, monkeypatch)
    sep = "&" if "?" in path else "?"
    # default: my game
    r = client.get(path)
    assert r.status_code == 200
    assert r.text.count('aria-current="true"') == 1
    assert "m=1-2" in r.text
    # other game selected, with its TD footballs
    r = client.get(f"{path}{sep}m=3-4")
    assert r.status_code == 200
    assert r.text.count('aria-current="true"') == 1
    hx = re.findall(r'hx-get="([^"]*)"', r.text)
    assert any("m=3-4" in h for h in hx)
    labels = _labels(r.text)
    # 1/3/5 TDs shown; the 0-TD and passing-only (4 thrown) players show none
    assert sorted(set(labels)) == ["1", "3", "5"]
    # bad / bye / unknown fall back without error
    for m in ("bad", "5-bye", "99-98", "%00"):
        r = client.get(f"{path}{sep}m={m}")
        assert r.status_code == 200
        assert r.text.count('aria-current="true"') == 1
    assert "m=1-2" in client.get(f"{path}{sep}m=bad").text
    r = client.get(f"{path}{sep}m=5-bye")
    assert any("m=5-bye" in h for h in re.findall(r'hx-get="([^"]*)"', r.text))


def test_preseason_renders(tmp_path, monkeypatch):
    client = _seed_week(tmp_path, monkeypatch, state="pre_draft")
    assert client.get("/").status_code == 200
    assert client.get("/?m=junk").status_code == 200


# -- live ESPN own-scored TDs (T49) -----------------------------------------

def test_stat_dict_to_scored_tds():
    from app import espn_client as ec
    stat = {"stats": {"4": 3, "25": 1.0, "43": "2", "104": 1, "105": 9, "63": 1,
                      "19": 2, "20": 1}}  # 19/20 = 2-pt conversions, 105/63 overlap sums
    assert ec._touchdowns(stat, ec.OWN_TD_STAT_IDS) == 4   # 1 + 2 + 1, passing excluded
    assert ec._touchdowns(stat) == 7                       # old total still counts passing
    assert ec._touchdowns({}, ec.OWN_TD_STAT_IDS) == 0


def _live_rows(conn, season, scored):
    rows = [{"team_id": 1, "player_id": pid, "name": f"P{pid}", "position": "RB",
             "lineup_slot": "RB", "pro_team": "KC", "is_starter": True, "tds": 9,
             "scored_tds": sc} for pid, sc in scored.items()]
    db.replace_team_week_players(conn, season, 3, rows, quiet=True)


def test_tds_fallback_order_nflverse_then_espn_then_zero(conn):
    # 10: in nflverse (2) and ESPN says 5 -> 2; 11: only ESPN (3) -> 3;
    # 12: ESPN NULL (old row) -> 0; 13: not mapped, ESPN 1 -> 1.
    with conn:
        for espn in (10, 11, 12):
            conn.execute("INSERT INTO nfl_player_ids (espn_id, gsis_id, updated_at) VALUES (?,?,'n')",
                         (espn, f"g{espn}"))
        conn.execute("""INSERT INTO nfl_player_weeks (season, week, gsis_id, season_type, updated_at,
            rushing_tds, passing_tds) VALUES (2026,3,'g10','REG','n',2,6)""")
    _live_rows(conn, 2026, {10: 5, 11: 3, 12: None, 13: 1})
    rosters = db.fetch_team_week_players(conn, 2026, 3, starters_only=False)
    main._attach_tds(conn, 2026, 3, rosters)
    got = {r["player_id"]: r["tds"] for r in rosters[1]}
    assert got == {10: 2, 11: 3, 12: 0, 13: 1}


def test_scored_tds_migration_on_old_schema(tmp_path):
    c = db.connect(tmp_path / "old.db")
    db.init_db(c)
    with c:
        c.execute("ALTER TABLE team_week_players DROP COLUMN scored_tds")
        c.execute("""INSERT INTO team_week_players (season, week, team_id, player_id, name,
                     lineup_slot, is_starter, updated_at) VALUES (2026,3,1,10,'P','RB',1,'n')""")
    db.init_db(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(team_week_players)")}
    assert "scored_tds" in cols
    rosters = db.fetch_team_week_players(c, 2026, 3, starters_only=False)
    assert rosters[1][0]["scored_tds"] is None
    main._attach_tds(c, 2026, 3, rosters)
    assert rosters[1][0]["tds"] == 0
    c.close()


# -- T51: ESPN plumbing + nflverse columns ----------------------------------

def _stat(source, stats=None, period=3):
    return {"scoringPeriodId": period, "statSourceId": source, "appliedTotal": 5.0,
            "stats": stats or {}}


def _entry(pid, stats):
    return {"playerId": pid, "lineupSlotId": 0,
            "playerPoolEntry": {"player": {"id": pid, "fullName": f"P{pid}",
                                           "defaultPositionId": 1, "proTeamId": 1,
                                           "stats": stats}}}


def test_espn_payload_to_db_row(conn, monkeypatch):
    from app import espn_client, poller
    entries = [
        _entry(1, [_stat(0, {"4": 2, "25": 1}), _stat(1, {"25": 7})]),
        _entry(2, [_stat(1, {"25": 1})]),
        _entry(3, []),
    ]
    payload = {"schedule": [{"matchupPeriodId": 3, "home": {
        "teamId": 1, "totalPoints": 1.0,
        "rosterForCurrentScoringPeriod": {"entries": entries}}}]}
    monkeypatch.setattr(espn_client, "_get", lambda *a, **k: payload)
    assert poller.sync_matchup_players(conn, 2026, 3, quiet=True) == 3
    got = {r["player_id"]: (r["tds"], r["scored_tds"]) for r in
           conn.execute("SELECT player_id, tds, scored_tds FROM team_week_players")}
    assert got == {1: (3, 1), 2: (0, None), 3: (0, None)}


@pytest.mark.parametrize("sid", [25, 43, 94, 101, 102, 103, 104])
def test_each_own_td_stat_id_counts_one(sid):
    from app import espn_client as ec
    assert sid in ec.OWN_TD_STAT_IDS
    assert ec._touchdowns({"stats": {str(sid): 1}}, ec.OWN_TD_STAT_IDS) == 1


def test_passing_and_overlap_ids_count_zero():
    from app import espn_client as ec
    assert set(ec.OWN_TD_STAT_IDS) == {25, 43, 94, 101, 102, 103, 104}
    assert ec._touchdowns({"stats": {"4": 1, "63": 1, "105": 1}}, ec.OWN_TD_STAT_IDS) == 0


def test_nflverse_row_with_zero_tds_beats_espn_count(conn):
    # nflverse is authoritative once the player has a row: 0 stays 0 even if
    # ESPN's live count said 2 (e.g. a stat correction).
    with conn:
        conn.execute("INSERT INTO nfl_player_ids (espn_id, gsis_id, updated_at) VALUES (10,'g10','n')")
        conn.execute("""INSERT INTO nfl_player_weeks (season, week, gsis_id, season_type, updated_at,
            rushing_tds) VALUES (2026,3,'g10','REG','n',0)""")
    _live_rows(conn, 2026, {10: 2})
    rosters = db.fetch_team_week_players(conn, 2026, 3, starters_only=False)
    main._attach_tds(conn, 2026, 3, rosters)
    assert rosters[1][0]["tds"] == 0


def test_nflverse_return_and_fumble_tds_counted_without_double_count(conn):
    with conn:
        conn.execute("""INSERT INTO nfl_player_weeks (season, week, gsis_id, season_type, updated_at,
            rushing_tds, special_teams_tds, def_tds, fumble_recovery_tds)
            VALUES (2026,3,'a','REG','n',1,1,0,1)""")
        conn.execute("""INSERT INTO nfl_player_weeks (season, week, gsis_id, season_type, updated_at,
            def_tds, fumble_recovery_tds, passing_tds)
            VALUES (2026,3,'b','REG','n',1,1,3)""")
    assert db.fetch_week_player_tds(conn, 2026, 3) == {"a": 3, "b": 1}


def test_nflverse_tracks_the_new_columns():
    from app import nflverse
    assert {"special_teams_tds", "fumble_recovery_tds"} <= set(nflverse.PLAYER_WEEK_STATS)


def test_new_nfl_columns_migration_clears_stats_stamp_once(tmp_path):
    c = db.connect(tmp_path / "old.db")
    db.init_db(c)
    with c:
        c.execute("ALTER TABLE nfl_player_weeks DROP COLUMN special_teams_tds")
        c.execute("ALTER TABLE nfl_player_weeks DROP COLUMN fumble_recovery_tds")
        c.execute("""INSERT INTO nfl_player_weeks (season, week, gsis_id, season_type, updated_at,
                     rushing_tds, def_tds) VALUES (2026,3,'a','REG','n',2,1)""")
    db.set_nflverse_sync(c, "stats_player:2026", "stamp", 5)
    db.set_nflverse_sync(c, "pbp:2026", "stamp", 5)
    db.init_db(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(nfl_player_weeks)")}
    assert {"special_teams_tds", "fumble_recovery_tds"} <= cols
    assert db.get_nflverse_sync(c, "stats_player:2026") is None
    assert db.get_nflverse_sync(c, "pbp:2026") is not None   # other tags untouched
    assert db.fetch_week_player_tds(c, 2026, 3) == {"a": 3}   # NULLs read as 0
    db.set_nflverse_sync(c, "stats_player:2026", "fresh", 9)
    db.init_db(c)
    assert db.get_nflverse_sync(c, "stats_player:2026")["last_updated"] == "fresh"
    c.close()

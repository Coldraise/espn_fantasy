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

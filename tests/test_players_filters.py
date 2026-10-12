"""Server-side /players filters: parsing, applying, chips, column groups."""

import os
import sys
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, players


def _r(pid, name="Player", team="KC", on=0, avg=None, proj=None, own=None, snap=None,
       rookie=False, inj=None, starter=None, game=None):
    return {"player_id": pid, "name": name, "pro_team": team, "on_team_id": on, "avg": avg,
            "projected": proj, "percent_owned": own, "snap_pct": snap, "rookie": rookie,
            "injury_status": inj, "is_starter": starter, "game": game}


ROWS = [
    _r(1, "Patrick Mahomes", "KC", on=6, avg=24.0, proj=22.0, own=99.0, snap=1.0, starter=1,
       game={"opp": "BUF"}),
    _r(2, "Josh Allen", "BUF", on=0, avg=4.0, proj=3.0, own=40.0, snap=None, inj="QUESTIONABLE"),
    _r(3, "Rookie Guy", "DET", on=0, avg=8.0, proj=9.0, own=10.0, snap=0.55, rookie=True, inj="OUT"),
    _r(4, "Bench Man", "KC", on=7, avg=None, proj=None, own=None, snap=0.2, starter=0,
       inj="INJURY_RESERVE"),
]


def ids(f):
    return [r["player_id"] for r in players.apply_filters(ROWS, f)]


def run(**q):
    return ids(players.parse_filters(q))


def test_no_filters_returns_everything_in_order():
    assert players.parse_filters({}) == {}
    assert run() == [1, 2, 3, 4]


def test_each_filter_alone():
    assert run(q="MAHO") == [1]
    assert run(team="buf,det") == [2, 3]
    assert run(owner="6") == [1]
    assert run(owner="fa") == [2, 3]
    assert run(avail="free") == [2, 3]
    assert run(avail="rostered") == [1, 4]
    assert run(avail="all") == [1, 2, 3, 4]
    assert run(avg_min="8") == [1, 3]
    assert run(proj_min="9") == [1, 3]
    assert run(own_min="40") == [1, 2]
    assert run(own_max="40") == [2, 3]
    assert run(rookie="1") == [3]
    assert run(starter="1") == [1]
    assert run(plays="1") == [1]
    assert run(inj="q") == [2]
    assert run(inj="o,ir") == [3, 4]
    assert run(inj="any") == [2, 3, 4]


def test_owner_fa_combined_with_team_ids():
    assert run(owner="fa,6") == [1, 2, 3]
    assert run(owner="fa,7,99") == [2, 3, 4]


def test_snap_min_excludes_missing_snap():
    assert run(snap_min="50") == [1, 3]
    assert 2 not in run(snap_min="0")


def test_combined_filters_and_together():
    assert run(avail="free", avg_min="5", inj="any") == [3]
    assert run(team="KC", avail="rostered", starter="1") == [1]


def test_legacy_available_maps_to_free():
    assert players.parse_filters({"available": "1"}) == {"avail": "free"}
    assert run(available="1") == [2, 3]
    assert players.parse_filters({"available": "0"}) == {}


def test_invalid_values_ignored():
    q = {"avg_min": "abc", "proj_min": "nan", "own_min": "inf", "avail": "bogus",
         "inj": "zzz", "owner": "x,-1", "rookie": "yes", "q": "  ", "team": ",,"}
    assert players.parse_filters(q) == {}
    assert run(**q) == [1, 2, 3, 4]


def test_percent_filters_clamped():
    assert players.parse_filters({"own_min": "-5", "snap_min": "250"}) == {
        "own_min": 0.0, "snap_min": 100.0}


def test_cols_parsing():
    everything = {k for k, _ in players.COLUMN_GROUPS}
    assert players.parse_cols(None) == everything
    assert players.parse_cols("bogus") == everything
    assert players.parse_cols("weeks, rec ,nope") == {"weeks", "rec"}
    groups = {g["key"]: g for g in players.column_groups("proj")}
    assert groups["proj"]["on"] and not groups["weeks"]["on"]
    assert all(g["default_on"] for g in groups.values())
    assert all(g["on"] for g in players.column_groups(None))


def test_every_column_has_a_group_key():
    cols = players.columns([1, 2], 3, "", True)
    by_key = {c["key"]: c["group"] for c in cols}
    assert by_key["name"] is None and by_key["total"] is None and by_key["avg"] is None
    assert by_key["w1"] == "weeks" and by_key["projected"] == "proj"
    assert by_key["opp"] == "matchup" and by_key["gp"] == "usage" and by_key["snap_pct"] == "usage"
    assert by_key["pass_yds"] == "pass" and by_key["tgt_share"] == "rec" and by_key["sacks"] == "def"
    assert all("group" in c for c in cols)


def test_remove_url_round_trip():
    f = players.parse_filters({"q": "a&b", "team": "KC,BUF", "avail": "free", "avg_min": "10.5",
                               "owner": "fa,6"})
    keep = {"pos": "WR", "sort": "avg", "dir": "desc", "cols": "weeks,proj"}
    chips = {c["key"]: c for c in players.active_filters(f, {6: "Obsessions"}, keep)}
    assert set(chips) == set(f)
    assert chips["avail"]["label"] == "Free agents"
    assert chips["owner"]["value_label"] == "Free agents, Obsessions"
    url = chips["q"]["remove_url"]
    assert url.startswith("/players?") and "a%26b" not in url
    qs = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert "q" not in qs
    assert qs["team"] == "BUF,KC" and qs["avg_min"] == "10.5" and qs["owner"] == "fa,6"
    assert (qs["pos"], qs["sort"], qs["dir"], qs["cols"]) == ("WR", "avg", "desc", "weeks,proj")
    # re-parsing the chip's own url yields the filters minus the removed one
    again = players.parse_filters(qs)
    assert again == {k: v for k, v in f.items() if k != "q"}


def test_remove_url_of_last_filter_is_bare():
    f = players.parse_filters({"rookie": "1"})
    assert players.active_filters(f, {}, {})[0]["remove_url"] == "/players"


def test_q_value_is_url_encoded():
    f = players.parse_filters({"q": "a&b", "rookie": "1"})
    chips = {c["key"]: c for c in players.active_filters(f, {}, {})}
    assert "q=a%26b" in chips["rookie"]["remove_url"]


# -- route ----------------------------------------------------------------

def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "f.db"))
    monkeypatch.setenv("CONFIG_PATH", "config/config.yml")
    from fastapi.testclient import TestClient
    from app import auth, main
    with db.session(tmp_path / "f.db") as c:
        db.init_db(c)
        db.set_meta(c, "status", {"state": "in_season"})
        auth.seed_accounts(c, {1: "Team A"})
        token = auth.make_token(c, db.get_franchise_user(c, 1))
    from app import config as _config
    _config.get_config(reload=True)
    client = TestClient(main.app)
    client.cookies.set(auth.SESSION_COOKIE, token)
    return client, main


def test_route_filters_200_and_context(tmp_path, monkeypatch):
    client, main = _client(tmp_path, monkeypatch)
    seen = []
    real = main.templates.TemplateResponse

    def spy(name, context, *a, **k):
        seen.append(context)
        return real(name, context, *a, **k)

    monkeypatch.setattr(main.templates, "TemplateResponse", spy)
    for path in ("/players", "/players?avail=free&pos=WR&avg_min=5", "/players?available=1",
                 "/players?avg_min=zzz&inj=bogus&cols=nope&snap_min=10"):
        assert client.get(path).status_code == 200
    plain, combo, legacy, junk = seen
    for ctx in seen:
        for key in ("total_rows", "filters", "active_filters", "owners", "teams", "column_groups"):
            assert key in ctx
        assert all("group" in c for c in ctx["columns"])
    assert plain["filters"] == {} and plain["active_filters"] == []
    assert combo["filters"] == {"avail": "free", "avg_min": 5.0}
    assert {c["key"] for c in combo["active_filters"]} == {"avail", "avg_min"}
    assert legacy["available_only"] and legacy["filters"] == {"avail": "free"}
    assert junk["filters"] == {"snap_min": 10.0}


def test_repeated_params_merge_like_csv():
    from starlette.datastructures import QueryParams
    q = QueryParams("inj=q&inj=d&team=KC&team=BUF&owner=fa&owner=6&cols=pass&cols=rush")
    f = players.parse_filters(q)
    assert f["inj"] == ["q", "d"] and f["team"] == ["BUF", "KC"] and f["owner"] == ["fa", 6]
    assert players.parse_cols(players.merged(q, "cols")) == {"pass", "rush"}


def test_repeated_and_mixed_params():
    from starlette.datastructures import QueryParams
    f = players.parse_filters(QueryParams("inj=q&inj=d&inj=q"))
    assert f["inj"] == ["q", "d"]
    f = players.parse_filters(QueryParams("team=KC,BUF&team=SF&team=KC"))
    assert f["team"] == ["BUF", "KC", "SF"]
    assert players.parse_cols(players.merged(QueryParams("cols=weeks&cols=proj"), "cols")) \
        == {"weeks", "proj"}
    chips = {c["key"]: c for c in players.active_filters(f, {}, {"cols": "weeks,proj"})}
    qs = parse_qs(urlsplit(chips["team"]["remove_url"]).query)
    assert "team" not in qs and qs["cols"] == ["weeks,proj"]


def test_route_repeated_params_and_empty_result_keeps_positions(tmp_path, monkeypatch):
    client, main = _client(tmp_path, monkeypatch)
    with db.session(tmp_path / "f.db") as c:
        with c:
            c.execute("INSERT INTO player_projections (season, week, player_id, name, position, "
                      "pro_team, projected, updated_at) VALUES (2026, 7, 1, 'A', 'WR', 'KC', 5, 'n')")
    seen = []
    real = main.templates.TemplateResponse

    def spy(name, context, *a, **k):
        seen.append(context)
        return real(name, context, *a, **k)

    monkeypatch.setattr(main.templates, "TemplateResponse", spy)
    assert client.get("/players?inj=q&inj=d").status_code == 200
    assert seen[-1]["filters"] == {"inj": ["q", "d"]}
    assert client.get("/players?q=zzzz").status_code == 200
    assert seen[-1]["rows"] == [] and seen[-1]["positions"] == seen[0]["positions"] != []


def test_snap_min_compares_fraction_against_percent():
    rows = [_r(1, snap=0.4996), _r(2, snap=0.5), _r(3, snap=0.75), _r(4, snap=None)]
    pick = lambda m: [r["player_id"] for r in players.apply_filters(rows, {"snap_min": m})]
    assert pick(50.0) == [2, 3]
    assert pick(70.0) == [3]
    assert pick(0.0) == [1, 2, 3]

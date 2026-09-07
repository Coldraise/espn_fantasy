"""nflverse parsing and storage.

Offline by design: the fetch layer is exercised by handing it bytes, never by
touching the network, so the suite stays deterministic and runs on a plane.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gzip
import sqlite3

import pytest

from app import db, nflverse


def _gz(text: str) -> bytes:
    return gzip.compress(text.encode())


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.init_db(connection)
    yield connection
    connection.close()


# --- value parsing --------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("12", 12), ("12.0", 12), ("0.3158", 0.3158), ("-1.5", -1.5),
    ("", None), ("   ", None), (None, None),
    # nflverse writes R's missing marker into CSV; float('NA') raises, and
    # keeping the string would poison every average downstream.
    ("NA", None), ("NaN", None), ("not a number", None),
])
def test_num_parsing(raw, expected):
    assert nflverse._num(raw) == expected


def test_num_returns_int_for_whole_numbers():
    """Counting stats read as '7', not '7.0', on the page."""
    assert isinstance(nflverse._num("7"), int)
    assert isinstance(nflverse._num("7.5"), float)


# --- team abbreviations ---------------------------------------------------

def test_team_aliases_are_translated_to_espn_spelling():
    assert nflverse.team("LA") == "LAR"
    assert nflverse.team("WAS") == "WSH"
    assert nflverse.team("kc") == "KC"
    assert nflverse.team("") is None and nflverse.team(None) is None


def test_every_nflverse_team_maps_to_a_known_espn_team():
    """A mismatch here is silent -- the D/ST join just returns nothing -- so it
    is asserted rather than discovered in a season with no defensive stats."""
    from app.players import PRO_TEAM_COLORS

    nflverse_codes = set(PRO_TEAM_COLORS) - set(nflverse.TEAM_ALIASES.values())
    nflverse_codes |= set(nflverse.TEAM_ALIASES)
    assert {nflverse.team(c) for c in nflverse_codes} <= set(PRO_TEAM_COLORS)


# --- fetch layer ----------------------------------------------------------

def test_fetch_csv_parses_a_gzipped_release(monkeypatch):
    monkeypatch.setattr(nflverse, "_get", lambda url: _gz("a,b\n1,2\n3,4\n"))
    assert nflverse.fetch_csv("t", "f.csv.gz") == [
        {"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


def test_player_ids_skips_rows_with_no_espn_id(monkeypatch):
    """Only about two thirds of nflverse's 25k players carry an espn_id; the
    rest are people ESPN's fantasy game never listed."""
    monkeypatch.setattr(nflverse, "_get", lambda url: _gz(
        "espn_id,gsis_id,pfr_id,display_name,position,latest_team\n"
        "8439,00-0023459,RodgAa00,Aaron Rodgers,QB,PIT\n"
        ",00-0099999,XxxxYy00,Nobody Known,WR,LA\n"
    ))
    ids = nflverse.player_ids()
    assert list(ids) == [8439]
    assert ids[8439]["gsis_id"] == "00-0023459"


def test_player_ids_normalises_the_team(monkeypatch):
    monkeypatch.setattr(nflverse, "_get", lambda url: _gz(
        "espn_id,gsis_id,pfr_id,display_name,position,latest_team\n"
        "1,00-1,A,Someone,WR,LA\n"))
    assert nflverse.player_ids()[1]["team"] == "LAR"


def test_weekly_player_stats_keeps_only_the_curated_columns(monkeypatch):
    monkeypatch.setattr(nflverse, "_get", lambda url: _gz(
        "player_id,player_display_name,position,season_type,week,team,opponent_team,"
        "targets,fantasy_points_ppr,headshot_url\n"
        "00-0023459,Aaron Rodgers,QB,REG,3,WAS,LA,0,18.4,http://x/y.png\n"))
    row = nflverse.weekly_player_stats(2025)[0]
    assert row["team"] == "WSH" and row["opponent"] == "LAR"
    assert row["week"] == 3 and row["fantasy_points_ppr"] == 18.4
    # 150 columns come down the wire; we store ~50 and headshots are not one.
    assert "headshot_url" not in row


def _http_error(code):
    """Patch at urlopen, not at _get: translating 404 into NotPublished is the
    behaviour under test, and patching _get would skip it entirely."""
    import urllib.error

    def raiser(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, code, "boom", {}, None)
    return raiser


def test_a_missing_season_raises_not_published(monkeypatch):
    """nflverse builds stats_player_week_2026 only once week 1 is played, so
    asking for it in August is normal and must not read as a failure."""
    monkeypatch.setattr(nflverse.urllib.request, "urlopen", _http_error(404))
    with pytest.raises(nflverse.NotPublished):
        nflverse.weekly_player_stats(2026)


def test_a_server_error_is_not_swallowed(monkeypatch):
    """Only 404 means "not yet" -- a 500 is a real failure and must surface."""
    import urllib.error

    monkeypatch.setattr(nflverse.urllib.request, "urlopen", _http_error(500))
    with pytest.raises(urllib.error.HTTPError):
        nflverse.weekly_player_stats(2025)


def test_release_timestamp_survives_a_missing_file(monkeypatch):
    """A release with no timestamp.json must degrade to "always download",
    never to a crash inside the poll loop."""
    monkeypatch.setattr(nflverse.urllib.request, "urlopen", _http_error(404))
    assert nflverse.release_timestamp("players") is None


# --- storage --------------------------------------------------------------

def test_table_columns_match_the_shared_tuples(conn):
    """db.py builds its INSERT from app/nflverse.py's tuples while the CREATE
    TABLE spells them out; a drift between the two is a silent write failure."""
    for table, columns in (("nfl_player_weeks", db._NFL_PLAYER_WEEK_COLUMNS),
                           ("nfl_team_weeks", db._NFL_TEAM_WEEK_COLUMNS)):
        actual = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert actual == set(columns) | {"updated_at"}, table


def test_replacing_a_season_leaves_other_seasons_alone(conn):
    for season in (2024, 2025):
        db.replace_nfl_player_weeks(conn, season, [
            {"season": season, "week": 1, "gsis_id": "00-1", "season_type": "REG",
             "fantasy_points_ppr": 10.0}])
    db.replace_nfl_player_weeks(conn, 2025, [
        {"season": 2025, "week": 1, "gsis_id": "00-1", "season_type": "REG",
         "fantasy_points_ppr": 99.0}])
    assert db.fetch_nfl_player_weeks(conn, 2024)[0]["fantasy_points_ppr"] == 10.0
    assert db.fetch_nfl_player_weeks(conn, 2025)[0]["fantasy_points_ppr"] == 99.0


def test_playoff_weeks_are_excluded_from_reads(conn):
    """A January line would otherwise be averaged in beside seventeen regular
    season weeks, quietly inflating anyone whose team went deep."""
    db.replace_nfl_player_weeks(conn, 2025, [
        {"season": 2025, "week": 1, "gsis_id": "00-1", "season_type": "REG"},
        {"season": 2025, "week": 19, "gsis_id": "00-1", "season_type": "POST"},
    ])
    assert [r["week"] for r in db.fetch_nfl_player_weeks(conn, 2025)] == [1]


def test_player_id_map_upserts_rather_than_replaces(conn):
    """Losing a mapping would orphan every stat row already stored for them."""
    db.replace_nfl_player_ids(conn, {1: {"gsis_id": "00-1", "name": "A"}})
    db.replace_nfl_player_ids(conn, {2: {"gsis_id": "00-2", "name": "B"}})
    assert set(db.nfl_id_map(conn)) == {1, 2}


def test_sync_state_roundtrip(conn):
    assert db.get_nflverse_sync(conn, "players") is None
    db.set_nflverse_sync(conn, "players", "2026-08-31 11:45:43 EDT", 16771)
    assert db.get_nflverse_sync(conn, "players")["last_updated"] == "2026-08-31 11:45:43 EDT"

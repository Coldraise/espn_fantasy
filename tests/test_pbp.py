"""Per-down target/carry aggregation from play-by-play.

Same idiom as tests/test_nflverse.py: the fetch layer is exercised by handing
it gzipped bytes, never the network, so the suite stays deterministic.
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


def _down_row(season, week, gsis_id, **counters):
    """A full nfl_player_down_weeks row -- every counter column is NOT NULL, so
    unlike nfl_player_weeks's fixtures a partial dict would fail to insert."""
    row = {"season": season, "week": week, "gsis_id": gsis_id}
    row.update(dict.fromkeys(nflverse.DOWN_COUNTER_STATS, 0))
    row.update(counters)
    return row


# --- aggregation ------------------------------------------------------------

PBP_HEADER = ("week,down,season_type,pass_attempt,rush_attempt,"
              "receiver_player_id,rusher_player_id\n")


def test_play_by_play_downs_hand_computed(monkeypatch):
    """One receiver targeted twice on the same down/week (the counter must
    increment, not overwrite), one rusher carrying once, plus three rows that
    must be filtered out: a POST week, a kickoff with a blank down, and a
    sack (pass_attempt=1 with no receiver)."""
    csv_text = PBP_HEADER + (
        "1,1,REG,1,0,00-1,\n"      # target #1: 00-1, week 1, down 1
        "1,1,REG,1,0,00-1,\n"      # target #2: same key -> counter increments
        "1,1,POST,1,0,00-1,\n"     # playoff week -> must be excluded
        "1,,REG,0,0,,\n"           # kickoff, blank down -> must be skipped
        "1,2,REG,1,0,,\n"          # sack: pass_attempt=1, no receiver -> not a target
        "1,3,REG,0,1,,00-2\n"      # carry: 00-2, week 1, down 3
    )
    monkeypatch.setattr(nflverse, "_get", lambda url: _gz(csv_text))
    rows = nflverse.play_by_play_downs(2025)
    by_key = {(r["gsis_id"], r["week"]): r for r in rows}

    assert set(by_key) == {("00-1", 1), ("00-2", 1)}
    assert by_key[("00-1", 1)]["targets_d1"] == 2
    assert by_key[("00-1", 1)]["targets_d2"] == 0
    assert by_key[("00-1", 1)]["carries_d1"] == 0
    assert by_key[("00-2", 1)]["carries_d3"] == 1
    assert by_key[("00-2", 1)]["targets_d3"] == 0
    assert all(r["season"] == 2025 for r in rows)


def test_same_player_different_weeks_stay_separate_rows(monkeypatch):
    csv_text = PBP_HEADER + (
        "1,1,REG,1,0,00-1,\n"
        "2,1,REG,1,0,00-1,\n"
    )
    monkeypatch.setattr(nflverse, "_get", lambda url: _gz(csv_text))
    rows = nflverse.play_by_play_downs(2025)
    by_key = {(r["gsis_id"], r["week"]): r for r in rows}

    assert set(by_key) == {("00-1", 1), ("00-1", 2)}
    assert by_key[("00-1", 1)]["targets_d1"] == 1
    assert by_key[("00-1", 2)]["targets_d1"] == 1


def test_a_missing_season_raises_not_published(monkeypatch):
    """nflverse builds play_by_play_2026 only once week 1 is played, so asking
    for it in August is normal and must propagate, not be swallowed."""
    def raiser(url):
        raise nflverse.NotPublished(url)

    monkeypatch.setattr(nflverse, "_get", raiser)
    with pytest.raises(nflverse.NotPublished):
        nflverse.play_by_play_downs(2026)


# --- storage ------------------------------------------------------------


def test_replace_and_fetch_round_trip(conn):
    rows = [
        _down_row(2025, 1, "00-1", targets_d1=2),
        _down_row(2025, 1, "00-2", carries_d3=1),
    ]
    written = db.replace_nfl_player_down_weeks(conn, 2025, rows)
    assert written == 2

    fetched = db.fetch_nfl_player_down_weeks(conn, 2025)
    by_gsis = {r["gsis_id"]: r for r in fetched}
    assert by_gsis["00-1"]["targets_d1"] == 2
    assert by_gsis["00-2"]["carries_d3"] == 1


def test_replacing_a_season_replaces_rather_than_duplicates(conn):
    db.replace_nfl_player_down_weeks(conn, 2025, [_down_row(2025, 1, "00-1", targets_d1=2)])
    db.replace_nfl_player_down_weeks(conn, 2025, [_down_row(2025, 1, "00-1", targets_d1=5)])
    fetched = db.fetch_nfl_player_down_weeks(conn, 2025)
    assert len(fetched) == 1
    assert fetched[0]["targets_d1"] == 5


def test_replacing_a_season_leaves_other_seasons_alone(conn):
    db.replace_nfl_player_down_weeks(conn, 2024, [_down_row(2024, 1, "00-1", targets_d1=1)])
    db.replace_nfl_player_down_weeks(conn, 2025, [_down_row(2025, 1, "00-1", targets_d1=9)])
    assert db.fetch_nfl_player_down_weeks(conn, 2024)[0]["targets_d1"] == 1
    assert db.fetch_nfl_player_down_weeks(conn, 2025)[0]["targets_d1"] == 9


def test_gsis_ids_filter(conn):
    db.replace_nfl_player_down_weeks(conn, 2025, [
        _down_row(2025, 1, "00-1", targets_d1=1),
        _down_row(2025, 1, "00-2", carries_d1=1),
    ])
    assert [r["gsis_id"] for r in db.fetch_nfl_player_down_weeks(conn, 2025, ["00-2"])] == ["00-2"]
    assert db.fetch_nfl_player_down_weeks(conn, 2025, []) == []

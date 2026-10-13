"""`pts` display filter and 2-decimal ingest."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import espn_client
from app.main import _pts, templates


@pytest.mark.parametrize("value, expected", [
    (5, "5,00"), (134.4, "134,40"), (None, "0,00"), (-3.1, "-3,10"),
    (134.455, "134,46"), (1834.5, "1834,50"), (0, "0,00"), (-0.001, "0,00"),
])
def test_pts(value, expected):
    assert _pts(value) == expected


def test_pts_sign():
    assert _pts(12.34, sign=True) == "+12,34"
    assert _pts(-3.1, sign=True) == "-3,10"
    assert _pts(0, sign=True) == "+0,00"
    assert _pts(None, sign=True) == "+0,00"


def test_pts_registered():
    out = templates.env.from_string("{{ x|pts }} {{ x|pts(sign=True) }}").render(x=134.455)
    assert out == "134,46 +134,46"


def test_ingest_keeps_two_decimals(monkeypatch):
    payload = {"players": [{"player": {
        "id": 1, "fullName": "A B", "defaultPositionId": 1,
        "stats": [
            {"scoringPeriodId": 3, "statSourceId": 1, "appliedTotal": 20.456},
            {"scoringPeriodId": 3, "statSourceId": 0, "appliedTotal": 134.46},
        ]}}]}
    monkeypatch.setattr(espn_client, "_get", lambda *a, **k: payload)
    rows = espn_client.player_projections(2026, 3)
    assert rows and rows[0]["actual"] == 134.46
    assert rows[0]["projected"] == 20.46


def test_pts_non_finite_and_garbage():
    assert _pts(float("nan")) == "—"
    assert _pts(float("inf")) == "—"
    assert _pts(float("-inf"), sign=True) == "—"
    assert _pts("abc") == "0,00"


# --- backfill -------------------------------------------------------------

from types import SimpleNamespace

from app import db, poller


class _Matchup:
    def __init__(self, home, away, hs, as_):
        self._home_team_id, self._away_team_id = home, away
        self.home_score, self.away_score = hs, as_
        self.is_playoff = False


class _League:
    currentMatchupPeriod = 3  # weeks 1-2 final, week 3 live

    def __init__(self, scores):
        self.teams = []
        self.scores = scores  # week -> (home, away)
        self.fail = set()

    def scoreboard(self, week):
        if week in self.fail:
            raise RuntimeError("boom")
        h, a = self.scores[week]
        return [_Matchup(1, 2, h, a)]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    league = _League({1: (134.45, 100.0), 2: (90.0, 80.0), 3: (1.0, 2.0)})
    cfg = SimpleNamespace(current_season=2026)
    monkeypatch.setattr(poller, "get_config", lambda: cfg)
    monkeypatch.setattr(poller.espn_client, "get_league", lambda s, refresh=False: league)
    monkeypatch.setattr(poller.espn_client, "call", lambda fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(poller, "schedule_periods", lambda s: [1, 2, 3])
    monkeypatch.setattr(poller, "sync_matchup_players", lambda *a, **k: 0)
    monkeypatch.setattr(poller, "_sync_projection_week", lambda *a, **k: 0)
    yield conn, league
    conn.close()


def _row(conn, week, team=1):
    return dict(conn.execute(
        "SELECT * FROM team_weeks WHERE season=2026 AND week=? AND team_id=?",
        (week, team)).fetchone())


def _seed(conn):
    db.upsert_team_week(conn, 2026, 1, 1, 134.4, 120.5, 2, False, "W")
    db.upsert_team_week(conn, 2026, 2, 1, 80.0, 70.0, 2, False, "W")
    db.upsert_team_week(conn, 2026, 3, 1, 5.0, 60.0, 2, False, None)


def test_backfill_keeps_projection_and_revision_skips_live(setup):
    conn, _ = setup
    _seed(conn)
    changed, failed = poller.backfill_past_weeks(conn, 2026)
    assert failed == [] and changed > 0
    w1 = _row(conn, 1)
    assert w1["points"] == 134.45 and w1["projected"] == 120.5 and w1["revision"] == 1
    w2 = _row(conn, 2)  # 80 -> 90 is a real correction
    assert w2["points"] == 90.0 and w2["projected"] == 70.0 and w2["revision"] == 2
    w3 = _row(conn, 3)  # live week untouched
    assert w3["points"] == 5.0 and w3["projected"] == 60.0


def test_backfill_idempotent(setup):
    conn, _ = setup
    _seed(conn)
    poller.backfill_past_weeks(conn, 2026)
    before = [_row(conn, w) for w in (1, 2, 3)]
    changed, failed = poller.backfill_past_weeks(conn, 2026)
    assert changed == 0 and failed == []
    assert [_row(conn, w) for w in (1, 2, 3)] == before


def test_backfill_reports_failed_week(setup):
    conn, league = setup
    _seed(conn)
    league.fail.add(2)
    _, failed = poller.backfill_past_weeks(conn, 2026)
    assert failed == [2]
    assert _row(conn, 1)["points"] == 134.45  # other weeks still done
    _, failed = poller.backfill_past_weeks(conn, 2026, only_weeks=[1])
    assert failed == []


def test_quiet_replace_emits_no_scoring_events(tmp_path):
    conn = db.connect(tmp_path / "q.db")
    db.init_db(conn)
    row = {"team_id": 1, "player_id": 9, "name": "X", "position": "RB", "tds": 0}
    db.replace_team_week_players(conn, 2026, 1, [row])
    db.replace_team_week_players(conn, 2026, 1, [{**row, "tds": 2}], quiet=True)
    assert conn.execute("SELECT COUNT(*) FROM scoring_events").fetchone()[0] == 0
    db.replace_team_week_players(conn, 2026, 1, [{**row, "tds": 3}])
    assert conn.execute("SELECT COUNT(*) FROM scoring_events").fetchone()[0] == 1
    conn.close()

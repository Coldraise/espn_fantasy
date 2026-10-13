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

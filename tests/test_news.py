"""ESPN news parsing and storage.

Offline by design: parse() is handed a payload dict and fetch()/requests.get is
never called, matching tests/test_nflverse.py's discipline.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from datetime import datetime, timezone

import pytest

from app import db, news

# A realistic pair: one bylined story with an athlete category, and one of the
# 16-of-50-sampled bylineless video clips ESPN's feed actually carries.
PAYLOAD = {
    "articles": [
        {
            "id": 12345,
            "published": "2026-09-03T20:15Z",
            "byline": "Adam Schefter",
            "headline": "Star RB expected to play Sunday",
            "description": "Details on the injury report.",
            "type": "Story",
            "premium": False,
            "links": {"web": {"href": "https://www.espn.com/nfl/story/_/id/12345"}},
            "categories": [
                {"type": "athlete", "athleteId": 4429795},
                {"type": "team", "teamId": 8},
            ],
        },
        {
            "id": 67890,
            "published": "2026-09-03T18:00Z",
            "headline": "Injury report update",
            "type": "Media",
        },
    ]
}


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.init_db(connection)
    yield connection
    connection.close()


# --- fetch ------------------------------------------------------------------

def test_fetch_uses_no_cookies_and_the_public_endpoint(monkeypatch):
    """Same precedent as espn_client.nfl_experience() -- a different host gets
    no fantasy session, ever."""
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return PAYLOAD

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params, timeout))
        return FakeResponse()

    monkeypatch.setattr(news.requests, "get", fake_get)
    assert news.fetch(limit=50) == PAYLOAD
    url, params, timeout = calls[0]
    assert url == news.NFL_NEWS_URL
    assert params == {"limit": 50}
    assert "cookies" not in url.lower()


# --- parse --------------------------------------------------------------

def test_parse_pulls_byline_athlete_ids_and_url():
    rows = news.parse(PAYLOAD)
    story = rows[0]
    assert story["article_id"] == 12345
    assert story["byline"] == "Adam Schefter"
    assert story["athlete_ids"] == [4429795]
    assert story["url"] == "https://www.espn.com/nfl/story/_/id/12345"


def test_parse_survives_missing_links_categories_and_byline():
    """No links, no categories, byline: None -- must still parse, not be
    dropped, since it is exactly the kind of row the roster join needs."""
    rows = news.parse(PAYLOAD)
    assert len(rows) == 2
    clip = rows[1]
    assert clip["article_id"] == 67890
    assert clip["byline"] is None
    assert clip["athlete_ids"] == []
    assert clip["url"] is None


# --- matches ------------------------------------------------------------

def test_matches_handles_a_co_byline_and_differing_case():
    reporters = ("Adam Schefter", "Dan Graziano", "Mike Clay")
    assert news.matches("Adam Schefter and Dan Graziano", reporters) == \
        ["Adam Schefter", "Dan Graziano"]
    assert news.matches("adam SCHEFTER", reporters) == ["Adam Schefter"]
    assert news.matches(None, reporters) == []


# --- storage --------------------------------------------------------------

def test_upsert_news_is_idempotent(conn):
    rows = news.parse(PAYLOAD)
    assert db.upsert_news(conn, rows) == len(rows)
    assert db.upsert_news(conn, rows) == 0


def test_a_revised_headline_updates_in_place(conn):
    """ESPN retitles articles -- unlike transactions, a second sight of the
    same id must update the row, not be ignored, and must never duplicate it."""
    rows = news.parse(PAYLOAD)
    db.upsert_news(conn, rows)

    revised = dict(rows[0], headline="Star RB officially active Sunday")
    written = db.upsert_news(conn, [revised])
    assert written == 0

    stored = [r for r in db.fetch_news(conn) if r["article_id"] == rows[0]["article_id"]]
    assert len(stored) == 1
    assert stored[0]["headline"] == "Star RB officially active Sunday"


def test_prune_news_drops_aged_items_and_their_athlete_rows(conn):
    old = {"article_id": 1, "published": "2020-01-01T00:00Z", "byline": "X",
           "headline": "Old news", "description": None, "url": None,
           "type": "Story", "premium": False, "athlete_ids": [111]}
    recent = {"article_id": 2,
              "published": datetime.now(timezone.utc).date().isoformat() + "T00:00Z",
              "byline": "Y", "headline": "Fresh news", "description": None,
              "url": None, "type": "Story", "premium": False, "athlete_ids": [222]}
    db.upsert_news(conn, [old, recent])

    removed = db.prune_news(conn, keep_days=21)
    assert removed == 1
    assert {r["article_id"] for r in db.fetch_news(conn)} == {2}
    assert conn.execute(
        "SELECT COUNT(*) FROM news_athletes WHERE article_id=1").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM news_athletes WHERE article_id=2").fetchone()[0] == 1


def test_prune_news_still_reaches_an_item_with_no_published_date(conn):
    """A null `published` sorts last under `published DESC`, so the page never
    shows the row -- if prune skipped it too, nothing would ever delete it."""
    orphan = {"article_id": 3, "published": None, "byline": None,
              "headline": "Undated clip", "description": None, "url": None,
              "type": "Media", "premium": False, "athlete_ids": [333]}
    db.upsert_news(conn, [orphan])
    conn.execute("UPDATE news_items SET updated_at='2020-01-01T00:00:00+00:00' "
                 "WHERE article_id=3")

    assert db.prune_news(conn, keep_days=21) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM news_athletes WHERE article_id=3").fetchone()[0] == 0


def test_roster_join_attributes_an_article_to_the_right_team(conn):
    """Same id space as fantasy player ids -- the join is direct, no name
    matching -- so a stored article's athlete id must resolve to the roster
    that actually carries that player."""
    db.replace_team_week_players(conn, 2026, 1, [
        {"team_id": 5, "player_id": 4429795, "name": "Jahmyr Gibbs",
         "position": "RB", "lineup_slot": "RB", "pro_team": "DET",
         "is_starter": True, "projected": 15.0, "actual": None},
    ])
    db.upsert_news(conn, news.parse(PAYLOAD))

    article = next(r for r in db.fetch_news(conn) if r["article_id"] == 12345)
    roster = db.fetch_team_week_players(conn, 2026, 1, starters_only=False)
    rostered_by = {p["player_id"]: team_id for team_id, players in roster.items() for p in players}
    assert rostered_by[article["athlete_ids"][0]] == 5

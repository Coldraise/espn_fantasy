"""ESPN's public NFL news feed.

X/Twitter was the original ask -- a reporter-attributed news panel, `@AdamSchefter`
was the example -- but every unauthenticated route into it is dead, rate-limited,
a JS shell, or pay-per-read (nitter is HTTP 410, syndication.twitter.com 429s on
the first call, x.com serves no content without JS, and the official API has had
no free tier since Feb 2026). ESPN's own news feed turns out to be the better
source anyway: every article carries a `byline` and `categories[] {type:
"athlete", athleteId}`, and those athlete ids are the same id space as fantasy
player ids (see `espn_client.nfl_experience`), so news joins onto rosters with no
name matching -- the one thing an X timeline could never give us.

Same shape as app/nflverse.py: fetch and parse kept apart, parsing pure and
testable against a fixture dict rather than the network.

Measured, not assumed:
  - The feed is hard-capped at 50 articles regardless of `limit` -- a rolling
    window with no cursor, covering roughly the last 36 hours. There is no
    backfill; the panel fills in as the poller runs.
  - `?athlete=<id>` is silently ignored -- ESPN returns the unfiltered feed
    regardless -- so athlete filtering happens at store time off `categories`,
    never by asking ESPN for it.
  - `now.core.api.espn.com/v1/sports/news` is not an alternative: it accepts
    `offset` but ignores `league=nfl` (returns college football) and carries no
    `byline`.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("fantasy.news")

NFL_NEWS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news"
TIMEOUT = 25


def fetch(limit: int = 50) -> dict:
    """The raw feed, newest first.

    This endpoint is public: it is fetched WITHOUT the ESPN session cookies,
    same as `espn_client.nfl_experience()` -- there is no reason to hand a
    fantasy session to a different host. `limit` above 50 changes nothing; the
    feed caps there regardless of what is asked for.
    """
    response = requests.get(NFL_NEWS_URL, params={"limit": limit}, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def parse(payload: dict) -> list[dict]:
    """One row per article. Every field is guarded -- `links.web.href`,
    `categories` and `byline` are all optionally absent (16 of 50 sampled
    articles had `byline: None`, mostly video clips of `type: "Media"`), and an
    article missing all three must still parse rather than be dropped.
    """
    rows = []
    for item in payload.get("articles") or []:
        try:
            article_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        athlete_ids = []
        for category in item.get("categories") or []:
            if category.get("type") != "athlete":
                continue
            try:
                athlete_ids.append(int(category["athleteId"]))
            except (KeyError, TypeError, ValueError):
                continue
        rows.append({
            "article_id": article_id,
            "published": item.get("published"),
            "byline": item.get("byline") or None,
            "headline": item.get("headline") or "",
            "description": item.get("description") or None,
            "url": ((item.get("links") or {}).get("web") or {}).get("href"),
            "type": item.get("type"),
            "premium": bool(item.get("premium")),
            "athlete_ids": athlete_ids,
        })
    return rows


def matches(byline: str | None, reporters) -> list[str]:
    """Which of `reporters` this byline credits, normalised rather than exact --
    a co-byline like "Adam Schefter and Dan Graziano" must match both names, and
    matching is case- and whitespace-insensitive.
    """
    if not byline:
        return []
    normalized = " ".join(byline.split()).lower()
    return [r for r in reporters if " ".join(r.split()).lower() in normalized]

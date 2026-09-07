"""nflverse NFL data.

ESPN tells us what a player *scored*. It never says why: no snap counts, no
target share, no tackles. nflverse publishes all of that as plain CSV on GitHub
Releases, so this module is the second data source -- and the only one that has
anything to say before the fantasy season starts.

Deliberately stdlib only (urllib, gzip, csv). Every dataset we need is published
as `.csv.gz`; reaching for the `.parquet` variants would drag in pyarrow, tens
of megabytes of wheel, to read files that are one megabyte each.

Same shape as espn_client: fetch and parse, no database handles, so it stays
testable against a fixture instead of the network.

Data is CC-BY-4.0 (c) nflverse -- attribution is a licence condition, not a
courtesy, so it is printed in the page footer and the README.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("fantasy.nflverse")

BASE = "https://github.com/nflverse/nflverse-data/releases/download"
USER_AGENT = "espn-fantasy-league-monitor (+https://github.com/nflverse/nflverse-data)"
TIMEOUT = 60

# A CSV field can legitimately be huge (nflverse packs list columns like
# `fg_made_list` into single cells); the default limit rejects those outright.
csv.field_size_limit(10_000_000)

# nflverse and ESPN disagree on exactly two abbreviations. Everything downstream
# -- the colour map, the D/ST join, the roster join -- keys on ESPN's spelling,
# so normalise once at the parse boundary rather than at every use site.
TEAM_ALIASES = {"LA": "LAR", "WAS": "WSH"}

# The columns we keep from `stats_player_week`, which has 150. Kept explicit and
# shared with db.py's table definition (a test asserts the two agree) so adding
# a stat is one edit in each place rather than a silent mismatch.
PLAYER_WEEK_STATS = (
    # passing
    "completions", "attempts", "passing_yards", "passing_tds",
    "passing_interceptions", "passing_air_yards", "passing_first_downs",
    "passing_epa",
    # rushing
    "carries", "rushing_yards", "rushing_tds", "rushing_first_downs",
    "rushing_epa",
    # receiving -- target_share/air_yards_share/wopr/racr are the usage metrics
    # ESPN never exposes, and the whole reason for this data source
    "targets", "receptions", "receiving_yards", "receiving_tds",
    "receiving_air_yards", "receiving_first_downs", "receiving_epa",
    "target_share", "air_yards_share", "wopr", "racr",
    # IDP -- this league starts seven defensive slots, so these matter as much
    # as the offensive columns
    "def_tackles_solo", "def_tackle_assists", "def_tackles_for_loss",
    "def_sacks", "def_qb_hits", "def_interceptions", "def_pass_defended",
    "def_fumbles_forced", "def_tds",
    # kicking
    "fg_made", "fg_att", "fg_long", "pat_made", "pat_att",
    # totals
    "fantasy_points", "fantasy_points_ppr",
)

# Merged in from the snap_counts release, which is a separate file keyed on a
# different id (see snap_counts()).
SNAP_STATS = ("offense_snaps", "offense_pct", "defense_snaps", "defense_pct",
              "st_snaps", "st_pct")

TEAM_WEEK_STATS = (
    "completions", "attempts", "passing_yards", "passing_tds",
    "passing_interceptions", "carries", "rushing_yards", "rushing_tds",
    "receptions", "targets", "receiving_yards", "receiving_tds",
    "def_tackles_solo", "def_sacks", "def_interceptions", "def_pass_defended",
    "def_fumbles_forced", "def_tds", "special_teams_tds",
    "fantasy_points", "fantasy_points_ppr",
)


class NotPublished(LookupError):
    """A season's file does not exist yet.

    Not an error: nflverse publishes `stats_player_week_2026` only once week 1
    has been played, so asking for the current season in August is a normal
    thing to do and must not be logged as a failure.
    """


# --- fetching -------------------------------------------------------------


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotPublished(url) from exc
        raise


def release_timestamp(tag: str) -> str | None:
    """When nflverse last rebuilt a release.

    Every release carries a tiny `timestamp.json`. Reading it costs a few bytes
    and lets the poller skip a download that would return identical data --
    which is most of them, since these files rebuild nightly at most.
    """
    try:
        payload = json.loads(_get(f"{BASE}/{tag}/timestamp.json"))
    except (NotPublished, ValueError, urllib.error.URLError) as exc:
        log.debug("no timestamp for %s: %s", tag, exc)
        return None
    return payload.get("last_updated")


def _num(value: str | None):
    """CSV cell -> number, or None.

    nflverse writes empty strings and the literal `NA` for missing values;
    float('NA') raises, and storing the string would poison every average
    computed downstream.
    """
    if value is None:
        return None
    text = value.strip()
    if not text or text in {"NA", "NaN", "nan"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else round(number, 4)


def team(abbrev: str | None) -> str | None:
    """nflverse team abbreviation in ESPN's spelling."""
    if not abbrev:
        return None
    code = abbrev.strip().upper()
    return TEAM_ALIASES.get(code, code)


def fetch_csv(tag: str, filename: str) -> list[dict]:
    """Download one gzipped CSV from a release and parse it into dicts.

    Read whole rather than streamed on purpose: the largest file we touch is
    2.5MB compressed, and a partial parse of a truncated download would look
    like a season with missing weeks rather than like a failed fetch.
    """
    raw = _get(f"{BASE}/{tag}/{filename}")
    text = gzip.decompress(raw).decode("utf-8", "replace")
    return list(csv.DictReader(io.StringIO(text)))


# --- datasets -------------------------------------------------------------


def player_ids() -> dict[int, dict]:
    """{espn_id: {gsis_id, pfr_id, name, position, team}} -- the whole join.

    ESPN fantasy player ids are our primary key everywhere; nflverse keys on
    `gsis_id` and Pro Football Reference keys on `pfr_id`. This file is the only
    thing that connects them. Roughly two thirds of its 25k rows carry an
    espn_id; the ones that do not are players ESPN's fantasy game never listed.
    """
    out: dict[int, dict] = {}
    for row in fetch_csv("players", "players.csv.gz"):
        espn_id = (row.get("espn_id") or "").strip()
        if not espn_id:
            continue
        try:
            key = int(float(espn_id))
        except ValueError:
            continue
        out[key] = {
            "gsis_id": (row.get("gsis_id") or "").strip() or None,
            "pfr_id": (row.get("pfr_id") or "").strip() or None,
            "name": (row.get("display_name") or "").strip() or None,
            "position": (row.get("position") or "").strip() or None,
            "team": team(row.get("latest_team")),
        }
    return out


def weekly_player_stats(season: int) -> list[dict]:
    """One row per player per week, trimmed to the columns we store.

    Raises NotPublished for a season nflverse has not built yet -- which is the
    normal state of the current season until its week 1 has been played.
    """
    out = []
    for row in fetch_csv("stats_player", f"stats_player_week_{season}.csv.gz"):
        gsis_id = (row.get("player_id") or "").strip()
        week = _num(row.get("week"))
        if not gsis_id or week is None:
            continue
        record = {
            "season": season,
            "week": int(week),
            "gsis_id": gsis_id,
            "name": (row.get("player_display_name") or "").strip() or None,
            "position": (row.get("position") or "").strip() or None,
            "team": team(row.get("team")),
            "opponent": team(row.get("opponent_team")),
            "season_type": (row.get("season_type") or "").strip() or None,
        }
        record.update({name: _num(row.get(name)) for name in PLAYER_WEEK_STATS})
        out.append(record)
    return out


def weekly_team_stats(season: int) -> list[dict]:
    """One row per NFL team per week -- this is what a fantasy D/ST scores on.

    Team defences never join by player id (they have no espn_id and no gsis_id),
    so they route through the team abbreviation instead.
    """
    raw = fetch_csv("stats_team", f"stats_team_week_{season}.csv.gz")
    out = []
    for row in raw:
        abbrev = team(row.get("team"))
        if not abbrev:
            continue
        record = {
            "season": season,
            "week": _num(row.get("week")),
            "team": abbrev,
            "opponent": team(row.get("opponent_team")),
            "season_type": row.get("season_type"),
        }
        record.update({name: _num(row.get(name)) for name in TEAM_WEEK_STATS})
        out.append(record)
    return out


def snap_counts(season: int) -> dict[tuple[str, int], dict]:
    """{(pfr_player_id, week): snap counts}.

    Keyed on Pro Football Reference's id, not gsis -- this release comes from
    PFR and carries no gsis column, which is why players.csv's `pfr_id` matters
    as much as its `gsis_id`.
    """
    out: dict[tuple[str, int], dict] = {}
    for row in fetch_csv("snap_counts", f"snap_counts_{season}.csv.gz"):
        pfr_id = (row.get("pfr_player_id") or "").strip()
        week = _num(row.get("week"))
        if not pfr_id or week is None:
            continue
        out[(pfr_id, int(week))] = {name: _num(row.get(name)) for name in SNAP_STATS}
    return out

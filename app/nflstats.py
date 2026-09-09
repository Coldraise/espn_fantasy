"""Analytics over nflverse NFL data.

Separate from analytics.py on purpose: that module is league math over
team_weeks, this one is NFL math over player-weeks. Mixing them would leave one
module with two unrelated row shapes and no clear contract.

Pure functions over already-joined rows -- no database handles, no network --
for the same reason analytics.py is: every number here looks plausible whether
or not it is right, so it has to be testable against a fixture.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

# Which snap column speaks for a position. A linebacker who plays every
# defensive snap reads 0% on offense_pct, so picking the wrong column would
# report the league's IDP starters as barely playing.
DEFENSIVE_POSITIONS = {"LB", "DE", "DT", "CB", "S", "DB", "DL", "EDGE", "NT", "OLB", "ILB", "MLB", "FS", "SS"}

# A "boom" is a week at 1.5x a player's own average, a "bust" at half of it.
# Self-relative rather than an absolute points threshold: 12 points is a boom
# for a kicker and a disaster for a bell-cow back, and one fixed cut-off would
# call every kicker consistent and every RB1 volatile.
BOOM_MULTIPLIER = 1.5
BUST_MULTIPLIER = 0.5


def _mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(statistics.fmean(clean), 2) if clean else None


def _total(rows: list[dict], key: str) -> float:
    return round(sum(r.get(key) or 0 for r in rows), 2)


def snap_share(rows: list[dict], position: str | None) -> float | None:
    """Mean share of their unit's snaps, read off the right side of the ball."""
    column = "defense_pct" if (position or "").upper() in DEFENSIVE_POSITIONS else "offense_pct"
    return _mean([r.get(column) for r in rows])


def player_summary(rows: list[dict], position: str | None = None,
                   recent: int = 4) -> dict:
    """Season shape for one player: volume, usage, and the spread around it.

    An average alone hides the thing managers actually care about -- whether the
    points arrive every week or in two enormous spikes -- so floor, ceiling and
    boom/bust rate travel with it.
    """
    weeks = sorted((r for r in rows if r.get("week") is not None),
                   key=lambda r: r["week"])
    played = [r for r in weeks if (r.get("offense_snaps") or r.get("defense_snaps")
                                   or r.get("fantasy_points_ppr"))]
    points = [r.get("fantasy_points_ppr") or 0.0 for r in played]

    average = _mean(points)
    booms = busts = 0
    if average:
        booms = sum(1 for p in points if p >= BOOM_MULTIPLIER * average)
        busts = sum(1 for p in points if p <= BUST_MULTIPLIER * average)

    tail = points[-recent:]
    return {
        "games": len(played),
        "ppr": _total(played, "fantasy_points_ppr"),
        "ppr_avg": average,
        "floor": round(min(points), 1) if points else None,
        "ceiling": round(max(points), 1) if points else None,
        # Population stdev, not sample: these are all the games the player
        # played, not a sample drawn from a larger set of them.
        "stdev": round(statistics.pstdev(points), 2) if len(points) > 1 else None,
        "boom_rate": round(booms / len(points), 2) if points else None,
        "bust_rate": round(busts / len(points), 2) if points else None,
        "snap_share": snap_share(played, position),
        # usage -- the columns ESPN never sends
        "targets": _total(played, "targets"),
        "target_share": _mean([r.get("target_share") for r in played]),
        "air_yards_share": _mean([r.get("air_yards_share") for r in played]),
        "wopr": _mean([r.get("wopr") for r in played]),
        "carries": _total(played, "carries"),
        "touches": _total(played, "carries") + _total(played, "receptions"),
        # IDP
        "tackles": _total(played, "def_tackles_solo") + _total(played, "def_tackle_assists"),
        "sacks": _total(played, "def_sacks"),
        "interceptions": _total(played, "def_interceptions"),
        "pass_defended": _total(played, "def_pass_defended"),
        # form
        "recent_avg": _mean(tail),
        "trend": (round(_mean(tail) - average, 2)
                  if tail and average is not None and _mean(tail) is not None else None),
    }


def team_defence_summary(rows: list[dict]) -> dict:
    """Season shape for a fantasy D/ST.

    Deliberately reports no fantasy points: the team-week file's `fantasy_points`
    is the *offence's* total, and showing it beside a defence would be a plainly
    wrong number that still looks reasonable. Sacks, takeaways and defensive
    touchdowns are what a D/ST actually scores on, so those are what it shows.
    """
    weeks = [r for r in rows if r.get("week") is not None]
    return {
        "games": len(weeks),
        "sacks": _total(weeks, "def_sacks"),
        "interceptions": _total(weeks, "def_interceptions"),
        "fumbles_forced": _total(weeks, "def_fumbles_forced"),
        "defensive_tds": _total(weeks, "def_tds") + _total(weeks, "special_teams_tds"),
        "pass_defended": _total(weeks, "def_pass_defended"),
    }


DOWN_KEYS = ("targets_d1", "targets_d2", "targets_d3", "targets_d4",
             "carries_d1", "carries_d2", "carries_d3", "carries_d4")


def down_counters(rows: list[dict]) -> dict:
    """Per-down targets and carries, summed over the weeks handed in.

    Summed rather than averaged on purpose: a count of third-down targets is a
    fact about a role, and a per-game average of it would round the difference
    between one and two third-down looks a week down to nothing.
    """
    out = {key: int(sum(r.get(key) or 0 for r in rows)) for key in DOWN_KEYS}
    out["down_targets"] = sum(out[k] for k in DOWN_KEYS if k.startswith("targets"))
    out["down_carries"] = sum(out[k] for k in DOWN_KEYS if k.startswith("carries"))
    return out


def weeks_in_range(weeks: list[int], mode: str, week: int | None = None,
                   span: int = 4) -> list[int]:
    """Which weeks a range covers, given the weeks actually on hand.

    Pure and given the available weeks rather than a season length, because
    "the last four weeks" in September is however many have been played -- a
    fixed window would silently include weeks with no rows and divide by them.
    """
    known = sorted({int(w) for w in weeks if w is not None})
    if not known:
        return []
    if mode == "season":
        return known
    end = week if week in known else known[-1]
    if mode == "week":
        return [end]
    upto = [w for w in known if w <= end]
    return upto[-span:]


def usage_rows(roster: list[dict], id_map: dict[int, dict],
               player_weeks: list[dict], team_weeks: list[dict],
               recent: int = 4, down_weeks: list[dict] | None = None) -> list[dict]:
    """Join a fantasy roster to NFL reality.

    `roster` is team_week_players rows (ESPN player ids); `id_map` is the
    espn -> gsis/pfr mapping. A player with no mapping is still returned, with
    empty stats and `matched: False` -- silently dropping them would make a
    roster look shorter than it is, which reads as a bug.

    `player_weeks` and `down_weeks` are expected to be filtered to the range
    being shown before they get here: every number below is computed over
    whatever rows it is given, which is what makes one week, four weeks and a
    whole season the same code path.
    """
    by_gsis: dict[str, list[dict]] = defaultdict(list)
    for row in player_weeks:
        by_gsis[row["gsis_id"]].append(row)
    by_team: dict[str, list[dict]] = defaultdict(list)
    for row in team_weeks:
        by_team[row["team"]].append(row)
    by_down: dict[str, list[dict]] = defaultdict(list)
    for row in down_weeks or []:
        by_down[row["gsis_id"]].append(row)

    out = []
    for entry in roster:
        espn_id = int(entry["player_id"])
        mapping = id_map.get(espn_id) or {}
        gsis_id = mapping.get("gsis_id")
        position = entry.get("position") or mapping.get("position")
        is_defence = (position or "").upper() in {"D/ST", "DST"}

        record = {
            "team_id": entry.get("team_id"),
            "player_id": espn_id,
            "name": entry.get("name") or mapping.get("name"),
            "position": position,
            "lineup_slot": entry.get("lineup_slot"),
            "pro_team": entry.get("pro_team") or mapping.get("team"),
            "is_defence": is_defence,
            "projected": entry.get("projected"),
        }
        if is_defence:
            weeks = by_team.get(record["pro_team"] or "", [])
            record["matched"] = bool(weeks)
            record |= team_defence_summary(weeks)
        else:
            weeks = by_gsis.get(gsis_id or "", [])
            record["matched"] = bool(weeks)
            record |= player_summary(weeks, position, recent=recent)
            record |= down_counters(by_down.get(gsis_id or "", []))
        out.append(record)

    out.sort(key=lambda r: -(r.get("ppr") or 0))
    return out


def franchise_usage(rows: list[dict]) -> dict[int, dict]:
    """Roll the per-player rows up to one line per fantasy franchise."""
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["team_id"]].append(row)

    out = {}
    for team_id, players in grouped.items():
        scorers = [p for p in players if not p["is_defence"] and p.get("ppr_avg")]
        out[team_id] = {
            "players": len(players),
            "matched": sum(1 for p in players if p["matched"]),
            "ppr": round(sum(p.get("ppr") or 0 for p in players), 1),
            "ppr_avg": _mean([p["ppr_avg"] for p in scorers]),
            "snap_share": _mean([p["snap_share"] for p in players
                                 if p.get("snap_share") is not None]),
            "boom_rate": _mean([p["boom_rate"] for p in scorers
                                if p.get("boom_rate") is not None]),
            "volatility": _mean([p["stdev"] for p in scorers
                                 if p.get("stdev") is not None]),
        }
    return out


def split_by_unit(rows: list[dict]) -> dict[str, list[dict]]:
    """Offence, IDP and team defence rendered apart.

    One table cannot serve all three: target share is meaningless for a
    linebacker, tackles are meaningless for a receiver, and a D/ST has neither.
    Splitting them keeps every column meaningful for every row under it.
    """
    groups: dict[str, list[dict]] = {"offence": [], "idp": [], "dst": []}
    for row in rows:
        if row["is_defence"]:
            groups["dst"].append(row)
        elif (row.get("position") or "").upper() in DEFENSIVE_POSITIONS:
            groups["idp"].append(row)
        else:
            groups["offence"].append(row)
    return groups

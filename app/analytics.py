"""Derived league analytics.

Pure functions over team_weeks rows. No network, no database handles -- which is
what makes this module directly testable, and it needs to be: a bug here is
invisible in the UI because every number still *looks* plausible.

ESPN precomputes none of this. It gives a schedule and weekly scores; everything
that makes rivalries legible has to be derived.

Row shape: {season, week, team_id, points, projected, opponent_id, is_playoff, result}
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from itertools import combinations

Row = dict


def _played(row: Row) -> bool:
    """A real, completed matchup: an opponent and a settled result."""
    return row.get("opponent_id") is not None and row.get("result") in {"W", "L", "T"}


def _scored(row: Row) -> bool:
    return row.get("points") is not None


def complete_weeks(rows: list[Row]) -> set[tuple[int, int]]:
    """(season, week) pairs where every matchup has settled.

    A week still in progress has real scores but no results yet. Counting it in
    all-play would rank teams that have played against teams that have not, so
    the standings would swing wildly mid-Sunday and then settle. All-play and
    luck are season-long records; they only count finished weeks.
    """
    weeks = {(r["season"], r["week"]) for r in rows}
    in_progress = {
        (r["season"], r["week"])
        for r in rows
        if r.get("opponent_id") is not None and r.get("result") is None
    }
    return weeks - in_progress


def points_index(rows: list[Row]) -> dict[tuple[int, int, int], float]:
    """(season, week, team_id) -> points, for opponent lookups."""
    return {
        (r["season"], r["week"], r["team_id"]): r["points"]
        for r in rows
        if _scored(r)
    }


# --- head to head ---------------------------------------------------------


def head_to_head(rows: list[Row]) -> dict[int, dict[int, dict[str, int]]]:
    """All-time W/L/T between every pair of teams.

    Each matchup appears twice in the data (once per side), so we read each row
    from its own team's perspective and never double count.
    """
    matrix: dict[int, dict[int, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"W": 0, "L": 0, "T": 0})
    )
    for row in rows:
        if not _played(row):
            continue
        matrix[row["team_id"]][row["opponent_id"]][row["result"]] += 1
    return {team: dict(opps) for team, opps in matrix.items()}


# --- all-play -------------------------------------------------------------


def all_play(rows: list[Row], regular_season_only: bool = True) -> dict[int, dict[str, float]]:
    """Record if every team played every other team every week.

    The schedule-independent truth: strips out who you happened to be matched
    against. Playoff weeks are excluded by default -- only a subset of teams
    play, so including them would compare those teams against a thin field.
    Weeks still in progress are excluded too -- see complete_weeks().
    """
    finished = complete_weeks(rows)
    per_week: dict[tuple[int, int], list[tuple[int, float]]] = defaultdict(list)
    for row in rows:
        if not _scored(row):
            continue
        if regular_season_only and row.get("is_playoff"):
            continue
        if (row["season"], row["week"]) not in finished:
            continue
        per_week[(row["season"], row["week"])].append((row["team_id"], row["points"]))

    tally: dict[int, dict[str, float]] = defaultdict(lambda: {"W": 0, "L": 0, "T": 0})
    for entries in per_week.values():
        if len(entries) < 2:
            continue
        for team_id, points in entries:
            for other_id, other_points in entries:
                if other_id == team_id:
                    continue
                if points > other_points:
                    tally[team_id]["W"] += 1
                elif points < other_points:
                    tally[team_id]["L"] += 1
                else:
                    tally[team_id]["T"] += 1

    for record in tally.values():
        total = record["W"] + record["L"] + record["T"]
        record["pct"] = (record["W"] + 0.5 * record["T"]) / total if total else 0.0
    return dict(tally)


def luck(rows: list[Row], regular_season_only: bool = True) -> dict[int, dict[str, float]]:
    """Actual wins minus all-play expected wins.

    Positive means the schedule flattered you: you won more than your scoring
    deserved. Negative means you scored well and lost anyway.
    """
    ap = all_play(rows, regular_season_only=regular_season_only)

    actual: dict[int, dict[str, float]] = defaultdict(lambda: {"W": 0, "L": 0, "T": 0})
    for row in rows:
        if not _played(row):
            continue
        if regular_season_only and row.get("is_playoff"):
            continue
        actual[row["team_id"]][row["result"]] += 1

    out: dict[int, dict[str, float]] = {}
    for team_id, record in actual.items():
        games = record["W"] + record["L"] + record["T"]
        pct = ap.get(team_id, {}).get("pct", 0.0)
        expected = pct * games
        out[team_id] = {
            "actual_wins": record["W"],
            "losses": record["L"],
            "ties": record["T"],
            "games": games,
            "expected_wins": round(expected, 2),
            "luck": round(record["W"] - expected, 2),
            "all_play_pct": round(pct, 4),
        }
    return out


# --- form, projections, consistency ----------------------------------------


def consistency(rows: list[Row], regular_season_only: bool = True) -> dict[int, dict]:
    """Spread of a team's weekly scores: floor, ceiling and how far they roam.

    Two teams can average the same and be nothing alike -- one wins the weeks it
    was going to win, the other is a coin flip every Sunday. Average alone hides
    exactly the thing that decides a season.
    """
    by_team: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        if not _scored(row) or not _played(row):
            continue
        if regular_season_only and row.get("is_playoff"):
            continue
        by_team[row["team_id"]].append(row["points"] or 0.0)

    out = {}
    for team_id, points in by_team.items():
        out[team_id] = {
            "weeks": len(points),
            "avg": round(statistics.fmean(points), 2),
            "floor": round(min(points), 1),
            "ceiling": round(max(points), 1),
            # Population, not sample: these are all the weeks played, not a
            # draw from a larger set of them.
            "stdev": round(statistics.pstdev(points), 2) if len(points) > 1 else 0.0,
        }
    return out


def projection_accuracy(rows: list[Row], regular_season_only: bool = True) -> dict[int, dict]:
    """How wrong ESPN's projection was, per team, and in which direction.

    `team_weeks.projected` has been stored since the first poll and never shown.
    Positive bias means the team beat its projection on average.
    """
    by_team: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        if not _scored(row) or not _played(row) or not row.get("projected"):
            continue
        if regular_season_only and row.get("is_playoff"):
            continue
        by_team[row["team_id"]].append((row["points"] or 0.0, row["projected"]))

    out = {}
    for team_id, pairs in by_team.items():
        errors = [actual - projected for actual, projected in pairs]
        out[team_id] = {
            "weeks": len(pairs),
            "bias": round(statistics.fmean(errors), 2),
            # Mean absolute error, so overshoots and undershoots cannot cancel
            # into a flattering zero.
            "error": round(statistics.fmean([abs(e) for e in errors]), 2),
            "beat": sum(1 for e in errors if e > 0),
        }
    return out


def power_rankings(rows: list[Row], recent: int = 3,
                   regular_season_only: bool = True) -> list[dict]:
    """Rank on scoring, not on record.

    Blends all-play win pct (schedule-independent), scoring rate, and recent
    form. Weighted 50/30/20 -- all-play carries the most because it is the only
    component a soft schedule cannot inflate.
    """
    ap = all_play(rows, regular_season_only=regular_season_only)
    summary = team_summary(rows, regular_season_only=regular_season_only)
    if not summary:
        return []

    by_team: dict[int, list[Row]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: (r["season"], r["week"])):
        if _scored(row) and _played(row) and not (regular_season_only and row.get("is_playoff")):
            by_team[row["team_id"]].append(row)

    averages = [e["avg"] for e in summary.values() if e["avg"]]
    best_avg = max(averages) if averages else 0.0

    table = []
    for team_id, entry in summary.items():
        history = by_team[team_id]
        tail = [r["points"] or 0.0 for r in history[-recent:]]
        recent_avg = round(statistics.fmean(tail), 2) if tail else 0.0
        score = (
            0.50 * ap.get(team_id, {}).get("pct", 0.0)
            + 0.30 * (entry["avg"] / best_avg if best_avg else 0.0)
            + 0.20 * (recent_avg / best_avg if best_avg else 0.0)
        )
        table.append({
            "team_id": team_id,
            "wins": entry["W"], "losses": entry["L"], "ties": entry["T"],
            "avg": entry["avg"], "recent_avg": recent_avg,
            "all_play_pct": round(ap.get(team_id, {}).get("pct", 0.0), 3),
            "power": round(100 * score, 1),
        })
    table.sort(key=lambda t: -t["power"])
    for position, entry in enumerate(table, start=1):
        entry["rank"] = position
    return table


def points_left_on_bench(lineups: dict[int, list[dict]],
                         slots: list[tuple[str, int]]) -> dict[int, dict]:
    """What each team would have scored playing its best legal lineup.

    `lineups` is one week of team_week_players rows including bench, `slots` is
    the league's starting requirement from rules.split_slots -- e.g.
    [("QB", 1), ("RB", 2), ...].

    Greedy by slot scarcity, not a full optimisation: slots are filled in the
    order given, each taking the best player still available who is eligible for
    it. For a fixed lineup like this one that matches what a manager could
    actually have set, and it cannot invent a lineup the rules forbid.
    """
    out: dict[int, dict] = {}
    for team_id, players in lineups.items():
        available = sorted(
            (p for p in players if p.get("actual") is not None),
            key=lambda p: -(p.get("actual") or 0.0),
        )
        if not available:
            continue
        actual = round(sum(p["actual"] for p in players
                           if p.get("is_starter") and p.get("actual") is not None), 2)

        used: set = set()
        optimal = 0.0
        for slot, count in slots:
            eligible = [p for p in available
                        if p["player_id"] not in used and _fills(p, slot)]
            for person in eligible[:count]:
                used.add(person["player_id"])
                optimal += person["actual"] or 0.0
        optimal = round(optimal, 2)
        out[team_id] = {
            "actual": actual,
            "optimal": optimal,
            "left_on_bench": round(optimal - actual, 2),
            # 1.0 means a perfect lineup call; this is the number that settles
            # arguments about who is actually good at setting a lineup.
            "efficiency": round(actual / optimal, 3) if optimal else None,
        }
    return out


# Which positions may fill which starting slot. Flex slots list every position
# they accept; a slot we do not recognise accepts only its own name.
_SLOT_ELIGIBILITY = {
    "RB/WR": {"RB", "WR"},
    "WR/TE": {"WR", "TE"},
    "RB/WR/TE": {"RB", "WR", "TE"},
    "FLEX": {"RB", "WR", "TE"},
    "OP": {"QB", "RB", "WR", "TE"},
    "DL": {"DE", "DT", "DL"},
    "DB": {"CB", "S", "DB"},
    "DP": {"DE", "DT", "DL", "LB", "CB", "S", "DB"},
}


def _fills(player: dict, slot: str) -> bool:
    position = (player.get("position") or "").upper()
    return position in _SLOT_ELIGIBILITY.get(slot.upper(), {slot.upper()})


# --- summary --------------------------------------------------------------


def team_summary(rows: list[Row], regular_season_only: bool = True) -> dict[int, dict]:
    """Per-team record, points for/against, extremes, and current streak."""
    index = points_index(rows)
    relevant = [
        r for r in rows
        if _played(r) and not (regular_season_only and r.get("is_playoff"))
    ]

    stats: dict[int, dict] = defaultdict(
        lambda: {"W": 0, "L": 0, "T": 0, "pf": 0.0, "pa": 0.0,
                 "best": None, "worst": None, "weeks": 0}
    )

    for row in relevant:
        team = row["team_id"]
        entry = stats[team]
        entry[row["result"]] += 1
        points = row["points"] or 0.0
        entry["pf"] += points
        entry["pa"] += index.get((row["season"], row["week"], row["opponent_id"]), 0.0) or 0.0
        entry["weeks"] += 1
        stamp = {"season": row["season"], "week": row["week"], "points": points}
        if entry["best"] is None or points > entry["best"]["points"]:
            entry["best"] = stamp
        if entry["worst"] is None or points < entry["worst"]["points"]:
            entry["worst"] = stamp

    by_team: dict[int, list[Row]] = defaultdict(list)
    for row in sorted(relevant, key=lambda r: (r["season"], r["week"])):
        by_team[row["team_id"]].append(row)

    for team, entry in stats.items():
        entry["pf"] = round(entry["pf"], 2)
        entry["pa"] = round(entry["pa"], 2)
        entry["avg"] = round(entry["pf"] / entry["weeks"], 2) if entry["weeks"] else 0.0
        history = by_team[team]
        streak = 0
        kind = history[-1]["result"] if history else None
        for row in reversed(history):
            if row["result"] != kind:
                break
            streak += 1
        entry["streak"] = f"{kind}{streak}" if kind else "-"
    return dict(stats)


def standings_table(rows: list[Row], names: dict[int, str], regular_season_only: bool = True) -> list[dict]:
    """Joined view: real record beside all-play and luck, sorted by real wins."""
    summary = team_summary(rows, regular_season_only)
    luck_map = luck(rows, regular_season_only)
    ap = all_play(rows, regular_season_only)

    table = []
    for team_id, entry in summary.items():
        luck_entry = luck_map.get(team_id, {})
        ap_entry = ap.get(team_id, {})
        table.append({
            "team_id": team_id,
            "name": names.get(team_id, f"Team {team_id}"),
            "wins": entry["W"], "losses": entry["L"], "ties": entry["T"],
            "pf": entry["pf"], "pa": entry["pa"], "avg": entry["avg"],
            "streak": entry["streak"], "best": entry["best"], "worst": entry["worst"],
            "all_play_w": int(ap_entry.get("W", 0)),
            "all_play_l": int(ap_entry.get("L", 0)),
            "all_play_pct": round(ap_entry.get("pct", 0.0), 3),
            "expected_wins": luck_entry.get("expected_wins", 0.0),
            "luck": luck_entry.get("luck", 0.0),
        })
    table.sort(key=lambda t: (-t["wins"], -t["pf"]))
    return table


# --- draft ----------------------------------------------------------------


def draft_value(picks: list[dict], points: dict[int, float],
                positions: dict[int, str] | None = None) -> list[dict]:
    """Which picks paid off, measured against where they went.

    Value is points scored minus the points the average pick at that slot
    returned. Comparing a first-rounder to a fifteenth-rounder on raw points
    would only ever rediscover that early picks score more; the baseline is the
    league's own draft, so a "steal" means beating the room, not beating a
    number someone published in August.
    """
    positions = positions or {}
    scored = [p for p in picks if p.get("player_id") in points]
    if not scored:
        return []

    # Baseline per round, from this draft. With eight teams a round is eight
    # picks -- enough to be a mean, not enough to be a smooth curve, which is
    # why this is a round baseline and not a per-slot one.
    by_round: dict[int, list[float]] = defaultdict(list)
    for pick in scored:
        by_round[pick.get("round") or 0].append(points[pick["player_id"]])
    baseline = {rnd: sum(vals) / len(vals) for rnd, vals in by_round.items()}

    out = []
    for pick in scored:
        earned = points[pick["player_id"]]
        expected = baseline.get(pick.get("round") or 0, 0.0)
        out.append({
            "overall_pick": pick.get("overall_pick"),
            "round": pick.get("round"),
            "round_pick": pick.get("round_pick"),
            "team_id": pick.get("team_id"),
            "player_id": pick.get("player_id"),
            "player_name": pick.get("player_name"),
            "position": positions.get(pick.get("player_id")),
            "keeper": bool(pick.get("keeper")),
            "bid_amount": pick.get("bid_amount"),
            "points": round(earned, 1),
            "expected": round(expected, 1),
            "value": round(earned - expected, 1),
        })
    out.sort(key=lambda p: -p["value"])
    return out


def draft_value_by_team(rows: list[dict]) -> list[dict]:
    """Roll pick-level value up to who drafted best."""
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["team_id"]].append(row)

    table = []
    for team_id, picks in grouped.items():
        ordered = sorted(picks, key=lambda p: -p["value"])
        table.append({
            "team_id": team_id,
            "picks": len(picks),
            "points": round(sum(p["points"] for p in picks), 1),
            "value": round(sum(p["value"] for p in picks), 1),
            "best": ordered[0],
            "worst": ordered[-1],
        })
    table.sort(key=lambda t: -t["value"])
    return table


def positional_runs(picks: list[dict], positions: dict[int, str],
                    length: int = 3) -> list[dict]:
    """Stretches where the room piled into one position.

    A run is `length` or more consecutive picks at the same position -- the
    moment somebody panicked and everyone followed.
    """
    ordered = sorted((p for p in picks if p.get("overall_pick")),
                     key=lambda p: p["overall_pick"])
    runs, current = [], []

    def close():
        if len(current) >= length:
            runs.append({
                "position": positions.get(current[0]["player_id"]),
                "length": len(current),
                "start": current[0]["overall_pick"],
                "end": current[-1]["overall_pick"],
                "players": [p.get("player_name") for p in current],
            })

    for pick in ordered:
        position = positions.get(pick.get("player_id"))
        if not position:
            close()
            current = []
            continue
        if current and positions.get(current[0]["player_id"]) == position:
            current.append(pick)
        else:
            close()
            current = [pick]
    close()
    return runs


# --- all-time, from season totals ----------------------------------------
#
# ESPN serves 2019-2025 as season totals only -- no weekly schedule is
# retrievable -- so these functions work on team_seasons rows rather than
# team_weeks. Everything above (head-to-head, all-play, luck)
# needs per-week opponents and is therefore 2026-onward only.


def has_week_data(rows: list[Row]) -> bool:
    """Whether any real game has been played, so routes can say so honestly
    instead of rendering an empty grid."""
    return any(_scored(r) and (r["points"] or 0) > 0 for r in rows)


def filter_franchise_history(season_rows: list[dict],
                            since: dict[int, int] | None = None) -> list[dict]:
    """Drop seasons a franchise played under a previous owner.

    Applied at the read boundary so all-time totals, the season grid, the
    name history and the hover records all agree on one view of the past.
    """
    if not since:
        return season_rows
    return [
        row for row in season_rows
        if int(row["season"]) >= since.get(int(row["team_id"]), 0)
    ]


def all_time_standings(
    season_rows: list[dict],
    names: dict[int, dict] | None = None,
) -> list[dict]:
    """Aggregate franchise records across every stored season.

    Keys on team_id, not name: franchise ids are stable across seasons but names
    are not -- several teams here have renamed at least once -- so aggregating by
    name would split one franchise into two.
    """
    names = names or {}
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in season_rows:
        if row.get("wins") is None:
            continue
        grouped[int(row["team_id"])].append(row)

    table = []
    for team_id, rows in grouped.items():
        wins = sum(r["wins"] or 0 for r in rows)
        losses = sum(r["losses"] or 0 for r in rows)
        ties = sum(r["ties"] or 0 for r in rows)
        games = wins + losses + ties
        pf = sum(r["points_for"] or 0.0 for r in rows)
        pa = sum(r["points_against"] or 0.0 for r in rows)
        ranked = [r for r in rows if r.get("final_rank")]
        best = min(ranked, key=lambda r: r["final_rank"], default=None)
        worst = max(ranked, key=lambda r: r["final_rank"], default=None)
        label = names.get(team_id, {})

        table.append({
            "team_id": team_id,
            "name": label.get("name") or (rows[-1].get("name") or f"Team {team_id}"),
            "former_names": label.get("former") or [],
            "seasons": len(rows),
            "first_season": min(r["season"] for r in rows),
            "last_season": max(r["season"] for r in rows),
            "wins": wins, "losses": losses, "ties": ties,
            "pct": round((wins + 0.5 * ties) / games, 3) if games else 0.0,
            "pf": round(pf, 1), "pa": round(pa, 1),
            "diff": round(pf - pa, 1),
            "pf_per_season": round(pf / len(rows), 1) if rows else 0.0,
            "titles": sum(1 for r in ranked if r["final_rank"] == 1),
            "runner_ups": sum(1 for r in ranked if r["final_rank"] == 2),
            "playoff_appearances": sum(1 for r in rows if (r.get("playoff_seed") or 0) > 0),
            "avg_finish": round(sum(r["final_rank"] for r in ranked) / len(ranked), 2) if ranked else None,
            "best_finish": best["final_rank"] if best else None,
            "best_finish_season": best["season"] if best else None,
            "worst_finish": worst["final_rank"] if worst else None,
        })

    table.sort(key=lambda t: (-t["pct"], -t["wins"]))
    return table


def season_grid(season_rows: list[dict], names: dict[int, dict] | None = None) -> dict:
    """Season-by-season finishes: {'seasons': [...], 'rows': [{team, by_season}]}."""
    names = names or {}
    seasons = sorted({int(r["season"]) for r in season_rows}, reverse=True)
    grouped: dict[int, dict[int, dict]] = defaultdict(dict)
    for row in season_rows:
        grouped[int(row["team_id"])][int(row["season"])] = row

    rows = []
    for team_id, by_season in grouped.items():
        latest = by_season[max(by_season)]
        rows.append({
            "team_id": team_id,
            "name": names.get(team_id, {}).get("name") or latest.get("name") or f"Team {team_id}",
            "by_season": {
                s: {
                    "record": f"{d['wins']}-{d['losses']}" + (f"-{d['ties']}" if d.get("ties") else ""),
                    "rank": d.get("final_rank"),
                    "seed": d.get("playoff_seed"),
                    "pf": round(d.get("points_for") or 0.0, 1),
                }
                for s, d in by_season.items()
            },
        })
    rows.sort(key=lambda r: r["name"].lower())
    return {"seasons": seasons, "rows": rows}

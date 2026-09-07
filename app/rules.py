"""League settings rendered as human-readable, searchable rules.

ESPN's mSettings payload is a pile of enum strings, epoch milliseconds, slot-id
maps and -1 sentinels. This turns it into labelled rows a person can read and
filter, grouped by topic. Pure functions over the raw payload so it can be
tested without a network call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

try:  # pragma: no cover - depends on installed espn-api
    from espn_api.football.constant import POSITION_MAP, SETTINGS_SCORING_FORMAT_MAP
except ImportError:  # pragma: no cover
    POSITION_MAP, SETTINGS_SCORING_FORMAT_MAP = {}, {}

UNLIMITED = -1

_ENUM_LABELS = {
    "H2H_POINTS": "Head-to-head, points",
    "H2H_RECORD": "Head-to-head record",
    "WAIVERS_TRADITIONAL": "Traditional waivers",
    "WAIVERS_CONTINUAL": "Continual rolling waivers",
    "FREE_AGENT_AUCTION": "Free agent auction (FAAB)",
    "INDIVIDUAL_GAME": "At each player's kickoff",
    "FIRST_GAME_OF_WEEK": "At the first game of the week",
    "SNAKE": "Snake",
    "AUCTION": "Auction",
    "OFFLINE": "Offline",
    "TRADITIONAL": "Traditional",
    "DRAFT_START": "Set at draft start",
    "NONE": "None",
    "PPR": "PPR",
    "STANDARD": "Standard",
}


def _label(value):
    if isinstance(value, str):
        return _ENUM_LABELS.get(value, value.replace("_", " ").title())
    return value


def _yesno(value) -> str:
    return "Yes" if value else "No"


def _limit(value, unit: str = "") -> str:
    if value in (None, UNLIMITED) or (isinstance(value, float) and value == -1.0):
        return "Unlimited"
    return f"{value}{(' ' + unit) if unit else ''}"


def _when(ms, tz_name: str) -> str | None:
    if not ms:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz).strftime(
        "%a %d %b %Y, %H:%M"
    )


def _points(value: float) -> str:
    """Trim trailing zeros: 6.0 -> 6, 0.5 stays 0.5, -2.0 -> -2."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-") else "0"


# Slots that hold players but never score: bench and injured reserve.
NON_SCORING_SLOTS = {"BE", "IR", "RES"}


def roster_slots(settings: dict) -> list[tuple[str, int]]:
    """Every used lineup slot, in slot order, including bench and IR."""
    counts = (settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}
    out = []
    for slot_id, count in sorted(counts.items(), key=lambda kv: int(kv[0])):
        if not count:
            continue
        name = POSITION_MAP.get(int(slot_id), f"Slot {slot_id}")
        out.append((name, int(count)))
    return out


def split_slots(settings: dict) -> tuple[list[tuple[str, int]], dict[str, int]]:
    """(starting slots, {bench/IR slot: count}).

    Bench and IR are roster capacity, not lineup spots -- counting them as
    starters would claim a 30-man starting lineup for a 16-man one.
    """
    starters, reserves = [], {}
    for name, count in roster_slots(settings):
        if name in NON_SCORING_SLOTS:
            reserves[name] = count
        else:
            starters.append((name, count))
    return starters, reserves


def scoring_rules(settings: dict) -> list[dict]:
    """Every scoring item with a non-zero value, labelled."""
    items = (settings.get("scoringSettings") or {}).get("scoringItems") or []
    rows = []
    for item in items:
        stat_id = item.get("statId")
        points = item.get("pointsOverrides", {}).get("16")
        if points is None:
            points = item.get("points", 0)
        if not points:
            continue
        meta = SETTINGS_SCORING_FORMAT_MAP.get(stat_id, {})
        rows.append({
            "label": meta.get("label") or f"Stat {stat_id}",
            "abbr": meta.get("abbr") or "",
            "value": _points(points),
            "points": points,
        })
    rows.sort(key=lambda r: (-abs(r["points"]), r["label"]))
    return rows


def build(settings: dict, tz_name: str = "UTC") -> list[dict]:
    """Group every setting into categories of {label, value} rows."""
    schedule = settings.get("scheduleSettings") or {}
    roster = settings.get("rosterSettings") or {}
    scoring = settings.get("scoringSettings") or {}
    acq = settings.get("acquisitionSettings") or {}
    trade = settings.get("tradeSettings") or {}
    draft = settings.get("draftSettings") or {}
    finance = settings.get("financeSettings") or {}

    slots, reserves = split_slots(settings)
    starters = sum(count for _, count in slots)

    categories: list[dict] = []

    def add(title: str, rows: list[tuple]):
        kept = [
            {"label": label, "value": value, "hint": (rest[0] if rest else None)}
            for label, value, *rest in rows
            if value not in (None, "")
        ]
        if kept:
            categories.append({"title": title, "rows": kept})

    add("Format", [
        ("League name", settings.get("name")),
        ("Teams", settings.get("size")),
        ("Scoring type", _label(scoring.get("scoringType"))),
        ("Player ranking", _label(scoring.get("playerRankType")),
         "Which ranking ESPN shows by default"),
        ("Public league", _yesno(settings.get("isPublic"))),
        ("Divisions", ", ".join(d.get("name", "?") for d in schedule.get("divisions") or [])),
    ])

    add("Roster", [
        ("Starting lineup", ", ".join(f"{count}×{name}" for name, count in slots)),
        ("Starters", starters),
        ("Bench", "Unlimited" if roster.get("isBenchUnlimited")
                  else reserves.get("BE") or roster.get("benchSize")),
        ("Injured reserve", reserves.get("IR")),
        ("Lineup locks", _label(roster.get("lineupLocktimeType"))),
        ("Undroppable list", _yesno(roster.get("isUsingUndroppableList"))),
        ("Roster moves per week", _limit(roster.get("moveLimit"))),
    ])

    add("Schedule & playoffs", [
        ("Regular season", f"{schedule.get('matchupPeriodCount')} weeks"),
        ("Matchup length", f"{schedule.get('matchupPeriodLength')} week(s)"),
        ("Playoff teams", schedule.get("playoffTeamCount")),
        ("Playoff round length", f"{schedule.get('playoffMatchupPeriodLength')} week(s)"),
        ("Playoff seeding", _label(schedule.get("playoffSeedingRule"))),
        ("Reseed each round", _yesno(schedule.get("playoffReseed"))),
        ("Regular season ties", _label(scoring.get("matchupTieRule"))),
        ("Playoff ties", _label(scoring.get("playoffMatchupTieRule"))),
        ("Home team bonus", _points(scoring.get("homeTeamBonus") or 0)),
    ])

    add("Waivers & free agency", [
        ("Acquisition type", _label(acq.get("acquisitionType"))),
        ("Waiver period", f"{acq.get('waiverHours')} hours"),
        ("Waivers process", ", ".join(
            d.title() for d in acq.get("waiverProcessDays") or []) or None,
         f"at {acq.get('waiverProcessHour')}:00" if acq.get("waiverProcessHour") is not None else None),
        ("Waiver order resets weekly", _yesno(acq.get("waiverOrderReset"))),
        ("Uses FAAB budget", _yesno(acq.get("isUsingAcquisitionBudget"))),
        ("FAAB budget", acq.get("acquisitionBudget") if acq.get("isUsingAcquisitionBudget") else None),
        ("Minimum bid", acq.get("minimumBid") if acq.get("isUsingAcquisitionBudget") else None),
        ("Acquisitions per season", _limit(acq.get("acquisitionLimit"))),
        ("Acquisitions per week", _limit(acq.get("matchupAcquisitionLimit"))),
    ])

    add("Trades", [
        ("Trade deadline", _when(trade.get("deadlineDate"), tz_name)),
        ("Veto votes required", trade.get("vetoVotesRequired")),
        ("Review period", f"{trade.get('revisionHours')} hours"),
        ("Trades per season", _limit(trade.get("max"))),
    ])

    add("Draft", [
        ("Type", _label(draft.get("type"))),
        ("Date", _when(draft.get("date"), tz_name)),
        ("Time per pick", f"{draft.get('timePerSelection')} seconds"
                          if draft.get("timePerSelection") else None),
        ("Keepers", draft.get("keeperCount") or "None"),
        ("Pick order", ", ".join(str(p) for p in draft.get("pickOrder") or []) or None,
         "Team ids in first-round order"),
        ("Trading during draft", _yesno(draft.get("isTradingEnabled"))),
        ("Auction budget", draft.get("auctionBudget") if draft.get("type") == "AUCTION" else None),
    ])

    fees = [(k, v) for k, v in finance.items() if v]
    if fees:
        add("Fees", [(k.replace("per", "Per ").replace("player", "Player ").title(), v)
                     for k, v in fees])

    scoring_rows = scoring_rules(settings)
    if scoring_rows:
        categories.append({
            "title": "Scoring",
            "rows": [{"label": r["label"], "value": r["value"], "hint": r["abbr"]}
                     for r in scoring_rows],
        })

    return categories


def flatten(categories: list[dict]) -> list[dict]:
    """One flat searchable list -- category, label, value, and a search blob."""
    out = []
    for cat in categories:
        for row in cat["rows"]:
            out.append({
                "category": cat["title"],
                "label": row["label"],
                "value": row["value"],
                "hint": row.get("hint"),
                "search": " ".join(
                    str(x).lower() for x in (cat["title"], row["label"], row["value"], row.get("hint")) if x
                ),
            })
    return out

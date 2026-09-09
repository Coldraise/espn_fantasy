"""Player positions and grouping.

Two ESPN traps are handled here.

`defaultPositionId` and `POSITION_MAP` are different enums: POSITION_MAP is a
map of *lineup slots*, so running a player's defaultPositionId through it puts
tight ends in the WR bucket and leaves QB, TE and K empty. The map below was
derived empirically against 250 players whose positions espn-api had already
resolved.

Player names are not unique -- there is a linebacker named Justin Jefferson and
a cornerback named Lamar Jackson -- so every lookup keys on player_id. Keying on
name silently merges two different people.
"""

from __future__ import annotations

from collections import defaultdict

# ESPN defaultPositionId -> position. NOT interchangeable with POSITION_MAP.
DEFAULT_POSITION_MAP = {
    1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K",
    9: "DT", 10: "DE", 11: "LB", 12: "CB", 13: "S",
    16: "D/ST",
}

# The lineup slots this league actually starts, and which positions fill them.
POSITION_GROUPS: list[tuple[str, list[str]]] = [
    ("QB", ["QB"]),
    ("RB", ["RB"]),
    ("WR", ["WR"]),
    ("TE", ["TE"]),
    ("K", ["K"]),
    ("D/ST", ["D/ST"]),
    ("LB", ["LB"]),
    ("DL", ["DE", "DT"]),
    ("DB", ["CB", "S"]),
]

GROUP_OF = {pos: label for label, positions in POSITION_GROUPS for pos in positions}

# NFL team colours, keyed by the abbreviation ESPN uses in `players.pro_team`.
# Each team's primary identity colour, used only as a tint behind a lineup row,
# so the pairing that matters is against the page background rather than against
# each other -- a few teams (LAR/LAC, NYG/NYJ) are near-identical on the field
# and no palette can separate them here either. The abbreviations are ESPN's,
# not the NFL's: WSH not WAS, and JAX not JAC.
PRO_TEAM_COLORS = {
    "ARI": "#97233F", "ATL": "#A71930", "BAL": "#241773", "BUF": "#00338D",
    "CAR": "#0085CA", "CHI": "#0B162A", "CIN": "#FB4F14", "CLE": "#FF3C00",
    "DAL": "#041E42", "DEN": "#FB4F14", "DET": "#0076B6", "GB":  "#203731",
    "HOU": "#03202F", "IND": "#002C5F", "JAX": "#006778", "KC":  "#E31837",
    "LAC": "#0080C6", "LAR": "#003594", "LV":  "#000000", "MIA": "#008E97",
    "MIN": "#4F2683", "NE":  "#002244", "NO":  "#D3BC8D", "NYG": "#0B2265",
    "NYJ": "#125740", "PHI": "#004C54", "PIT": "#FFB612", "SEA": "#002244",
    "SF":  "#AA0000", "TB":  "#D50A0A", "TEN": "#4B92DB", "WSH": "#5A1414",
}


def team_color(abbrev: str | None) -> str | None:
    return PRO_TEAM_COLORS.get((abbrev or "").upper()) or None



def position_for(default_position_id: int | None) -> str | None:
    return DEFAULT_POSITION_MAP.get(default_position_id)


def group_for(position: str | None) -> str | None:
    return GROUP_OF.get(position or "")


def group_by_position(rows: list[dict], per_group: int = 60,
                      available_only: bool = False) -> list[dict]:
    """Rank players into league lineup groups, best projection first.

    Returns [{label, players: [...]}] in POSITION_GROUPS order, skipping groups
    with nobody in them.
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if available_only and (row.get("on_team_id") or 0):
            continue
        label = group_for(row.get("position"))
        if label is None:
            continue
        buckets[label].append(row)

    out = []
    for label, _ in POSITION_GROUPS:
        players = buckets.get(label)
        if not players:
            continue
        players = sorted(players, key=lambda r: -(r.get("projected") or 0))
        out.append({"label": label, "players": players[:per_group],
                    "total": len(players)})
    return out


def leaders(rows: list[dict], per: int = 3, by: str = "projected") -> list[dict]:
    """The top `per` players in each lineup group, best first.

    `by` is "actual" once a week has been played and "projected" before that.
    Ranking a played week on projections would list the players we expected to
    do well rather than the ones who did.
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        label = group_for(row.get("position"))
        if label is None:
            continue
        buckets[label].append(row)

    out = []
    for label, _ in POSITION_GROUPS:
        got = buckets.get(label)
        if not got:
            continue
        got = sorted(got, key=lambda r: -(r.get(by) or 0))
        out.append({
            "label": label,
            "players": [{"name": p.get("name"), "pro_team": p.get("pro_team"),
                         "value": p.get(by) or 0} for p in got[:per]],
        })
    return out


# Generational suffixes are not surnames. Without stripping these first,
# "Kenneth Walker III" initials to "Kenneth I."
_NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}


def short_name(name: str | None) -> str:
    """"Derrick Henry" -> "Derrick H.", for rows too narrow for the full name.

    Team defences are returned untouched. ESPN names them "Titans D/ST", which
    the general rule would happily turn into "Titans D." -- plausible enough on
    screen that it would never be reported as a bug, and wrong.

    Compound surnames are a deliberate simplification: "Amon-Ra St. Brown"
    becomes "Amon-Ra B." rather than "Amon-Ra St. B.". Encoding which multi-word
    surnames are really one name is a bigger job than this line of a lineup row
    is worth.
    """
    full = (name or "").strip()
    if not full or full.upper().endswith("D/ST"):
        return full

    parts = full.split()
    while len(parts) > 1 and parts[-1].lower().rstrip(",") in _NAME_SUFFIXES:
        parts.pop()
    if len(parts) < 2:
        return full
    return f"{parts[0]} {parts[-1][0].upper()}."


def ranked(rows: list[dict], by: str = "projected") -> list[dict]:
    """Every player in one list, best first, tagged with its lineup group.

    The by-position grouping answers "who is the best tight end". This answers
    "who is the best player", which is a different question and not obtainable
    by concatenating the groups -- their order is per-group, so the top kicker
    would sit above the second-best running back.
    """
    out = []
    for row in rows:
        label = group_for(row.get("position"))
        if label is None:
            continue
        out.append({**row, "group": label})
    out.sort(key=lambda r: -(r.get(by) or 0))
    return out


# Lineup slots in the order a box score reads them. Anything ESPN sends that is
# not here sorts to the end rather than being dropped -- an unknown slot is
# still someone's starter.
SLOT_ORDER = [
    "QB", "RB", "WR", "TE", "RB/WR", "RB/WR/TE", "WR/TE", "OP", "FLEX",
    "K", "P", "D/ST", "DP", "DL", "DE", "DT", "LB", "DB", "CB", "S", "EDR",
]

# The flex slots, written the way a scoreboard writes them.
SLOT_LABELS = {"RB/WR": "W/R", "RB/WR/TE": "W/R/T", "WR/TE": "W/T"}


def slot_label(slot: str | None) -> str:
    return SLOT_LABELS.get(slot or "", slot or "")


def _slot_rank(slot: str) -> tuple[int, str]:
    try:
        return (SLOT_ORDER.index(slot), "")
    except ValueError:
        return (len(SLOT_ORDER), slot)


def live_totals(lineup: list[dict], games: dict[str, dict]) -> dict:
    """A team's headline score pair: what's on the board now, and where the
    week is expected to land.

    The two sums deliberately cover different sets of starters. `actual` only
    counts a player once their game has state 'in' or 'post' -- a starter whose
    game is still 'pre', or who has no game row at all (a bye, an unresolved
    pro_team), contributes nothing, even if a stale number happens to sit in
    their `actual` column. Crediting a franchise with points from a player who
    has not kicked off yet is the whole failure mode this exists to avoid.
    `projected` has no such filter: it sums every starter, because it stands
    for the full expected final total, the number `actual` is climbing toward.
    """
    actual = projected = 0.0
    counted = pending = 0
    for p in lineup:
        projected += p.get("projected") or 0
        game = games.get(p.get("pro_team")) if p.get("pro_team") else None
        if game and game.get("state") in ("in", "post"):
            actual += p.get("actual") or 0
            counted += 1
        else:
            pending += 1
    return {"actual": round(actual, 1), "projected": round(projected, 1),
            "counted": counted, "pending": pending}


def roster_rows(roster: list[dict]) -> list[dict]:
    """One franchise's roster, ordered the way a box score reads: starters in
    SLOT_ORDER, then bench and IR.

    `pair_lineups` answers a different question -- it pairs two franchises'
    starters onto shared-slot rows for a matchup card, and drops anyone not a
    starter in the process. This is one team on its own, for the modal a click
    on a team name opens, so there is no second side to align against and the
    reserve slots that a matchup card has no room for need to survive. Sorting
    the whole roster by `_slot_rank` does the ordering in one pass: real lineup
    slots (QB, RB, ...) all rank below the reserve slots (BE, IR, RES), which
    are not in SLOT_ORDER and so fall to the end together, in the same order
    `pair_lineups` would put them in if it ever saw them.
    """
    rows = sorted(roster or [], key=lambda p: _slot_rank(p.get("lineup_slot") or ""))
    return [{
        "player": p,
        "slot": p.get("lineup_slot") or "",
        "label": slot_label(p.get("lineup_slot")),
        "reserve": not p.get("is_starter"),
    } for p in rows]


def pair_lineups(home: list[dict] | None, away: list[dict] | None) -> list[dict]:
    """One row per lineup slot, both franchises on it.

    The slot is a property of the row, not of either player, which is what lets
    a card print it once down the middle instead of twice. That only holds if
    the two lineups are aligned by slot: left and right both arrive sorted by
    points, so row three of one is a running back and row three of the other is
    a kicker, and a single shared label would be a lie about one of them.

    Where a slot appears more than once -- two running backs, three receivers --
    the higher scorer of each side pairs with the higher scorer of the other.
    """
    def by_slot(players):
        buckets: dict[str, list[dict]] = defaultdict(list)
        for player in players or []:
            buckets[player.get("lineup_slot") or ""].append(player)
        for group in buckets.values():
            group.sort(key=lambda p: -((p.get("actual")
                                        if p.get("actual") is not None
                                        else p.get("projected")) or 0))
        return buckets

    left, right = by_slot(home), by_slot(away)
    slots = sorted(set(left) | set(right), key=_slot_rank)

    rows = []
    for slot in slots:
        theirs, ours = left.get(slot, []), right.get(slot, [])
        for i in range(max(len(theirs), len(ours))):
            rows.append({
                "slot": slot,
                "label": slot_label(slot),
                "home": theirs[i] if i < len(theirs) else None,
                "away": ours[i] if i < len(ours) else None,
            })
    return rows

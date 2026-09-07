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

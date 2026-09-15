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

from . import nflstats

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


def _best_first(by: str):
    """Descending on `by`, players with no value last.

    Not `-(value or 0)`: fantasy points go negative (a quarterback's fumble
    week, a defence that gets shredded), and treating a missing score as 0 would
    rank a player who did not play above one who played badly.
    """
    return lambda r: (r.get(by) is None, -(r.get(by) or 0), -(r.get("projected") or 0), r.get("name") or "")


def group_by_position(rows: list[dict], per_group: int = 60,
                      available_only: bool = False, by: str = "projected") -> list[dict]:
    """Rank players into league lineup groups, best `by` first.

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
        players = sorted(players, key=_best_first(by))
        out.append({"label": label, "players": players[:per_group],
                    "total": len(players)})
    return out


def storage_rows(rows: list[dict], per_group: int = 60) -> list[dict]:
    """The players worth keeping for a stored week: this week's top projections,
    plus this week's top actuals.

    Keeping only the top N by projection drops exactly the players the season
    columns exist to surface -- a low-projected player who has a big week -- so
    a finished week also keeps its top scorers. Deduped by player_id in
    first-seen order: a player who clears both cuts is stored once.
    """
    seen: dict[int, dict] = {}
    for group in group_by_position(rows, per_group):
        for row in group["players"]:
            seen.setdefault(row["player_id"], row)
    for group in group_by_position(rows, per_group, by="actual"):
        for row in group["players"]:
            seen.setdefault(row["player_id"], row)
    return list(seen.values())


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
    """Every player in one list, best `by` first, tagged with its lineup group.

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
    out.sort(key=_best_first(by))
    return out


def played_weeks(actual_weeks: list[int], games: list[dict], current: int | None) -> list[int]:
    """Which of the stored projection weeks have actually been played.

    A week whose games are still running has partial actuals; it is not
    "already played", and its projection is still the useful number. Before
    the season has a current week at all, every stored week counts as played --
    there is nothing left to be mid-flight. A week behind the current one is
    always played; the current week itself only counts once every one of its
    games has reached a final state.
    """
    by_week: dict[int, list[dict]] = defaultdict(list)
    for game in games:
        by_week[game.get("week")].append(game)

    out = []
    for week in sorted(actual_weeks):
        if current is None or week < current:
            out.append(week)
            continue
        week_games = by_week.get(week)
        if week_games and all((g.get("state") or "").lower() == "post" for g in week_games):
            out.append(week)
    return out


def next_projection_week(projection_weeks: list[int], played: list[int]) -> int | None:
    """The soonest stored week that has not yet been played, if any."""
    remaining = sorted(w for w in projection_weeks if w not in played)
    return remaining[0] if remaining else None


def season_rows(by_week: dict[int, list[dict]], played: list[int],
                next_week: int | None) -> list[dict]:
    """One row per player, season points to date plus next week's projection.

    A player who has not played any of the stored weeks but is projected for
    next week still gets a row -- someone who missed the season so far to
    injury and is projected to return belongs on the page. Identity fields
    (name, position, ...) come from the next week's row where there is one,
    since that is the freshest data; a player with no next-week row (already
    on a bye, or dropped) falls back to their most recent played appearance.
    `avg` divides by weeks actually scored, not weeks played -- a bye or a DNP
    should not drag a per-game average down.
    """
    next_rows = {r["player_id"]: r for r in by_week.get(next_week, [])} if next_week else {}
    played_rows = {week: {r["player_id"]: r for r in by_week.get(week, [])} for week in played}
    latest_by_player: dict[int, dict] = {}
    for week in played:
        for player_id, row in played_rows[week].items():
            latest_by_player[player_id] = row

    player_ids = set(next_rows) | set(latest_by_player)
    out = []
    for player_id in player_ids:
        source = next_rows.get(player_id) or latest_by_player[player_id]
        row = {k: source.get(k) for k in
               ("player_id", "name", "position", "pro_team", "on_team_id", "percent_owned")}
        row["projected"] = next_rows.get(player_id, {}).get("projected")

        total = 0.0
        has_total = False
        scored_weeks = 0
        for week in played:
            actual = played_rows[week].get(player_id, {}).get("actual")
            row[f"w{week}"] = actual
            if actual is not None:
                total += actual
                has_total = True
                scored_weeks += 1
        row["total"] = round(total, 1) if has_total else None
        row["avg"] = round(total / scored_weeks, 1) if scored_weeks else None
        out.append(row)
    return out


def columns(played: list[int], next_week: int | None, pos: str, matchups: bool) -> list[dict]:
    """The table's column set, in display order, for the current filters.

    Identity, then fantasy, then (if requested) matchups, then NFL season
    stats -- the last narrowed to the families `pos` actually plays, or every
    family when no position is picked. `desc` is the direction a first click
    on that column sorts; `block` marks the first column of a new visual
    group, for the divider style.css draws between them.
    """
    def col(key, label, kind, desc, title=None, block=False):
        return {"key": key, "label": label, "title": title, "kind": kind,
                "desc": desc, "block": block}

    cols = [col("name", "Player", "player", False)]
    if not pos:
        cols.append(col("group", "Pos", "text", False))
    cols += [
        col("pro_team", "NFL", "text", False),
        col("owner", "Owner", "text", False, title="Fantasy team; blank is a free agent"),
        col("percent_owned", "Own%", "own", True),
    ]

    block = True
    for week in played:
        cols.append(col(f"w{week}", f"W{week}", "pts", True,
                        title=f"Fantasy points, week {week}", block=block))
        block = False
    if played:
        cols.append(col("total", "Tot", "total", True, block=block))
        block = False
        cols.append(col("avg", "Avg", "pts", True, title="Points per scored week", block=block))
        block = False
    if next_week:
        cols.append(col("projected", f"Proj W{next_week}", "proj", True, block=block))
        block = False

    if matchups:
        cols.append(col("opp", "Opp", "opp", False, block=True))
        for key, label in (("run_rank", "Run"), ("pass_rank", "Pass"),
                           ("kick_rank", "Kick"), ("def_rank", "Def")):
            cols.append(col(key, label, "rank", False))

    cols.append(col("gp", "GP", "int", True, block=True))
    if pos not in nflstats.NO_SNAP_GROUPS:
        cols.append(col("snap_pct", "Snap%", "pct", True))

    families = nflstats.FAMILY_ORDER if not pos else nflstats.GROUP_FAMILIES.get(pos, [])
    for family in (f for f in nflstats.FAMILY_ORDER if f in families):
        for key, label, title, fam in nflstats.STAT_COLUMNS:
            if fam == family:
                cols.append(col(key, label, "pct" if key == "tgt_share" else "int",
                                True, title=title))
    return cols


def resolve_sort(sort: str | None, direction: str | None, cols: list[dict],
                 played: list[int], has_next: bool) -> tuple[str, bool]:
    """The (key, desc) pair a sort request resolves to.

    An invalid or absent request -- an old bookmark for a column the current
    filters no longer offer -- falls back to the season total once there is
    one, and to the next projection before that, rather than erroring or
    sorting on nothing.
    """
    if sort == "proj":
        sort = "projected"
    keys = {c["key"]: c for c in cols}
    if sort not in keys:
        sort = "total" if played else ("projected" if has_next else "name")
    if direction == "desc":
        desc = True
    elif direction == "asc":
        desc = False
    else:
        desc = keys[sort]["desc"]
    return sort, desc


# The four matchup ranks live inside row["matchup"], attached separately by
# attach_matchups, rather than as plain row keys -- sort_rows has to know to
# look there instead of on the row itself.
_MATCHUP_KEYS = {"run_rank", "pass_rank", "kick_rank", "def_rank"}

_GROUP_ORDER = {label: i for i, (label, _) in enumerate(POSITION_GROUPS)}


def _sort_value(row: dict, key: str):
    """The raw value `key` reads off `row` for sorting, missing as None."""
    if key == "group":
        return _GROUP_ORDER.get(row.get("group"))
    if key == "opp":
        matchup = row.get("matchup")
        return matchup.get("opponent") if matchup else None
    if key in _MATCHUP_KEYS:
        matchup = row.get("matchup")
        return matchup.get(key) if matchup else None
    value = row.get(key)
    return None if value == "" else value


def _norm(value):
    return value.casefold() if isinstance(value, str) else value


def sort_rows(rows: list[dict], key: str, desc: bool) -> list[dict]:
    """`rows`, ordered by `key`.

    A player missing `key` -- no game that week, no NFL stat line synced, no
    matchup this week -- sorts last regardless of direction: an ascending sort
    putting every blank player ahead of every player who actually has a value
    would be worse than useless. Ties break on projected points (highest
    first, missing last), then name (A-Z); that pair is computed once as the
    tiebreak key and used to order both the present rows (before the primary,
    stable, sort) and the missing ones (which never reach a primary sort at
    all).
    """
    def tiebreak(row):
        return (row.get("projected") is None, -(row.get("projected") or 0),
                (row.get("name") or "").casefold())

    present, missing = [], []
    for row in rows:
        (missing if _sort_value(row, key) is None else present).append(row)

    present.sort(key=tiebreak)
    present.sort(key=lambda r: _norm(_sort_value(r, key)), reverse=desc)
    missing.sort(key=tiebreak)
    return present + missing


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


def live_totals(lineup: list[dict], games: dict[str, dict],
                 projected: float | None = None) -> dict:
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

    That summed-from-starters projection is itself a pre-game forecast: each
    player's `projected` figure is static, set once by ESPN and never moved
    once their game kicks off, so the sum reads the same all week. The
    `projected` argument is ESPN's own live team projection -- the one figure
    that does converge as games play out -- and it wins whenever we have it.
    The sum remains the fallback for anywhere we do not: the pre-season hub, a
    settled past week, or a tick where ESPN published no live figure.
    """
    actual = summed_projected = 0.0
    counted = pending = 0
    for p in lineup:
        summed_projected += p.get("projected") or 0
        game = games.get(p.get("pro_team")) if p.get("pro_team") else None
        if game and game.get("state") in ("in", "post"):
            actual += p.get("actual") or 0
            counted += 1
        else:
            pending += 1
    if projected is not None and projected > 0:
        summed_projected = projected
    return {"actual": round(actual, 1), "projected": round(summed_projected, 1),
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

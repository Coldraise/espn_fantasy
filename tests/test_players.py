"""Player position mapping and grouping.

Guards two ESPN traps that produce plausible-looking wrong output:
defaultPositionId is not the lineup-slot enum, and player names are not unique.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, players


def test_default_position_map_is_not_the_slot_map():
    """POSITION_MAP is lineup slots; using it here puts TEs in WR and empties QB."""
    from espn_api.football.constant import POSITION_MAP
    assert players.position_for(4) == "TE"       # slot map would say WR
    assert POSITION_MAP.get(4) == "WR"           # ...proving they differ
    assert players.position_for(1) == "QB"
    assert players.position_for(3) == "WR"
    assert players.position_for(5) == "K"
    assert players.position_for(16) == "D/ST"


def test_idp_positions_fold_into_league_slots():
    assert players.group_for("DE") == "DL"
    assert players.group_for("DT") == "DL"
    assert players.group_for("CB") == "DB"
    assert players.group_for("S") == "DB"
    assert players.group_for("LB") == "LB"


def test_unknown_position_is_dropped_not_guessed():
    assert players.position_for(99) is None
    assert players.group_for(None) is None
    assert players.group_by_position([{"player_id": 1, "position": None, "projected": 99}]) == []


def _p(pid, name, pos, proj, on_team=0):
    return {"player_id": pid, "name": name, "position": pos,
            "projected": proj, "on_team_id": on_team}


ROWS = [
    _p(1, "Brock Bowers", "TE", 15.2),
    _p(2, "Puka Nacua", "WR", 20.1),
    _p(3, "Jalen Hurts", "QB", 20.2),
    _p(4, "Maxx Crosby", "DE", 7.8),
    _p(5, "Jeffery Simmons", "DT", 8.0),
    _p(6, "Budda Baker", "S", 9.0),
    _p(7, "Tykee Smith", "CB", 9.2),
    _p(8, "Fred Warner", "LB", 12.5, on_team=3),
]


def test_grouping_orders_by_projection_within_group():
    groups = {g["label"]: g for g in players.group_by_position(ROWS)}
    assert [p["name"] for p in groups["DL"]["players"]] == ["Jeffery Simmons", "Maxx Crosby"]
    assert [p["name"] for p in groups["DB"]["players"]] == ["Tykee Smith", "Budda Baker"]
    assert groups["TE"]["players"][0]["name"] == "Brock Bowers"


def test_groups_follow_league_slot_order():
    labels = [g["label"] for g in players.group_by_position(ROWS)]
    assert labels == ["QB", "WR", "TE", "LB", "DL", "DB"], "empty groups dropped, order kept"


def test_available_only_excludes_rostered():
    all_labels = [g["label"] for g in players.group_by_position(ROWS)]
    free = [g["label"] for g in players.group_by_position(ROWS, available_only=True)]
    assert "LB" in all_labels and "LB" not in free, "Fred Warner is rostered"


def test_per_group_cap():
    rows = [_p(i, f"P{i}", "RB", 100 - i) for i in range(30)]
    group = players.group_by_position(rows, per_group=5)[0]
    assert len(group["players"]) == 5
    assert group["total"] == 30, "total reports the full pool, not the capped slice"


def test_players_sharing_a_name_are_kept_separate():
    """There is a WR and a LB both named Justin Jefferson."""
    rows = [_p(101, "Justin Jefferson", "WR", 18.0), _p(202, "Justin Jefferson", "LB", 6.0)]
    groups = {g["label"]: g for g in players.group_by_position(rows)}
    assert groups["WR"]["players"][0]["player_id"] == 101
    assert groups["LB"]["players"][0]["player_id"] == 202


def test_projection_storage_replaces_not_accumulates():
    """Projections are a forecast: a re-sync must replace the week, not stack."""
    path = "/tmp/_players_test.db"
    if os.path.exists(path):
        os.remove(path)
    conn = db.connect(path)
    db.init_db(conn)
    rows = [{"player_id": 1, "name": "A", "position": "QB", "pro_team": "BUF",
             "projected": 20.0, "percent_owned": 99.0, "on_team_id": 0}]
    db.replace_player_projections(conn, 2026, 1, rows)
    rows[0]["projected"] = 22.5
    db.replace_player_projections(conn, 2026, 1, rows)
    stored = db.fetch_player_projections(conn, 2026, 1)
    assert len(stored) == 1 and stored[0]["projected"] == 22.5
    assert db.projection_weeks(conn, 2026) == [1]
    conn.close()


# --- rookie flag and matchup lineups --------------------------------------

def _fresh(path="/tmp/_rookie_test.db"):
    if os.path.exists(path):
        os.remove(path)
    c = db.connect(path)
    db.init_db(c)
    return c


def test_rookie_is_experience_zero_not_an_inference():
    c = _fresh()
    db.replace_players(c, {
        101: {"name": "Rook", "pro_team": "DET", "years": 0},
        102: {"name": "Sophomore", "pro_team": "DET", "years": 1},
        103: {"name": "Vet", "pro_team": "DET", "years": 8},
        104: {"name": "Unknown exp", "pro_team": "DET", "years": None},
    })
    assert db.rookie_ids(c) == {101}, "only years == 0 is a rookie"
    c.close()


def test_unknown_player_gets_no_badge_rather_than_a_default():
    """A player absent from the reference table must not be guessed either way.
    This also covers D/ST entries, which carry negative ids and are not people."""
    c = _fresh()
    db.replace_players(c, {101: {"name": "Rook", "pro_team": "DET", "years": 0}})
    db.replace_team_week_players(c, 2026, 1, [
        {"team_id": 1, "player_id": 101, "name": "Rook", "position": "RB",
         "lineup_slot": "RB", "projected": 12.0, "actual": None},
        {"team_id": 1, "player_id": -16033, "name": "Ravens D/ST", "position": "D/ST",
         "lineup_slot": "D/ST", "projected": 7.3, "actual": None},
    ])
    by_team = db.fetch_team_week_players(c, 2026, 1)
    flags = {p["name"]: p["rookie"] for p in by_team[1]}
    assert flags["Rook"] == 1
    assert flags["Ravens D/ST"] == 0
    c.close()


def test_lineups_ordered_by_actual_once_played_else_projected():
    c = _fresh()
    db.replace_team_week_players(c, 2026, 1, [
        {"team_id": 1, "player_id": 1, "name": "LowProjBigGame", "position": "RB",
         "lineup_slot": "RB", "projected": 5.0, "actual": 30.0},
        {"team_id": 1, "player_id": 2, "name": "HighProjBust", "position": "WR",
         "lineup_slot": "WR", "projected": 20.0, "actual": 2.0},
    ])
    names = [p["name"] for p in db.fetch_team_week_players(c, 2026, 1)[1]]
    assert names[0] == "LowProjBigGame", "actual points win once the game is played"
    c.close()


def test_unsigned_players_have_no_pro_team():
    """espn-api maps proTeamId 0 to the string "None", which is truthy -- letting
    it through puts a free agent on a team called None and tints their row."""
    from app import espn_client

    pro_team_map = {0: "None", 8: "DET"}
    assert espn_client._pro_team(pro_team_map, {"proTeamId": 0}) is None
    assert espn_client._pro_team(pro_team_map, {}) is None
    assert espn_client._pro_team(pro_team_map, {"proTeamId": 8}) == "DET"


def test_every_nfl_team_abbreviation_has_a_colour():
    """The lineup tint is keyed on ESPN's abbreviations -- WSH not WAS, JAX not
    JAC -- so a mismatch shows up as an untinted row, not as an error."""
    from espn_api.football.constant import PRO_TEAM_MAP

    from app.players import PRO_TEAM_COLORS

    live = {v for v in PRO_TEAM_MAP.values() if v and v != "None"}
    assert live - set(PRO_TEAM_COLORS) == set()


def test_sync_matchup_players_keeps_the_whole_starting_lineup():
    """The featured card shows all 16 starters, so the poller must store all of
    them -- a top-N slice would send the page back to ESPN for the rest."""
    from app import espn_client, poller

    roster = {1: [{"player_id": n, "name": f"P{n}", "position": "RB",
                   "lineup_slot": "BE" if n > 15 else "RB",
                   "projected": float(n), "actual": None} for n in range(1, 21)]}
    original = espn_client.matchup_rosters
    espn_client.matchup_rosters = lambda season, week: roster
    try:
        c = _fresh()
        poller.sync_matchup_players(c, 2026, 1)
        stored = db.fetch_team_week_players(c, 2026, 1)[1]
        assert len(stored) == 15, "every starter, bench excluded"
        assert [p["name"] for p in stored[:2]] == ["P15", "P14"], "best first"
        c.close()
    finally:
        espn_client.matchup_rosters = original


def test_sync_matchup_players_excludes_bench_and_ir(monkeypatch=None):
    """A benched player can hold the highest projection on a roster; billing them
    as the team's best player for a week they don't play would be wrong."""
    from app import espn_client, poller

    roster = {
        4: [
            {"player_id": 1, "name": "Benched Star", "position": "QB",
             "lineup_slot": "BE", "projected": 99.0, "actual": None},
            {"player_id": 2, "name": "IR Guy", "position": "RB",
             "lineup_slot": "IR", "projected": 88.0, "actual": None},
            {"player_id": 3, "name": "Real Starter", "position": "RB",
             "lineup_slot": "RB", "projected": 21.1, "actual": None},
            {"player_id": 4, "name": "Second Starter", "position": "WR",
             "lineup_slot": "WR", "projected": 16.9, "actual": None},
        ]
    }
    original = espn_client.matchup_rosters
    espn_client.matchup_rosters = lambda season, week: roster
    try:
        c = _fresh()
        poller.sync_matchup_players(c, 2026, 1)
        stored = [p["name"] for p in db.fetch_team_week_players(c, 2026, 1)[4]]
        assert stored == ["Real Starter", "Second Starter"]
        assert "Benched Star" not in stored and "IR Guy" not in stored
        c.close()
    finally:
        espn_client.matchup_rosters = original


def test_player_sync_is_skipped_while_fresh():
    """32 requests is a daily cost, not a per-poll one."""
    from app import espn_client, poller
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return {101: {"name": "Rook", "pro_team": "DET", "years": 0}}

    original = espn_client.nfl_experience
    espn_client.nfl_experience = fake
    try:
        c = _fresh()
        assert poller.sync_players(c) == 1 and calls["n"] == 1
        assert poller.sync_players(c) == 0 and calls["n"] == 1, "skipped while fresh"
        assert poller.sync_players(c, force=True) == 1 and calls["n"] == 2
        c.close()
    finally:
        espn_client.nfl_experience = original


# --- last week's actuals beside this week's projection --------------------

def _proj(pid, name, pos, projected, actual=None):
    return {"player_id": pid, "name": name, "position": pos, "pro_team": "DET",
            "projected": projected, "actual": actual, "percent_owned": 50.0,
            "on_team_id": 0}


def test_week_actuals_returns_only_played_results():
    c = _fresh()
    db.replace_player_projections(c, 2026, 1, [
        _proj(1, "Played Well", "RB", 12.0, actual=24.5),
        _proj(2, "Played Badly", "WR", 18.0, actual=3.1),
        _proj(3, "Did Not Play", "TE", 9.0, actual=None),
    ])
    assert db.week_actuals(c, 2026, 1) == {1: 24.5, 2: 3.1}
    c.close()


def test_week_actuals_empty_before_week_one():
    """Week 1 has no previous week; the lookup must not invent one."""
    c = _fresh()
    db.replace_player_projections(c, 2026, 1, [_proj(1, "A", "RB", 12.0, actual=20.0)])
    assert db.week_actuals(c, 2026, 0) == {}
    assert db.week_actuals(c, 2026, -1) == {}
    c.close()


def test_last_week_joins_by_player_id_across_weeks():
    """Week 2's view shows week 1's actual for the same player."""
    c = _fresh()
    db.replace_player_projections(c, 2026, 1, [
        _proj(1, "Star", "RB", 15.0, actual=27.4),
        _proj(2, "Bust", "WR", 19.0, actual=2.0),
    ])
    db.replace_player_projections(c, 2026, 2, [
        _proj(1, "Star", "RB", 17.0),
        _proj(2, "Bust", "WR", 14.0),
        _proj(3, "Newcomer", "TE", 8.0),
    ])
    last = db.week_actuals(c, 2026, 1)
    rows = db.fetch_player_projections(c, 2026, 2)
    for r in rows:
        r["last"] = last.get(r["player_id"])
    by_name = {r["name"]: r for r in rows}
    assert by_name["Star"]["last"] == 27.4 and by_name["Star"]["projected"] == 17.0
    assert by_name["Bust"]["last"] == 2.0
    assert by_name["Newcomer"]["last"] is None, "no prior row means no last-week value"
    c.close()


def test_a_week_resync_can_fill_in_actuals_written_as_null():
    """The row written before kickoff has a null actual; the later re-sync of
    that week is what fills it in."""
    c = _fresh()
    db.replace_player_projections(c, 2026, 1, [_proj(1, "Star", "RB", 15.0, actual=None)])
    assert db.week_actuals(c, 2026, 1) == {}
    db.replace_player_projections(c, 2026, 1, [_proj(1, "Star", "RB", 15.0, actual=27.4)])
    assert db.week_actuals(c, 2026, 1) == {1: 27.4}
    c.close()


def test_fifty_per_group_is_capped_not_padded():
    rows = [_proj(i, f"RB{i}", "RB", 100 - i) for i in range(80)]
    rows += [_proj(500 + i, f"K{i}", "K", 10 - i * 0.1) for i in range(20)]
    groups = {g["label"]: g for g in players.group_by_position(rows, per_group=50)}
    assert len(groups["RB"]["players"]) == 50 and groups["RB"]["total"] == 80
    assert len(groups["K"]["players"]) == 20, "a shallow group shows what it has"
    assert groups["K"]["total"] == 20


def test_projection_sync_is_throttled():
    """tick() runs every 45s during games; each projection fetch is several MB,
    so repeated ticks must not re-pull it."""
    from app import espn_client, poller

    calls = {"n": 0}

    def counted(season, week, limit=2000):
        calls["n"] += 1
        return [{"player_id": 1, "name": "A", "position": "RB", "pro_team": "DET",
                 "projected": 10.0, "actual": None, "percent_owned": 9.0, "on_team_id": 0}]

    original = espn_client.player_projections
    original_age = poller.PROJECTION_MAX_AGE_MINUTES
    espn_client.player_projections = counted
    try:
        c = _fresh()
        poller.sync_projections(c, 2026, 1)
        assert calls["n"] == 1
        poller.sync_projections(c, 2026, 1)
        assert calls["n"] == 1, "a fresh week must not be re-fetched"
        poller.sync_projections(c, 2026, 1, force=True)
        assert calls["n"] == 2, "force overrides the throttle"
        poller.PROJECTION_MAX_AGE_MINUTES = 0
        poller.sync_projections(c, 2026, 1)
        assert calls["n"] == 3, "an expired week is re-fetched"
        c.close()
    finally:
        espn_client.player_projections = original
        poller.PROJECTION_MAX_AGE_MINUTES = original_age


# --- shortened names for narrow rows ---------------------------------------

def test_short_name_takes_the_first_name_and_an_initial():
    assert players.short_name("Derrick Henry") == "Derrick H."
    assert players.short_name("Bijan Robinson") == "Bijan R."


def test_short_name_keeps_punctuation_inside_a_name():
    """An apostrophe or hyphen is part of the name, not a separator."""
    assert players.short_name("Ja'Marr Chase") == "Ja'Marr C."
    assert players.short_name("A.J. Brown") == "A.J. B."
    assert players.short_name("T.J. Watt") == "T.J. W."


def test_short_name_drops_generational_suffixes():
    """Without this, "Kenneth Walker III" initials to "Kenneth I."."""
    assert players.short_name("Kenneth Walker III") == "Kenneth W."
    assert players.short_name("Marvin Harrison Jr.") == "Marvin H."
    assert players.short_name("Michael Pittman Jr.") == "Michael P."


def test_short_name_leaves_team_defences_alone():
    """The general rule would make "Titans D/ST" into "Titans D." -- plausible
    enough on screen that nobody would report it, and wrong."""
    assert players.short_name("Titans D/ST") == "Titans D/ST"
    assert players.short_name("49ers D/ST") == "49ers D/ST"


def test_short_name_handles_nothing_to_shorten():
    assert players.short_name("Titans") == "Titans"
    assert players.short_name("") == ""
    assert players.short_name(None) == ""
    assert players.short_name("  Bijan  Robinson  ") == "Bijan R."


# --- one list across every position ----------------------------------------

RANK_ROWS = [
    {"name": "Kicker", "position": "K", "projected": 9, "actual": 0},
    {"name": "Back", "position": "RB", "projected": 21, "actual": 0},
    {"name": "Back2", "position": "RB", "projected": 18, "actual": 0},
    {"name": "Passer", "position": "QB", "projected": 20, "actual": 0},
    {"name": "Snapper", "position": "LS", "projected": 99, "actual": 0},
]


def test_ranked_orders_across_positions_not_within_them():
    """Concatenating the groups would put the best kicker above the second
    running back, because each group is only sorted inside itself."""
    got = [p["name"] for p in players.ranked(RANK_ROWS)]
    assert got == ["Back", "Passer", "Back2", "Kicker"]


def test_ranked_tags_each_row_with_its_group():
    by_name = {p["name"]: p["group"] for p in players.ranked(RANK_ROWS)}
    assert by_name["Back"] == "RB"
    assert by_name["Passer"] == "QB"


def test_ranked_drops_positions_the_league_does_not_start():
    assert all(p["name"] != "Snapper" for p in players.ranked(RANK_ROWS))


def test_ranked_can_order_on_the_result_instead():
    rows = [{"name": "A", "position": "RB", "projected": 30, "actual": 2},
            {"name": "B", "position": "RB", "projected": 1, "actual": 25}]
    assert [p["name"] for p in players.ranked(rows, by="actual")] == ["B", "A"]


def test_ranked_survives_missing_scores():
    rows = [{"name": "A", "position": "RB", "projected": None},
            {"name": "B", "position": "RB", "projected": 5}]
    assert [p["name"] for p in players.ranked(rows)] == ["B", "A"]


# --- one lineup down the middle of a matchup card --------------------------

def test_pair_lineups_puts_both_franchises_on_one_slot_row():
    """The card prints the slot once between the two scores, which is only
    honest if the row holds the same slot on both sides."""
    home = [{"name": "QB1", "lineup_slot": "QB", "projected": 20}]
    away = [{"name": "qb1", "lineup_slot": "QB", "projected": 15}]
    rows = players.pair_lineups(home, away)
    assert len(rows) == 1
    assert rows[0]["slot"] == "QB"
    assert rows[0]["home"]["name"] == "QB1"
    assert rows[0]["away"]["name"] == "qb1"


def test_pair_lineups_reads_in_slot_order_not_points_order():
    """Both sides arrive sorted by points, so row three of one is a running
    back and row three of the other a kicker. Pairing has to re-order."""
    home = [{"name": "K", "lineup_slot": "K", "projected": 30},
            {"name": "QB", "lineup_slot": "QB", "projected": 1}]
    away = [{"name": "qb", "lineup_slot": "QB", "projected": 9}]
    assert [r["slot"] for r in players.pair_lineups(home, away)] == ["QB", "K"]


def test_repeated_slots_pair_best_with_best():
    home = [{"name": "RBlow", "lineup_slot": "RB", "projected": 4},
            {"name": "RBhigh", "lineup_slot": "RB", "projected": 19}]
    away = [{"name": "rbhigh", "lineup_slot": "RB", "projected": 17},
            {"name": "rblow", "lineup_slot": "RB", "projected": 2}]
    rows = players.pair_lineups(home, away)
    assert [(r["home"]["name"], r["away"]["name"]) for r in rows] == [
        ("RBhigh", "rbhigh"), ("RBlow", "rblow")]


def test_a_slot_only_one_side_starts_leaves_the_other_empty():
    rows = players.pair_lineups([{"name": "K", "lineup_slot": "K", "projected": 8}], [])
    assert rows[0]["home"]["name"] == "K"
    assert rows[0]["away"] is None


def test_a_bye_week_still_lists_the_lineup():
    rows = players.pair_lineups([{"name": "QB", "lineup_slot": "QB", "projected": 8}], None)
    assert len(rows) == 1 and rows[0]["away"] is None


def test_flex_is_labelled_the_way_a_scoreboard_writes_it():
    assert players.slot_label("RB/WR") == "W/R"
    assert players.slot_label("RB/WR/TE") == "W/R/T"
    assert players.slot_label("QB") == "QB"
    assert players.slot_label(None) == ""


def test_an_unknown_slot_sorts_last_rather_than_vanishing():
    home = [{"name": "X", "lineup_slot": "MYSTERY", "projected": 5},
            {"name": "QB", "lineup_slot": "QB", "projected": 5}]
    assert [r["slot"] for r in players.pair_lineups(home, [])] == ["QB", "MYSTERY"]

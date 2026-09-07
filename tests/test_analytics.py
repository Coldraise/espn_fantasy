"""Analytics correctness against a hand-computed fixture.

Four teams, three weeks, every number below worked out by hand. This exists
because an off-by-one in all-play or luck produces numbers that still look
entirely plausible on the page.

Scores
        wk1   wk2   wk3
  T1    100    60   100
  T2     90    70    50
  T3     80    80    60
  T4     70    90    70

Matchups   wk1: 1v2, 3v4   wk2: 1v3, 2v4   wk3: 1v4, 2v3
Actual     T1 2-1   T2 0-3   T3 3-0   T4 1-2
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analytics

SCORES = {
    1: {1: 100.0, 2: 60.0, 3: 100.0},
    2: {1: 90.0, 2: 70.0, 3: 50.0},
    3: {1: 80.0, 2: 80.0, 3: 60.0},
    4: {1: 70.0, 2: 90.0, 3: 70.0},
}
PAIRS = {1: [(1, 2), (3, 4)], 2: [(1, 3), (2, 4)], 3: [(1, 4), (2, 3)]}


def build_rows():
    rows = []
    for week, pairs in PAIRS.items():
        for a, b in pairs:
            pa, pb = SCORES[a][week], SCORES[b][week]
            for team, opp, mine, theirs in ((a, b, pa, pb), (b, a, pb, pa)):
                rows.append({
                    "season": 2026, "week": week, "team_id": team,
                    "points": mine, "projected": None, "opponent_id": opp,
                    "is_playoff": 0,
                    "result": "W" if mine > theirs else "L" if mine < theirs else "T",
                })
    return rows


ROWS = build_rows()


def test_actual_records():
    summary = analytics.team_summary(ROWS)
    assert (summary[1]["W"], summary[1]["L"]) == (2, 1)
    assert (summary[2]["W"], summary[2]["L"]) == (0, 3)
    assert (summary[3]["W"], summary[3]["L"]) == (3, 0)
    assert (summary[4]["W"], summary[4]["L"]) == (1, 2)


def test_all_play_hand_computed():
    ap = analytics.all_play(ROWS)
    assert (ap[1]["W"], ap[1]["L"]) == (6, 3)
    assert (ap[2]["W"], ap[2]["L"]) == (3, 6)
    assert (ap[3]["W"], ap[3]["L"]) == (4, 5)
    assert (ap[4]["W"], ap[4]["L"]) == (5, 4)


def test_all_play_conservation():
    """Total all-play wins == weeks * C(n,2). Catches double counting."""
    ap = analytics.all_play(ROWS)
    assert sum(r["W"] for r in ap.values()) == 3 * 6 == 18
    assert sum(r["W"] for r in ap.values()) == sum(r["L"] for r in ap.values())


def test_luck():
    """T3 went 3-0 while only the third-best scorer -- the luckiest team."""
    result = analytics.luck(ROWS)
    assert result[1]["luck"] == 0.0
    assert result[2]["luck"] == -1.0
    assert result[3]["luck"] == 1.67
    assert result[4]["luck"] == -0.67
    assert result[3]["luck"] == max(r["luck"] for r in result.values())


def test_head_to_head_symmetry():
    h2h = analytics.head_to_head(ROWS)
    assert h2h[1][2]["W"] == 1 and h2h[2][1]["L"] == 1
    assert h2h[3][1]["W"] == 1 and h2h[1][3]["L"] == 1
    for a, opponents in h2h.items():
        for b, record in opponents.items():
            mirror = h2h[b][a]
            assert record["W"] == mirror["L"] and record["T"] == mirror["T"]


def test_playoff_weeks_excluded_from_all_play():
    rows = [dict(r) for r in ROWS]
    for row in rows:
        if row["week"] == 3:
            row["is_playoff"] = 1
    assert sum(r["W"] for r in analytics.all_play(rows).values()) == 2 * 6
    assert sum(r["W"] for r in analytics.all_play(rows, regular_season_only=False).values()) == 18


def test_points_against_uses_opponent_score():
    summary = analytics.team_summary(ROWS)
    # T1 faced T2(90), T3(80), T4(70)
    assert summary[1]["pa"] == 240.0
    assert summary[1]["pf"] == 260.0


def test_in_progress_week_excluded_from_all_play():
    """A live week has scores but no results; counting it would swing standings
    mid-Sunday and then settle. All-play must ignore it."""
    live = [dict(r) for r in ROWS]
    for team, points, opp in ((1, 42.5, 3), (3, 51.0, 1), (2, 33.0, 4), (4, 29.5, 2)):
        live.append({"season": 2026, "week": 4, "team_id": team, "points": points,
                     "projected": None, "opponent_id": opp, "is_playoff": 0, "result": None})

    assert analytics.complete_weeks(live) == {(2026, 1), (2026, 2), (2026, 3)}
    # identical to the three-week answer: the live week contributed nothing
    assert analytics.all_play(live) == analytics.all_play(ROWS)
    assert analytics.luck(live) == analytics.luck(ROWS)
    assert sum(r["W"] for r in analytics.all_play(live).values()) == 3 * 6


def test_completed_week_still_counts_after_finalising():
    finalised = [dict(r) for r in ROWS]
    for team, points, opp, res in ((1, 42.5, 3, "L"), (3, 51.0, 1, "W"),
                                   (2, 33.0, 4, "W"), (4, 29.5, 2, "L")):
        finalised.append({"season": 2026, "week": 4, "team_id": team, "points": points,
                          "projected": None, "opponent_id": opp, "is_playoff": 0, "result": res})
    assert (2026, 4) in analytics.complete_weeks(finalised)
    assert sum(r["W"] for r in analytics.all_play(finalised).values()) == 4 * 6


# --- power rankings, projections, consistency, bench ----------------------
#
# Same fixture as above: T1 2-1, T2 0-3, T3 3-0, T4 1-2, with weekly scores
#   T1 100/60/100   T2 90/70/50   T3 80/80/60   T4 70/90/70

def test_consistency_hand_computed():
    """T3 scores 80/80/60 -- mean 73.33, and the tightest spread in the league."""
    c = analytics.consistency(ROWS)
    assert c[3]["avg"] == 73.33
    assert (c[3]["floor"], c[3]["ceiling"]) == (60.0, 80.0)
    assert c[3]["stdev"] == 9.43            # pstdev of 80,80,60
    # T1 swings 60 to 100 and is the most volatile of the four.
    assert c[1]["stdev"] > c[3]["stdev"]


def test_power_ranking_ignores_a_lucky_record():
    """T3 wins every game on 73.3 a week; T1 loses one on 86.7. All-play carries
    half the weight, so the schedule cannot be the whole story either way."""
    table = analytics.power_rankings(ROWS)
    assert [t["rank"] for t in table] == [1, 2, 3, 4]
    assert {t["team_id"] for t in table} == {1, 2, 3, 4}
    assert table[0]["power"] >= table[-1]["power"]
    # T1 outscores T3 by 13 a week; a ranking built on record alone would put
    # the 3-0 team top regardless.
    by_team = {t["team_id"]: t for t in table}
    assert by_team[1]["avg"] > by_team[3]["avg"]
    assert by_team[1]["power"] > by_team[2]["power"]


def test_power_rankings_empty_before_any_game():
    assert analytics.power_rankings([]) == []


def test_projection_accuracy_measures_bias_and_absolute_error():
    """Bias can cancel to zero while every week was badly wrong, so the mean
    absolute error is reported beside it."""
    rows = [
        {"season": 2026, "week": 1, "team_id": 1, "points": 110.0, "projected": 100.0,
         "opponent_id": 2, "is_playoff": 0, "result": "W"},
        {"season": 2026, "week": 2, "team_id": 1, "points": 90.0, "projected": 100.0,
         "opponent_id": 2, "is_playoff": 0, "result": "L"},
    ]
    acc = analytics.projection_accuracy(rows)[1]
    assert acc["bias"] == 0.0 and acc["error"] == 10.0 and acc["beat"] == 1


def test_projection_accuracy_skips_weeks_with_no_projection():
    rows = [{"season": 2026, "week": 1, "team_id": 1, "points": 100.0,
             "projected": None, "opponent_id": 2, "is_playoff": 0, "result": "W"}]
    assert analytics.projection_accuracy(rows) == {}


# --- best possible lineup -------------------------------------------------

SLOTS = [("QB", 1), ("RB", 2), ("RB/WR", 1)]


def _player(pid, position, points, starter):
    return {"player_id": pid, "position": position, "actual": points,
            "is_starter": starter}


def test_points_left_on_bench_finds_the_better_lineup():
    """The manager started a 5-point RB while a 20-point RB sat on the bench."""
    lineups = {1: [
        _player(1, "QB", 25.0, True),
        _player(2, "RB", 15.0, True),
        _player(3, "RB", 5.0, True),
        _player(4, "WR", 10.0, True),
        _player(5, "RB", 20.0, False),      # benched, and the best RB they had
    ]}
    result = analytics.points_left_on_bench(lineups, SLOTS)[1]
    assert result["actual"] == 55.0                  # 25 + 15 + 5 + 10
    assert result["optimal"] == 70.0                 # 25 + 20 + 15 + 10
    assert result["left_on_bench"] == 15.0
    assert result["efficiency"] == round(55 / 70, 3)


def test_a_perfect_lineup_scores_full_efficiency():
    lineups = {1: [
        _player(1, "QB", 25.0, True), _player(2, "RB", 15.0, True),
        _player(3, "RB", 12.0, True), _player(4, "WR", 10.0, True),
        _player(5, "RB", 2.0, False),
    ]}
    assert analytics.points_left_on_bench(lineups, SLOTS)[1]["efficiency"] == 1.0


def test_the_optimal_lineup_respects_slot_eligibility():
    """A 40-point QB cannot fill an RB slot, however much the team wishes it."""
    lineups = {1: [
        _player(1, "QB", 25.0, True), _player(2, "RB", 15.0, True),
        _player(3, "RB", 5.0, True), _player(4, "WR", 10.0, True),
        _player(5, "QB", 40.0, False),
    ]}
    result = analytics.points_left_on_bench(lineups, SLOTS)[1]
    # The bench QB beats the starting QB, but cannot also fill RB or RB/WR.
    assert result["optimal"] == 70.0                 # 40 + 15 + 5 + 10
    assert result["left_on_bench"] == 15.0


def test_flex_slots_accept_more_than_one_position():
    assert analytics._fills({"position": "WR"}, "RB/WR")
    assert analytics._fills({"position": "RB"}, "RB/WR")
    assert not analytics._fills({"position": "TE"}, "RB/WR")
    # IDP slots this league actually starts
    assert analytics._fills({"position": "DE"}, "DL")
    assert analytics._fills({"position": "CB"}, "DB")
    assert analytics._fills({"position": "LB"}, "DP")


def test_a_week_with_no_results_yet_is_skipped():
    """Mid-Sunday every actual is None; claiming 0.0 efficiency would be a lie."""
    lineups = {1: [_player(1, "QB", None, True), _player(2, "RB", None, False)]}
    assert analytics.points_left_on_bench(lineups, SLOTS) == {}


# --- draft ----------------------------------------------------------------

PICKS = [
    {"overall_pick": 1, "round": 1, "round_pick": 1, "team_id": 1, "player_id": 10,
     "player_name": "Bust", "keeper": 0},
    {"overall_pick": 2, "round": 1, "round_pick": 2, "team_id": 2, "player_id": 11,
     "player_name": "Star", "keeper": 0},
    {"overall_pick": 9, "round": 2, "round_pick": 1, "team_id": 2, "player_id": 12,
     "player_name": "Steal", "keeper": 0},
    {"overall_pick": 10, "round": 2, "round_pick": 2, "team_id": 1, "player_id": 13,
     "player_name": "Fine", "keeper": 0},
]
POINTS = {10: 50.0, 11: 250.0, 12: 200.0, 13: 100.0}
POSITIONS = {10: "RB", 11: "RB", 12: "WR", 13: "WR"}


def test_draft_value_is_measured_against_the_round_not_raw_points():
    """Raw points would only ever rediscover that round 1 outscores round 2.
    Round 1 here averages 150, round 2 averages 150 as well."""
    rows = analytics.draft_value(PICKS, POINTS, POSITIONS)
    by_name = {r["player_name"]: r for r in rows}
    assert by_name["Star"]["expected"] == 150.0 and by_name["Star"]["value"] == 100.0
    assert by_name["Bust"]["value"] == -100.0
    assert by_name["Steal"]["value"] == 50.0
    assert rows[0]["player_name"] == "Star"     # sorted best value first


def test_draft_value_ignores_picks_with_no_points_yet():
    assert analytics.draft_value(PICKS, {}) == []


def test_draft_value_by_team_finds_each_manager_s_best_and_worst():
    table = analytics.draft_value_by_team(analytics.draft_value(PICKS, POINTS, POSITIONS))
    best_team = table[0]
    assert best_team["team_id"] == 2 and best_team["value"] == 150.0
    assert best_team["best"]["player_name"] == "Star"
    assert table[-1]["worst"]["player_name"] == "Bust"


def test_positional_runs_finds_consecutive_same_position_picks():
    picks = [{"overall_pick": n, "player_id": n, "player_name": f"P{n}"} for n in range(1, 6)]
    positions = {1: "RB", 2: "RB", 3: "RB", 4: "WR", 5: "QB"}
    runs = analytics.positional_runs(picks, positions)
    assert len(runs) == 1
    assert runs[0]["position"] == "RB" and runs[0]["length"] == 3
    assert (runs[0]["start"], runs[0]["end"]) == (1, 3)


def test_a_run_shorter_than_the_threshold_is_not_a_run():
    picks = [{"overall_pick": n, "player_id": n, "player_name": f"P{n}"} for n in range(1, 4)]
    assert analytics.positional_runs(picks, {1: "RB", 2: "RB", 3: "WR"}) == []


def test_an_unknown_position_breaks_a_run_rather_than_extending_it():
    """Guessing that an unmapped player continues the run would invent one."""
    picks = [{"overall_pick": n, "player_id": n, "player_name": f"P{n}"} for n in range(1, 5)]
    assert analytics.positional_runs(picks, {1: "RB", 2: "RB", 4: "RB"}) == []

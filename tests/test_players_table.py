"""Table rendering for season statistics.

Tests for nflstats.season_stat_line, team_stat_line, attach_season_stats,
and players.columns, resolve_sort, sort_rows with their season-grain contracts.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import nflstats, players


# --- Fixtures: test data builders ---


def _week(gsis_id="player1", week=1, offense_snaps=10, defense_snaps=None,
          fantasy_points_ppr=5.0, passing_yards=100, passing_tds=1,
          passing_interceptions=0, carries=5, rushing_yards=50, rushing_tds=0,
          targets=3, target_share=0.15, receptions=2, receiving_yards=25,
          receiving_tds=0, fg_made=2, fg_att=3, pat_made=1,
          def_tackles_solo=4, def_tackle_assists=2, def_sacks=1.0,
          def_interceptions=0, def_pass_defended=2, def_fumbles_forced=1,
          def_tds=0, special_teams_tds=0):
    """Minimal player-week row for stat tests."""
    return {
        "gsis_id": gsis_id, "week": week,
        "offense_snaps": offense_snaps, "defense_snaps": defense_snaps,
        "fantasy_points_ppr": fantasy_points_ppr,
        "passing_yards": passing_yards, "passing_tds": passing_tds,
        "passing_interceptions": passing_interceptions,
        "carries": carries, "rushing_yards": rushing_yards, "rushing_tds": rushing_tds,
        "targets": targets, "target_share": target_share, "receptions": receptions,
        "receiving_yards": receiving_yards, "receiving_tds": receiving_tds,
        "fg_made": fg_made, "fg_att": fg_att, "pat_made": pat_made,
        "def_tackles_solo": def_tackles_solo, "def_tackle_assists": def_tackle_assists,
        "def_sacks": def_sacks, "def_interceptions": def_interceptions,
        "def_pass_defended": def_pass_defended, "def_fumbles_forced": def_fumbles_forced,
        "def_tds": def_tds, "special_teams_tds": special_teams_tds,
    }


def _team_week(team="KC", week=1, def_tackles_solo=5, def_tackle_assists=3,
               def_sacks=2.0, def_interceptions=1, def_pass_defended=4,
               def_fumbles_forced=2, def_tds=1, special_teams_tds=0):
    """Minimal team-week row for team stat tests."""
    return {
        "team": team, "week": week,
        "def_tackles_solo": def_tackles_solo, "def_tackle_assists": def_tackle_assists,
        "def_sacks": def_sacks, "def_interceptions": def_interceptions,
        "def_pass_defended": def_pass_defended, "def_fumbles_forced": def_fumbles_forced,
        "def_tds": def_tds, "special_teams_tds": special_teams_tds,
    }


def _season_row(player_id=1, name="Player", position="RB", pro_team="KC",
                group="RB", projected=None, actual=None):
    """Minimal season table row (before stat attachment)."""
    return {
        "player_id": player_id, "name": name, "position": position,
        "pro_team": pro_team, "group": group, "projected": projected,
        "actual": actual, "percent_owned": 50.0, "on_team_id": 0,
    }


# --- season_stat_line tests ---


class TestSeasonStatLineStructure:
    """Every result has every stat key plus gp and snap_pct."""

    def test_season_stat_line_all_keys_present(self):
        """Result contains every STAT_COLUMNS key, gp, snap_pct."""
        weeks = [_week()]
        result = nflstats.season_stat_line(weeks, "QB")
        stat_keys = {k for k, *_ in nflstats.STAT_COLUMNS}
        for key in stat_keys:
            assert key in result, f"{key} missing from result"
        assert "gp" in result
        assert "snap_pct" in result

    def test_season_stat_line_empty_weeks_all_none(self):
        """Empty weeks returns all values None."""
        result = nflstats.season_stat_line([], "RB")
        stat_keys = {k for k, *_ in nflstats.STAT_COLUMNS}
        for key in stat_keys:
            assert result[key] is None, f"{key} should be None"
        assert result["gp"] is None
        assert result["snap_pct"] is None


class TestSeasonStatLinePlayedWeeks:
    """A week counts as played if any of three snaps/points fields is truthy."""

    def test_season_stat_line_counts_offense_snaps_as_played(self):
        """Week with offense_snaps > 0 counts as played."""
        weeks = [_week(offense_snaps=10)]
        result = nflstats.season_stat_line(weeks, "QB")
        assert result["gp"] == 1

    def test_season_stat_line_counts_defense_snaps_as_played(self):
        """Week with defense_snaps > 0 counts as played."""
        weeks = [_week(offense_snaps=None, defense_snaps=8)]
        result = nflstats.season_stat_line(weeks, "LB")
        assert result["gp"] == 1

    def test_season_stat_line_counts_fantasy_points_as_played(self):
        """Week with fantasy_points_ppr counts as played."""
        weeks = [_week(offense_snaps=None, defense_snaps=None, fantasy_points_ppr=3.5)]
        result = nflstats.season_stat_line(weeks, "RB")
        assert result["gp"] == 1

    def test_season_stat_line_ignores_all_falsy_snaps_and_points(self):
        """Week with all three falsy does not count as played."""
        weeks = [_week(offense_snaps=0, defense_snaps=0, fantasy_points_ppr=0)]
        result = nflstats.season_stat_line(weeks, "QB")
        assert result["gp"] == 0

    def test_season_stat_line_counts_only_played_weeks(self):
        """gp counts only played weeks, not total weeks handed in."""
        weeks = [
            _week(week=1, offense_snaps=10),
            _week(week=2, offense_snaps=0, defense_snaps=0, fantasy_points_ppr=0),
            _week(week=3, offense_snaps=8),
        ]
        result = nflstats.season_stat_line(weeks, "QB")
        assert result["gp"] == 2


class TestSeasonStatLineQB:
    """QB families: pass and rush."""

    def test_season_stat_line_qb_fill_pass_rush_only(self):
        """QB fills pass and rush families; other families are None."""
        weeks = [_week()]
        result = nflstats.season_stat_line(weeks, "QB")
        # Pass keys
        assert result["pass_yds"] is not None
        assert result["pass_td"] is not None
        assert result["pass_int"] is not None
        # Rec keys should be None
        assert result["rec_yds"] is None
        assert result["receptions"] is None
        # Def keys should be None
        assert result["tackles"] is None
        assert result["sacks"] is None

    def test_season_stat_line_qb_pass_yds_sum(self):
        """QB pass_yds sums passing_yards over played weeks."""
        weeks = [
            _week(week=1, passing_yards=200),
            _week(week=2, passing_yards=300),
        ]
        result = nflstats.season_stat_line(weeks, "QB")
        assert result["pass_yds"] == 500

    def test_season_stat_line_qb_pass_td_sum(self):
        """QB pass_td sums passing_tds."""
        weeks = [
            _week(week=1, passing_tds=2),
            _week(week=2, passing_tds=3),
        ]
        result = nflstats.season_stat_line(weeks, "QB")
        assert result["pass_td"] == 5

    def test_season_stat_line_qb_pass_int_sum(self):
        """QB pass_int sums passing_interceptions."""
        weeks = [
            _week(week=1, passing_interceptions=1),
            _week(week=2, passing_interceptions=2),
        ]
        result = nflstats.season_stat_line(weeks, "QB")
        assert result["pass_int"] == 3

    def test_season_stat_line_qb_rush_yds_sum(self):
        """QB rush_yds sums rushing_yards (from rush family)."""
        weeks = [
            _week(week=1, rushing_yards=25),
            _week(week=2, rushing_yards=15),
        ]
        result = nflstats.season_stat_line(weeks, "QB")
        assert result["rush_yds"] == 40

    def test_season_stat_line_qb_rush_td_sum(self):
        """QB rush_td sums rushing_tds."""
        weeks = [
            _week(week=1, rushing_tds=1),
            _week(week=2, rushing_tds=0),
        ]
        result = nflstats.season_stat_line(weeks, "QB")
        assert result["rush_td"] == 1


class TestSeasonStatLineRB:
    """RB families: rush and rec."""

    def test_season_stat_line_rb_fill_rush_rec_only(self):
        """RB fills rush and rec; pass and def are None."""
        weeks = [_week()]
        result = nflstats.season_stat_line(weeks, "RB")
        # Rec keys
        assert result["rec_yds"] is not None
        assert result["receptions"] is not None
        # Rush keys
        assert result["carries"] is not None
        assert result["rush_yds"] is not None
        # Pass keys should be None
        assert result["pass_yds"] is None
        assert result["pass_td"] is None
        # Def keys should be None
        assert result["tackles"] is None

    def test_season_stat_line_rb_targets_sum(self):
        """RB targets sums targets."""
        weeks = [
            _week(week=1, targets=5),
            _week(week=2, targets=3),
        ]
        result = nflstats.season_stat_line(weeks, "RB")
        assert result["targets"] == 8

    def test_season_stat_line_rb_tgt_share_mean(self):
        """RB tgt_share is mean of target_share, ignoring None."""
        weeks = [
            _week(week=1, target_share=0.10),
            _week(week=2, target_share=0.20),
        ]
        result = nflstats.season_stat_line(weeks, "RB")
        assert result["tgt_share"] == 0.15

    def test_season_stat_line_rb_tgt_share_ignores_none(self):
        """RB tgt_share ignores None values when computing mean."""
        weeks = [
            _week(week=1, target_share=0.10),
            _week(week=2, target_share=None),
            _week(week=3, target_share=0.20),
        ]
        result = nflstats.season_stat_line(weeks, "RB")
        assert result["tgt_share"] == 0.15


class TestSeasonStatLineWR:
    """WR families: rec and rush (in that order in GROUP_FAMILIES)."""

    def test_season_stat_line_wr_fill_rec_rush_only(self):
        """WR fills rec and rush."""
        weeks = [_week()]
        result = nflstats.season_stat_line(weeks, "WR")
        assert result["rec_yds"] is not None
        assert result["carries"] is not None
        assert result["pass_yds"] is None


class TestSeasonStatLineTE:
    """TE family: rec only."""

    def test_season_stat_line_te_fill_rec_only(self):
        """TE fills rec only."""
        weeks = [_week()]
        result = nflstats.season_stat_line(weeks, "TE")
        assert result["rec_yds"] is not None
        assert result["carries"] is None
        assert result["pass_yds"] is None


class TestSeasonStatLineK:
    """K family: kick only; snap_pct is None."""

    def test_season_stat_line_k_fill_kick_only(self):
        """K fills kick family."""
        weeks = [_week()]
        result = nflstats.season_stat_line(weeks, "K")
        assert result["fgm"] is not None
        assert result["fga"] is not None
        assert result["xpm"] is not None
        assert result["rec_yds"] is None
        assert result["tackles"] is None

    def test_season_stat_line_k_snap_pct_none(self):
        """K snap_pct is None (kickers not in snap-share logic)."""
        weeks = [_week(offense_snaps=100)]
        result = nflstats.season_stat_line(weeks, "K")
        assert result["snap_pct"] is None


class TestSeasonStatLineDef:
    """Defensive families: def (def groups: LB, DL, DB; D/ST handled separately)."""

    def test_season_stat_line_lb_fill_def_only(self):
        """LB fills def family."""
        weeks = [_week()]
        result = nflstats.season_stat_line(weeks, "LB")
        assert result["tackles"] is not None
        assert result["sacks"] is not None
        assert result["def_int"] is not None
        assert result["rec_yds"] is None
        assert result["pass_yds"] is None

    def test_season_stat_line_def_tackles_sum(self):
        """Tackles = def_tackles_solo + def_tackle_assists."""
        weeks = [
            _week(week=1, def_tackles_solo=4, def_tackle_assists=2),
            _week(week=2, def_tackles_solo=3, def_tackle_assists=1),
        ]
        result = nflstats.season_stat_line(weeks, "LB")
        assert result["tackles"] == 10  # (4+2) + (3+1)

    def test_season_stat_line_def_sacks_sum_preserves_halves(self):
        """Sacks sums and preserves 0.5 values."""
        weeks = [
            _week(week=1, def_sacks=1.5),
            _week(week=2, def_sacks=0.5),
        ]
        result = nflstats.season_stat_line(weeks, "LB")
        assert result["sacks"] == 2.0

    def test_season_stat_line_def_int_sum(self):
        """def_int sums def_interceptions."""
        weeks = [
            _week(week=1, def_interceptions=1),
            _week(week=2, def_interceptions=2),
        ]
        result = nflstats.season_stat_line(weeks, "LB")
        assert result["def_int"] == 3

    def test_season_stat_line_def_pd_sum(self):
        """pd sums def_pass_defended."""
        weeks = [
            _week(week=1, def_pass_defended=2),
            _week(week=2, def_pass_defended=3),
        ]
        result = nflstats.season_stat_line(weeks, "LB")
        assert result["pd"] == 5

    def test_season_stat_line_def_ff_sum(self):
        """ff sums def_fumbles_forced."""
        weeks = [
            _week(week=1, def_fumbles_forced=1),
            _week(week=2, def_fumbles_forced=2),
        ]
        result = nflstats.season_stat_line(weeks, "LB")
        assert result["ff"] == 3

    def test_season_stat_line_def_td_sum(self):
        """def_td sums def_tds (NOT special_teams_tds)."""
        weeks = [
            _week(week=1, def_tds=1, special_teams_tds=0),
            _week(week=2, def_tds=0, special_teams_tds=1),
        ]
        result = nflstats.season_stat_line(weeks, "LB")
        assert result["def_td"] == 1, "only def_tds, not special_teams_tds"

    def test_season_stat_line_lb_snap_pct_defense_mean(self):
        """LB snap_pct is mean of defense_pct (defense groups use defense_pct)."""
        weeks = [
            _week(week=1, offense_snaps=None, defense_snaps=10),
        ]
        # need to modify the week to have defense_pct
        weeks[0]["defense_pct"] = 0.8
        weeks[0]["offense_pct"] = 0.1
        result = nflstats.season_stat_line(weeks, "LB")
        assert result["snap_pct"] == 0.8

    def test_season_stat_line_dl_snap_pct_defense_mean(self):
        """DL snap_pct is mean of defense_pct."""
        weeks = [_week(week=1, defense_snaps=10)]
        weeks[0]["defense_pct"] = 0.75
        weeks[0]["offense_pct"] = 0.05
        result = nflstats.season_stat_line(weeks, "DL")
        assert result["snap_pct"] == 0.75


class TestSeasonStatLineSnapPct:
    """snap_pct computation for offensive groups."""

    def test_season_stat_line_qb_snap_pct_offense_mean(self):
        """QB snap_pct is mean of offense_pct."""
        weeks = [
            _week(week=1, offense_snaps=10),
            _week(week=2, offense_snaps=10),
        ]
        weeks[0]["offense_pct"] = 1.0
        weeks[1]["offense_pct"] = 0.8
        result = nflstats.season_stat_line(weeks, "QB")
        assert result["snap_pct"] == 0.9

    def test_season_stat_line_rb_snap_pct_offense_mean(self):
        """RB snap_pct is mean of offense_pct."""
        weeks = [_week(week=1, offense_snaps=10)]
        weeks[0]["offense_pct"] = 0.5
        result = nflstats.season_stat_line(weeks, "RB")
        assert result["snap_pct"] == 0.5

    def test_season_stat_line_snap_pct_unplayed_week_excluded(self):
        """snap_pct only computed over played weeks."""
        weeks = [
            _week(week=1, offense_snaps=0, defense_snaps=0, fantasy_points_ppr=0),
            _week(week=2, offense_snaps=10),
        ]
        weeks[0]["offense_pct"] = 0.0
        weeks[1]["offense_pct"] = 0.8
        result = nflstats.season_stat_line(weeks, "QB")
        assert result["snap_pct"] == 0.8, "unplayed weeks excluded from snap_pct mean"


class TestSeasonStatLineOnly:
    """Only totals played weeks; unplayed weeks contribute nothing."""

    def test_season_stat_line_ignores_unplayed_weeks_in_totals(self):
        """Weeks with all snaps/points falsy don't contribute to totals."""
        weeks = [
            _week(week=1, offense_snaps=0, defense_snaps=0, fantasy_points_ppr=0,
                  passing_yards=999),
            _week(week=2, offense_snaps=10, passing_yards=100),
        ]
        result = nflstats.season_stat_line(weeks, "QB")
        assert result["pass_yds"] == 100, "unplayed week's yards ignored"
        assert result["gp"] == 1


# --- team_stat_line tests ---


class TestTeamStatLineStructure:
    """Every result has every stat key plus gp and snap_pct."""

    def test_team_stat_line_all_keys_present(self):
        """Result contains every STAT_COLUMNS key, gp, snap_pct."""
        weeks = [_team_week()]
        result = nflstats.team_stat_line(weeks)
        stat_keys = {k for k, *_ in nflstats.STAT_COLUMNS}
        for key in stat_keys:
            assert key in result, f"{key} missing from result"
        assert "gp" in result
        assert "snap_pct" in result

    def test_team_stat_line_empty_weeks_all_none(self):
        """Empty weeks returns all values None."""
        result = nflstats.team_stat_line([])
        stat_keys = {k for k, *_ in nflstats.STAT_COLUMNS}
        for key in stat_keys:
            assert result[key] is None, f"{key} should be None"
        assert result["gp"] is None
        assert result["snap_pct"] is None


class TestTeamStatLineNonDef:
    """Team stat line has no passing/rushing/rec/kick keys."""

    def test_team_stat_line_non_def_keys_none(self):
        """All non-def keys are None."""
        weeks = [_team_week()]
        result = nflstats.team_stat_line(weeks)
        assert result["pass_yds"] is None
        assert result["pass_td"] is None
        assert result["rush_yds"] is None
        assert result["tackles"] is not None  # def key


class TestTeamStatLineGp:
    """gp = len(weeks)."""

    def test_team_stat_line_gp_len_weeks(self):
        """gp equals number of weeks, not "played" count."""
        weeks = [_team_week(), _team_week(), _team_week()]
        result = nflstats.team_stat_line(weeks)
        assert result["gp"] == 3


class TestTeamStatLineDef:
    """Defensive stats summed; special_teams_tds included in def_td."""

    def test_team_stat_line_tackles_solo_only(self):
        """tackles = def_tackles_solo (no assists)."""
        weeks = [
            _team_week(week=1, def_tackles_solo=4, def_tackle_assists=2),
            _team_week(week=2, def_tackles_solo=3, def_tackle_assists=1),
        ]
        result = nflstats.team_stat_line(weeks)
        assert result["tackles"] == 7, "solo only, not assists"

    def test_team_stat_line_sacks_sum(self):
        """sacks sum preserving halves."""
        weeks = [
            _team_week(week=1, def_sacks=1.5),
            _team_week(week=2, def_sacks=0.5),
        ]
        result = nflstats.team_stat_line(weeks)
        assert result["sacks"] == 2.0

    def test_team_stat_line_def_int_sum(self):
        """def_int sums def_interceptions."""
        weeks = [
            _team_week(week=1, def_interceptions=1),
            _team_week(week=2, def_interceptions=2),
        ]
        result = nflstats.team_stat_line(weeks)
        assert result["def_int"] == 3

    def test_team_stat_line_def_td_includes_special_teams(self):
        """def_td = def_tds + special_teams_tds."""
        weeks = [
            _team_week(week=1, def_tds=1, special_teams_tds=0),
            _team_week(week=2, def_tds=0, special_teams_tds=1),
        ]
        result = nflstats.team_stat_line(weeks)
        assert result["def_td"] == 2, "includes both def_tds and special_teams_tds"

    def test_team_stat_line_snap_pct_none(self):
        """snap_pct is always None for team_stat_line."""
        weeks = [_team_week()]
        result = nflstats.team_stat_line(weeks)
        assert result["snap_pct"] is None


# --- attach_season_stats tests ---


class TestAttachSeasonStats:
    """Mutates rows with season stat lines joined from player_weeks/team_weeks."""

    def test_attach_season_stats_mutates_rows(self):
        """attach_season_stats mutates rows in place."""
        rows = [_season_row(player_id=1, group="QB")]
        player_weeks = [_week(gsis_id="g1")]
        id_map = {1: {"gsis_id": "g1"}}
        nflstats.attach_season_stats(rows, id_map, player_weeks, [])
        assert "pass_yds" in rows[0], "row has stat keys after attach"

    def test_attach_season_stats_joins_by_gsis_id(self):
        """Regular players join via id_map[player_id][gsis_id] to player_weeks."""
        rows = [_season_row(player_id=1, group="QB")]
        player_weeks = [_week(gsis_id="abc123", week=1, passing_yards=300)]
        id_map = {1: {"gsis_id": "abc123"}}
        nflstats.attach_season_stats(rows, id_map, player_weeks, [])
        assert rows[0]["pass_yds"] == 300

    def test_attach_season_stats_dst_joins_by_pro_team(self):
        """D/ST rows join by pro_team to team_weeks."""
        rows = [_season_row(player_id=2, group="D/ST", pro_team="KC")]
        team_weeks = [_team_week(team="KC", week=1, def_sacks=3.0)]
        nflstats.attach_season_stats(rows, {}, [], team_weeks)
        assert rows[0]["sacks"] == 3.0

    def test_attach_season_stats_no_mapping_gets_all_none(self):
        """Player with no id_map entry still gets all stat keys, all None."""
        rows = [_season_row(player_id=99, group="QB")]
        player_weeks = [_week(gsis_id="g1")]
        id_map = {1: {"gsis_id": "g1"}}  # 99 not mapped
        nflstats.attach_season_stats(rows, id_map, player_weeks, [])
        assert rows[0]["pass_yds"] is None
        assert rows[0]["gp"] is None

    def test_attach_season_stats_no_weeks_gets_all_none(self):
        """Player with mapping but no weeks still gets all keys, all None."""
        rows = [_season_row(player_id=1, group="QB")]
        player_weeks = [_week(gsis_id="g1")]
        id_map = {1: {"gsis_id": "different_id"}}  # mapped but no matching weeks
        nflstats.attach_season_stats(rows, id_map, player_weeks, [])
        assert rows[0]["pass_yds"] is None
        assert rows[0]["gp"] is None

    def test_attach_season_stats_multiple_weeks_same_player(self):
        """Multiple weeks for one player sum correctly."""
        rows = [_season_row(player_id=1, group="QB")]
        player_weeks = [
            _week(gsis_id="g1", week=1, passing_yards=250),
            _week(gsis_id="g1", week=2, passing_yards=300),
        ]
        id_map = {1: {"gsis_id": "g1"}}
        nflstats.attach_season_stats(rows, id_map, player_weeks, [])
        assert rows[0]["pass_yds"] == 550

    def test_attach_season_stats_two_players_never_mix(self):
        """Two different players' weeks never sum together."""
        rows = [
            _season_row(player_id=1, group="QB"),
            _season_row(player_id=2, group="QB"),
        ]
        player_weeks = [
            _week(gsis_id="g1", week=1, passing_yards=250),
            _week(gsis_id="g2", week=1, passing_yards=300),
        ]
        id_map = {1: {"gsis_id": "g1"}, 2: {"gsis_id": "g2"}}
        nflstats.attach_season_stats(rows, id_map, player_weeks, [])
        assert rows[0]["pass_yds"] == 250
        assert rows[1]["pass_yds"] == 300

    def test_attach_season_stats_empty_team_weeks_dst_all_none(self):
        """D/ST with no matching team_weeks gets all None."""
        rows = [_season_row(player_id=2, group="D/ST", pro_team="KC")]
        nflstats.attach_season_stats(rows, {}, [], [])
        assert rows[0]["sacks"] is None
        assert rows[0]["gp"] is None


# --- columns tests ---


class TestColumnsStructure:
    """columns() returns list of dicts with key, label, title, kind, desc, block."""

    def test_columns_each_is_dict_with_required_keys(self):
        """Each column dict has key, label, title, kind, desc, block."""
        cols = players.columns([], None, "", False)
        for col in cols:
            assert "key" in col
            assert "label" in col
            assert "title" in col
            assert "kind" in col
            assert "desc" in col
            assert "block" in col

    def test_columns_starts_with_name(self):
        """First column is always 'name'."""
        cols = players.columns([], None, "", False)
        assert cols[0]["key"] == "name"

    def test_columns_no_group_when_pos_set(self):
        """Group column omitted when pos is set."""
        cols_no_pos = players.columns([], None, "", False)
        cols_with_pos = players.columns([], None, "QB", False)
        keys_no_pos = {c["key"] for c in cols_no_pos}
        keys_with_pos = {c["key"] for c in cols_with_pos}
        assert "group" in keys_no_pos
        assert "group" not in keys_with_pos

    def test_columns_group_when_pos_not_set(self):
        """Group column included when pos is empty string."""
        cols = players.columns([], None, "", False)
        keys = {c["key"] for c in cols}
        assert "group" in keys

    def test_columns_identity_section_first(self):
        """Identity columns (name, group, pro_team, owner, percent_owned) come first."""
        cols = players.columns([], None, "", False)
        keys = [c["key"] for c in cols[:5]]
        assert keys[:3] == ["name", "group", "pro_team"]
        assert "owner" in keys
        assert "percent_owned" in keys


class TestColumnsPlayedWeeks:
    """w{n} columns for each played week, then total and avg."""

    def test_columns_no_played_weeks_no_w_columns(self):
        """No played weeks → no wN, total, or avg columns."""
        cols = players.columns([], None, "", False)
        keys = {c["key"] for c in cols}
        assert not any(k.startswith("w") for k in keys)
        assert "total" not in keys
        assert "avg" not in keys

    def test_columns_played_weeks_adds_wn_columns(self):
        """played=[1,2] adds w1, w2 columns."""
        cols = players.columns([1, 2], None, "", False)
        keys = [c["key"] for c in cols]
        assert "w1" in keys
        assert "w2" in keys

    def test_columns_played_weeks_adds_total_and_avg(self):
        """With played weeks, adds total and avg columns after weeks."""
        cols = players.columns([1, 2], None, "", False)
        keys = [c["key"] for c in cols]
        w1_idx = keys.index("w1")
        w2_idx = keys.index("w2")
        total_idx = keys.index("total")
        avg_idx = keys.index("avg")
        assert w1_idx < total_idx < avg_idx
        assert w2_idx < total_idx

    def test_columns_no_total_without_played_weeks(self):
        """No total/avg if no played weeks."""
        cols = players.columns([], None, "", False)
        keys = {c["key"] for c in cols}
        assert "total" not in keys
        assert "avg" not in keys


class TestColumnsNextWeek:
    """projected column only if next_week is set."""

    def test_columns_no_projected_when_next_week_none(self):
        """next_week=None → no projected column."""
        cols = players.columns([], None, "", False)
        keys = {c["key"] for c in cols}
        assert "projected" not in keys

    def test_columns_projected_when_next_week_set(self):
        """next_week=3 → projected column."""
        cols = players.columns([], 3, "", False)
        keys = {c["key"] for c in cols}
        assert "projected" in keys

    def test_columns_projected_label_includes_week(self):
        """Projected column label includes week number."""
        cols = players.columns([], 5, "", False)
        proj_col = next((c for c in cols if c["key"] == "projected"), None)
        assert "W5" in proj_col["label"]


class TestColumnsMatchups:
    """opp and rank columns only if matchups=True."""

    def test_columns_no_matchup_when_matchups_false(self):
        """matchups=False → no opp/rank columns."""
        cols = players.columns([], None, "", False)
        keys = {c["key"] for c in cols}
        assert "opp" not in keys
        assert "run_rank" not in keys
        assert "pass_rank" not in keys

    def test_columns_opp_and_ranks_when_matchups_true(self):
        """matchups=True → opp and all four rank columns."""
        cols = players.columns([], None, "", True)
        keys = {c["key"] for c in cols}
        assert "opp" in keys
        assert "run_rank" in keys
        assert "pass_rank" in keys
        assert "kick_rank" in keys
        assert "def_rank" in keys


class TestColumnsBlock:
    """block=True on first fantasy column, opp (if matchups), and gp."""

    def test_columns_block_on_first_fantasy_w1(self):
        """block=True on w1 when played weeks exist."""
        cols = players.columns([1, 2], None, "", False)
        w1_col = next((c for c in cols if c["key"] == "w1"), None)
        assert w1_col["block"] is True

    def test_columns_block_on_first_fantasy_projected_no_played(self):
        """block=True on projected when no played weeks but next_week."""
        cols = players.columns([], 3, "", False)
        proj_col = next((c for c in cols if c["key"] == "projected"), None)
        assert proj_col["block"] is True

    def test_columns_block_on_gp_always(self):
        """block=True on gp with or without played weeks."""
        cols_with_played = players.columns([1], None, "", False)
        cols_no_played = players.columns([], None, "", False)
        gp_col_with = next((c for c in cols_with_played if c["key"] == "gp"), None)
        gp_col_no = next((c for c in cols_no_played if c["key"] == "gp"), None)
        assert gp_col_with["block"] is True
        assert gp_col_no["block"] is True

    def test_columns_block_on_total_when_played(self):
        """block=False on total when played weeks exist (w1 has block=True)."""
        cols = players.columns([1, 2], None, "", False)
        total_col = next((c for c in cols if c["key"] == "total"), None)
        assert total_col["block"] is False

    def test_columns_block_on_opp_when_matchups(self):
        """block=True on opp when matchups=True."""
        cols = players.columns([], None, "", True)
        opp_col = next((c for c in cols if c["key"] == "opp"), None)
        assert opp_col["block"] is True

    def test_columns_block_on_gp(self):
        """block=True on gp."""
        cols = players.columns([], None, "", False)
        gp_col = next((c for c in cols if c["key"] == "gp"), None)
        assert gp_col["block"] is True


class TestColumnsKind:
    """Each column has the correct kind."""

    def test_columns_name_kind_player(self):
        """name column kind is 'player'."""
        cols = players.columns([], None, "", False)
        name_col = cols[0]
        assert name_col["kind"] == "player"

    def test_columns_text_columns_kind_text(self):
        """group, pro_team, owner kind is 'text'."""
        cols = players.columns([], None, "", False)
        by_key = {c["key"]: c for c in cols}
        for key in ["group", "pro_team", "owner"]:
            if key in by_key:
                assert by_key[key]["kind"] == "text"

    def test_columns_percent_owned_kind_own(self):
        """percent_owned kind is 'own'."""
        cols = players.columns([], None, "", False)
        po_col = next((c for c in cols if c["key"] == "percent_owned"), None)
        assert po_col["kind"] == "own"

    def test_columns_w_columns_kind_pts(self):
        """w{n} columns kind is 'pts'."""
        cols = players.columns([1, 2], None, "", False)
        w1_col = next((c for c in cols if c["key"] == "w1"), None)
        assert w1_col["kind"] == "pts"

    def test_columns_total_kind_total(self):
        """total column kind is 'total'."""
        cols = players.columns([1], None, "", False)
        total_col = next((c for c in cols if c["key"] == "total"), None)
        assert total_col["kind"] == "total"

    def test_columns_avg_kind_pts(self):
        """avg column kind is 'pts'."""
        cols = players.columns([1], None, "", False)
        avg_col = next((c for c in cols if c["key"] == "avg"), None)
        assert avg_col["kind"] == "pts"

    def test_columns_projected_kind_proj(self):
        """projected column kind is 'proj'."""
        cols = players.columns([], 1, "", False)
        proj_col = next((c for c in cols if c["key"] == "projected"), None)
        assert proj_col["kind"] == "proj"

    def test_columns_opp_kind_opp(self):
        """opp column kind is 'opp'."""
        cols = players.columns([], None, "", True)
        opp_col = next((c for c in cols if c["key"] == "opp"), None)
        assert opp_col["kind"] == "opp"

    def test_columns_rank_columns_kind_rank(self):
        """run_rank, pass_rank, kick_rank, def_rank kind is 'rank'."""
        cols = players.columns([], None, "", True)
        by_key = {c["key"]: c for c in cols}
        for key in ["run_rank", "pass_rank", "kick_rank", "def_rank"]:
            assert by_key[key]["kind"] == "rank"

    def test_columns_gp_kind_int(self):
        """gp column kind is 'int'."""
        cols = players.columns([], None, "", False)
        gp_col = next((c for c in cols if c["key"] == "gp"), None)
        assert gp_col["kind"] == "int"

    def test_columns_snap_pct_kind_pct(self):
        """snap_pct column kind is 'pct'."""
        cols = players.columns([], None, "QB", False)
        snap_col = next((c for c in cols if c["key"] == "snap_pct"), None)
        assert snap_col["kind"] == "pct"

    def test_columns_tgt_share_kind_pct(self):
        """tgt_share column kind is 'pct'."""
        cols = players.columns([], None, "RB", False)
        tgt_col = next((c for c in cols if c["key"] == "tgt_share"), None)
        assert tgt_col["kind"] == "pct"

    def test_columns_stat_columns_kind_int_except_tgt_share(self):
        """Stat columns (except tgt_share) kind is 'int'."""
        cols = players.columns([], None, "QB", False)
        by_key = {c["key"]: c for c in cols}
        for key in ["pass_yds", "pass_td", "carries"]:
            if key in by_key:
                assert by_key[key]["kind"] == "int"


class TestColumnsDesc:
    """desc default: text/player/opp/rank False; numeric else True."""

    def test_columns_name_desc_false(self):
        """name desc is False."""
        cols = players.columns([], None, "", False)
        name_col = cols[0]
        assert name_col["desc"] is False

    def test_columns_text_desc_false(self):
        """text columns desc is False."""
        cols = players.columns([], None, "", False)
        by_key = {c["key"]: c for c in cols}
        for key in ["group", "pro_team", "owner"]:
            if key in by_key:
                assert by_key[key]["desc"] is False

    def test_columns_opp_desc_false(self):
        """opp desc is False."""
        cols = players.columns([], None, "", True)
        opp_col = next((c for c in cols if c["key"] == "opp"), None)
        assert opp_col["desc"] is False

    def test_columns_rank_desc_false(self):
        """rank columns desc is False."""
        cols = players.columns([], None, "", True)
        by_key = {c["key"]: c for c in cols}
        for key in ["run_rank", "pass_rank", "kick_rank", "def_rank"]:
            assert by_key[key]["desc"] is False

    def test_columns_numeric_desc_true(self):
        """Numeric columns desc is True (e.g., w1, total, gp)."""
        cols = players.columns([1], None, "", False)
        by_key = {c["key"]: c for c in cols}
        for key in ["w1", "total", "gp", "percent_owned"]:
            assert by_key[key]["desc"] is True

    def test_columns_snap_pct_desc_true(self):
        """snap_pct desc is True."""
        cols = players.columns([], None, "QB", False)
        snap_col = next((c for c in cols if c["key"] == "snap_pct"), None)
        assert snap_col["desc"] is True

    def test_columns_stat_keys_desc_true(self):
        """Stat keys desc is True."""
        cols = players.columns([], None, "QB", False)
        by_key = {c["key"]: c for c in cols}
        for key in ["pass_yds", "pass_td"]:
            assert by_key[key]["desc"] is True


class TestColumnsStatFamilies:
    """Stat columns filtered by GROUP_FAMILIES[pos] or all FAMILY_ORDER if pos=""."""

    def test_columns_pos_empty_all_families(self):
        """pos="" includes all families in FAMILY_ORDER."""
        cols = players.columns([], None, "", False)
        keys = {c["key"] for c in cols}
        for key, _, _, family in nflstats.STAT_COLUMNS:
            assert key in keys

    def test_columns_pos_qb_pass_rush_only(self):
        """pos='QB' includes pass and rush keys only."""
        cols = players.columns([], None, "QB", False)
        keys = {c["key"] for c in cols}
        # QB should have pass and rush
        assert "pass_yds" in keys
        assert "carries" in keys
        # QB should not have rec or kick
        assert "receptions" not in keys
        assert "fgm" not in keys

    def test_columns_pos_rb_rush_rec_only(self):
        """pos='RB' includes rush and rec keys only."""
        cols = players.columns([], None, "RB", False)
        keys = {c["key"] for c in cols}
        assert "carries" in keys
        assert "receptions" in keys
        assert "pass_yds" not in keys

    def test_columns_pos_k_kick_only(self):
        """pos='K' includes kick keys only."""
        cols = players.columns([], None, "K", False)
        keys = {c["key"] for c in cols}
        assert "fgm" in keys
        assert "carries" not in keys

    def test_columns_pos_k_no_snap_pct(self):
        """pos='K' omits snap_pct (K in NO_SNAP_GROUPS)."""
        cols = players.columns([], None, "K", False)
        keys = {c["key"] for c in cols}
        assert "snap_pct" not in keys

    def test_columns_pos_dst_no_snap_pct(self):
        """pos='D/ST' omits snap_pct (D/ST in NO_SNAP_GROUPS)."""
        cols = players.columns([], None, "D/ST", False)
        keys = {c["key"] for c in cols}
        assert "snap_pct" not in keys


# --- resolve_sort tests ---


class TestResolveSortValid:
    """Valid sort keys resolve to themselves."""

    def test_resolve_sort_valid_key_in_cols(self):
        """Valid key in cols resolves to that key."""
        cols = players.columns([1], None, "QB", False)
        key, desc = players.resolve_sort("pass_yds", "desc", cols, [1], False)
        assert key == "pass_yds"

    def test_resolve_sort_proj_alias(self):
        """'proj' is legacy alias for 'projected'."""
        cols = players.columns([], 1, "", False)
        key, desc = players.resolve_sort("proj", "desc", cols, [], True)
        assert key == "projected"


class TestResolveSortInvalid:
    """Invalid/absent/empty sort keys fall back to total/projected/name."""

    def test_resolve_sort_invalid_key_default_total_if_played(self):
        """Invalid key with played weeks → 'total'."""
        cols = players.columns([1], None, "QB", False)
        key, desc = players.resolve_sort("bogus", "desc", cols, [1], False)
        assert key == "total"

    def test_resolve_sort_absent_key_default_total_if_played(self):
        """Absent sort key with played weeks → 'total'."""
        cols = players.columns([1], None, "QB", False)
        key, desc = players.resolve_sort(None, "desc", cols, [1], False)
        assert key == "total"

    def test_resolve_sort_invalid_key_default_projected_if_next_no_played(self):
        """Invalid key, no played weeks, has next → 'projected'."""
        cols = players.columns([], 2, "", False)
        key, desc = players.resolve_sort("bogus", "desc", cols, [], True)
        assert key == "projected"

    def test_resolve_sort_invalid_key_default_name_if_no_played_no_next(self):
        """Invalid key, no played, no next → 'name'."""
        cols = players.columns([], None, "", False)
        key, desc = players.resolve_sort("bogus", "desc", cols, [], False)
        assert key == "name"

    def test_resolve_sort_stat_key_not_in_cols_uses_default(self):
        """Stat key not in current cols (e.g. rush_yds when pos=K) → default."""
        cols = players.columns([1], None, "K", False)
        key, desc = players.resolve_sort("rush_yds", "desc", cols, [1], False)
        assert key == "total", "stat not in cols falls to default"


class TestResolveSortDirection:
    """Direction param mapped: 'desc' → True, 'asc' → False, else default."""

    def test_resolve_sort_direction_desc(self):
        """direction='desc' → True."""
        cols = players.columns([1], None, "", False)
        key, desc = players.resolve_sort("total", "desc", cols, [1], False)
        assert desc is True

    def test_resolve_sort_direction_asc(self):
        """direction='asc' → False."""
        cols = players.columns([1], None, "", False)
        key, desc = players.resolve_sort("total", "asc", cols, [1], False)
        assert desc is False

    def test_resolve_sort_direction_none_uses_default(self):
        """direction=None → uses column default."""
        cols = players.columns([1], None, "", False)
        key, desc = players.resolve_sort("total", None, cols, [1], False)
        assert desc is True, "total default desc is True"

    def test_resolve_sort_direction_invalid_uses_default(self):
        """direction='invalid' → uses column default."""
        cols = players.columns([1], None, "", False)
        key, desc = players.resolve_sort("total", "invalid", cols, [1], False)
        assert desc is True, "falls back to column default"

    def test_resolve_sort_direction_text_column_default_false(self):
        """Text column (e.g. name) default desc is False."""
        cols = players.columns([1], None, "", False)
        key, desc = players.resolve_sort("name", None, cols, [1], False)
        assert desc is False


# --- sort_rows tests ---


class TestSortRowsNumeric:
    """Numeric sorts work in both directions."""

    def test_sort_rows_numeric_descending(self):
        """Numeric descending: 5, 3, 1."""
        rows = [
            {"name": "A", "pass_yds": 100},
            {"name": "B", "pass_yds": 300},
            {"name": "C", "pass_yds": 200},
        ]
        sorted_rows = players.sort_rows(rows, "pass_yds", True)
        assert [r["name"] for r in sorted_rows] == ["B", "C", "A"]

    def test_sort_rows_numeric_ascending(self):
        """Numeric ascending: 1, 3, 5."""
        rows = [
            {"name": "A", "pass_yds": 100},
            {"name": "B", "pass_yds": 300},
            {"name": "C", "pass_yds": 200},
        ]
        sorted_rows = players.sort_rows(rows, "pass_yds", False)
        assert [r["name"] for r in sorted_rows] == ["A", "C", "B"]

    def test_sort_rows_numeric_negative_ordered_correctly(self):
        """Negative numbers ordered correctly."""
        rows = [
            {"name": "A", "pass_yds": -10},
            {"name": "B", "pass_yds": 5},
            {"name": "C", "pass_yds": -20},
        ]
        sorted_rows = players.sort_rows(rows, "pass_yds", True)
        assert [r["name"] for r in sorted_rows] == ["B", "A", "C"]


class TestSortRowsMissing:
    """Missing values (None or empty string) LAST in both directions."""

    def test_sort_rows_none_last_descending(self):
        """None values last even in descending sort."""
        rows = [
            {"name": "A", "pass_yds": None},
            {"name": "B", "pass_yds": 300},
            {"name": "C", "pass_yds": 200},
        ]
        sorted_rows = players.sort_rows(rows, "pass_yds", True)
        assert [r["name"] for r in sorted_rows] == ["B", "C", "A"]

    def test_sort_rows_none_last_ascending(self):
        """None values last even in ascending sort."""
        rows = [
            {"name": "A", "pass_yds": None},
            {"name": "B", "pass_yds": 100},
            {"name": "C", "pass_yds": 200},
        ]
        sorted_rows = players.sort_rows(rows, "pass_yds", False)
        assert [r["name"] for r in sorted_rows] == ["B", "C", "A"]

    def test_sort_rows_empty_string_last(self):
        """Empty string treated as missing, last."""
        rows = [
            {"name": "A", "owner": ""},
            {"name": "B", "owner": "Team1"},
            {"name": "C", "owner": "Team2"},
        ]
        sorted_rows = players.sort_rows(rows, "owner", True)
        assert [r["name"] for r in sorted_rows[-1:]] == ["A"]


class TestSortRowsText:
    """Text sorts case-insensitively."""

    def test_sort_rows_text_case_insensitive(self):
        """Text comparison is case-insensitive."""
        rows = [
            {"name": "alice"},
            {"name": "Aaron"},
            {"name": "bob"},
        ]
        sorted_rows = players.sort_rows(rows, "name", False)
        names = [r["name"] for r in sorted_rows]
        assert names == ["Aaron", "alice", "bob"]

    def test_sort_rows_text_descending_case_insensitive(self):
        """Text descending, case-insensitive."""
        rows = [
            {"name": "alice"},
            {"name": "Aaron"},
            {"name": "bob"},
        ]
        sorted_rows = players.sort_rows(rows, "name", True)
        names = [r["name"] for r in sorted_rows]
        assert names == ["bob", "alice", "Aaron"]


class TestSortRowsGroup:
    """Group sorts by POSITION_GROUPS order, not alphabetically."""

    def test_sort_rows_group_position_groups_order(self):
        """Group sorts by POSITION_GROUPS order."""
        rows = [
            {"name": "K1", "group": "K"},
            {"name": "QB1", "group": "QB"},
            {"name": "RB1", "group": "RB"},
        ]
        sorted_rows = players.sort_rows(rows, "group", False)
        names = [r["name"] for r in sorted_rows]
        assert names == ["QB1", "RB1", "K1"]

    def test_sort_rows_group_descending_reverses_order(self):
        """Group descending reverses POSITION_GROUPS order."""
        rows = [
            {"name": "QB1", "group": "QB"},
            {"name": "RB1", "group": "RB"},
            {"name": "K1", "group": "K"},
        ]
        sorted_rows = players.sort_rows(rows, "group", True)
        names = [r["name"] for r in sorted_rows]
        assert names == ["K1", "RB1", "QB1"]


class TestSortRowsMatchup:
    """opp and *_rank read from row["matchup"]."""

    def test_sort_rows_opp_from_matchup(self):
        """opp key reads from row['matchup']['opponent']."""
        rows = [
            {"name": "A", "matchup": {"opponent": "KC"}},
            {"name": "B", "matchup": {"opponent": "BUF"}},
        ]
        sorted_rows = players.sort_rows(rows, "opp", False)
        # BUF before KC alphabetically
        assert [r["name"] for r in sorted_rows] == ["B", "A"]

    def test_sort_rows_rank_from_matchup(self):
        """rank keys read from row['matchup']['run_rank'] etc."""
        rows = [
            {"name": "A", "matchup": {"run_rank": 10}},
            {"name": "B", "matchup": {"run_rank": 5}},
        ]
        sorted_rows = players.sort_rows(rows, "run_rank", False)
        assert [r["name"] for r in sorted_rows] == ["B", "A"]

    def test_sort_rows_matchup_missing_sorts_last(self):
        """Row without matchup sorts last."""
        rows = [
            {"name": "A", "matchup": {"opponent": "KC"}},
            {"name": "B", "matchup": None},
        ]
        sorted_rows = players.sort_rows(rows, "opp", False)
        assert [r["name"] for r in sorted_rows] == ["A", "B"]

    def test_sort_rows_no_matchup_key_sorts_last(self):
        """Row without matchup key at all sorts last."""
        rows = [
            {"name": "A", "matchup": {"opponent": "KC"}},
            {"name": "B"},
        ]
        sorted_rows = players.sort_rows(rows, "opp", False)
        assert [r["name"] for r in sorted_rows] == ["A", "B"]


class TestSortRowsTiebreak:
    """Ties break on projected (desc, None last) then name (asc); tie-break never flips."""

    def test_sort_rows_ties_break_projected_descending(self):
        """Tied on sort key, higher projected wins."""
        rows = [
            {"name": "A", "pass_yds": 100, "projected": 10},
            {"name": "B", "pass_yds": 100, "projected": 20},
        ]
        sorted_rows = players.sort_rows(rows, "pass_yds", False)
        assert [r["name"] for r in sorted_rows] == ["B", "A"]

    def test_sort_rows_ties_break_projected_even_when_desc_true(self):
        """Tied, projected tie-break is descending even when sort desc=True."""
        rows = [
            {"name": "A", "pass_yds": 100, "projected": 10},
            {"name": "B", "pass_yds": 100, "projected": 20},
        ]
        sorted_rows = players.sort_rows(rows, "pass_yds", True)
        # Even though sorting pass_yds desc, projected tie-break is still desc (B > A)
        assert [r["name"] for r in sorted_rows] == ["B", "A"]

    def test_sort_rows_ties_break_projected_none_last(self):
        """Tied on sort key and projected is None, name ascending wins."""
        rows = [
            {"name": "A", "pass_yds": 100, "projected": 10},
            {"name": "B", "pass_yds": 100, "projected": None},
            {"name": "C", "pass_yds": 100, "projected": 5},
        ]
        sorted_rows = players.sort_rows(rows, "pass_yds", False)
        # A, C, B: A highest projected, C next, B has None so last
        assert [r["name"] for r in sorted_rows] == ["A", "C", "B"]

    def test_sort_rows_ties_break_name_ascending(self):
        """Tied on sort key and projected, name ascending wins."""
        rows = [
            {"name": "Zebra", "pass_yds": 100, "projected": 5},
            {"name": "Apple", "pass_yds": 100, "projected": 5},
        ]
        sorted_rows = players.sort_rows(rows, "pass_yds", False)
        assert [r["name"] for r in sorted_rows] == ["Apple", "Zebra"]

    def test_sort_rows_ties_break_name_ascending_even_when_sort_desc(self):
        """Name tie-break is ascending even when sort desc=True."""
        rows = [
            {"name": "Zebra", "pass_yds": 100, "projected": 5},
            {"name": "Apple", "pass_yds": 100, "projected": 5},
        ]
        sorted_rows = players.sort_rows(rows, "pass_yds", True)
        # Name tie-break is ascending (Apple before Zebra) even though desc=True
        assert [r["name"] for r in sorted_rows] == ["Apple", "Zebra"]

    def test_sort_rows_all_missing_follow_tiebreak(self):
        """Missing rows among themselves also follow tie-break."""
        rows = [
            {"name": "Zebra", "pass_yds": None, "projected": 5},
            {"name": "Apple", "pass_yds": None, "projected": 5},
        ]
        sorted_rows = players.sort_rows(rows, "pass_yds", False)
        # Both missing, so tie-break applies: Apple < Zebra alphabetically
        assert [r["name"] for r in sorted_rows] == ["Apple", "Zebra"]


class TestSortRowsNotMutating:
    """sort_rows returns new list, does not mutate input."""

    def test_sort_rows_returns_new_list(self):
        """Returns new list, not the same object."""
        rows = [
            {"name": "C", "pass_yds": 100},
            {"name": "A", "pass_yds": 200},
        ]
        sorted_rows = players.sort_rows(rows, "pass_yds", False)
        assert sorted_rows is not rows

    def test_sort_rows_does_not_mutate_input(self):
        """Input list unchanged."""
        rows = [
            {"name": "C", "pass_yds": 100},
            {"name": "A", "pass_yds": 200},
        ]
        original_order = [r["name"] for r in rows]
        players.sort_rows(rows, "pass_yds", False)
        assert [r["name"] for r in rows] == original_order

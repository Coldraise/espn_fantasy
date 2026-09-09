#!/usr/bin/env python3
"""Backfill league history and week-level data.

    docker compose run --rm fantasy python scripts/backfill.py [season ...]
    docker compose run --rm fantasy python scripts/backfill.py --nflverse [season ...]

Two different grains, because ESPN serves two:

  * History (past seasons) -- season TOTALS only: W-L-T, points for/against,
    final rank. One request covers every season. No weekly schedule is
    retrievable for them by any means, so head-to-head, all-play and luck
    necessarily start from the current season.
  * Current season -- full weekly matchups, which is what those views need.

An empty head-to-head matrix before the season starts is therefore correct
behaviour, not a failed backfill.

`--nflverse` loads NFL player-week data instead, from nflverse's GitHub
releases. It shares nothing with the ESPN path: no cookies, no league id, and
seasons going back as far as nflverse publishes them.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, espn_client, poller  # noqa: E402
from app.config import ConfigError, get_config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("backfill")


def backfill_nflverse(cfg, seasons: list[int]) -> int:
    """Load NFL weekly stats. Needs no ESPN credentials at all."""
    from app import nflverse  # noqa: PLC0415 - only needed on this path

    total = 0
    with db.session(cfg.db_path) as conn:
        db.init_db(conn)
        run_id = db.start_poll_run(conn, "backfill")
        try:
            total += poller.sync_nfl_ids(conn, force=True)
            for season in seasons:
                try:
                    total += poller.sync_nflverse(conn, season, force=True)
                    total += poller.sync_nfl_pbp(conn, season, force=True)
                except nflverse.NotPublished:
                    log.info("%s: nflverse has not published this season yet", season)
            db.finish_poll_run(conn, run_id, "ok", total)
        except Exception as exc:  # noqa: BLE001
            db.finish_poll_run(conn, run_id, "error", 0, str(exc))
            log.error("nflverse backfill failed: %s", exc)
            return 1
        log.info("nflverse: %d row(s) written, seasons stored %s",
                 total, db.nfl_seasons(conn))
    return 0


def main(argv: list[str]) -> int:
    try:
        cfg = get_config()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if "--nflverse" in argv:
        rest = [a for a in argv if a != "--nflverse"]
        seasons = [int(a) for a in rest] or [cfg.current_season, cfg.current_season - 1]
        return backfill_nflverse(cfg, seasons)

    if not cfg.has_credentials:
        log.error("ESPN_S2 / SWID are not set. Fill in config/secrets.env first.")
        return 2

    total, failures = 0, []
    with db.session(cfg.db_path) as conn:
        db.init_db(conn)

        # 1. History: one request, every past season, season totals.
        run_id = db.start_poll_run(conn, "backfill")
        try:
            written = poller.sync_history(conn)
            db.finish_poll_run(conn, run_id, "ok", written)
            stored = db.history_seasons(conn)
            total += written
            log.info("history: %d row(s) written, seasons %s (season totals only)",
                     written, stored)
        except espn_client.AuthInvalid as exc:
            db.finish_poll_run(conn, run_id, "error", 0, str(exc))
            log.error("auth rejected — refresh cookies in config/secrets.env. Stopping.")
            return 2
        except Exception as exc:  # noqa: BLE001
            db.finish_poll_run(conn, run_id, "error", 0, str(exc))
            log.warning("history unavailable: %s", exc)

        # 2. Week-level data. Only seasons ESPN serves a schedule for.
        seasons = [int(a) for a in argv] if argv else [cfg.current_season]
        log.info("week-level backfill for: %s", seasons)
        for season in seasons:
            run_id = db.start_poll_run(conn, "backfill")
            try:
                changed = poller.poll_season(conn, season, refresh=True)
                db.finish_poll_run(conn, run_id, "ok", changed)
                total += changed
                stored = conn.execute(
                    "SELECT COUNT(*) FROM team_weeks WHERE season=?", (season,)
                ).fetchone()[0]
                log.info("%s: %d row(s) written, %d stored", season, changed, stored)
            except espn_client.AuthInvalid as exc:
                db.finish_poll_run(conn, run_id, "error", 0, str(exc))
                log.error("auth rejected — refresh cookies in config/secrets.env. Stopping.")
                return 2
            except Exception as exc:  # noqa: BLE001
                db.finish_poll_run(conn, run_id, "error", 0, str(exc))
                failures.append(season)
                log.warning("%s: skipped (%s)", season, exc)

        weeks = conn.execute("SELECT COUNT(*) FROM team_weeks").fetchone()[0]
        hist = conn.execute("SELECT COUNT(*) FROM team_seasons").fetchone()[0]

    log.info("done: %d row(s) written. team_seasons=%d team_weeks=%d", total, hist, weeks)
    if failures:
        log.warning("no weekly data for: %s", failures)
    log.info("note: past seasons are season-totals only — ESPN serves no weekly "
             "schedule for them, so head-to-head / all-play / luck / schedule "
             "swap begin with %s.", cfg.current_season)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

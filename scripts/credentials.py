#!/usr/bin/env python3
"""List and reset the per-franchise logins.

    docker compose exec fantasy python scripts/credentials.py list
    docker compose exec fantasy python scripts/credentials.py reset "Juhu"
    docker compose exec fantasy python scripts/credentials.py reset --all

Passwords are stored only as PBKDF2 digests, so a forgotten one cannot be
recovered -- `reset` mints a new one and prints it once. That print is the only
place it ever appears; hand it over and it is gone from this process.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, db  # noqa: E402
from app.config import get_config  # noqa: E402


def _seed(conn, cfg) -> list[dict]:
    franchises = {int(t["team_id"]): t["name"] for t in db.fetch_teams(conn, cfg.current_season)}
    return auth.seed_accounts(conn, franchises) if franchises else []


def cmd_list(conn, cfg, _args) -> int:
    created = _seed(conn, cfg)
    for row in created:
        print(f"created  {row['username']}  {row['password']}")
    users = db.fetch_franchise_users(conn)
    if not users:
        print("No franchises stored yet -- run a poll or scripts/backfill.py first.")
        return 1
    width = max(len(u["username"]) for u in users)
    print(f"{'FRANCHISE'.ljust(width)}  TEAM  LAST LOGIN")
    for user in users:
        print(f"{user['username'].ljust(width)}  {str(user['team_id']).rjust(4)}  "
              f"{user['last_login_at'] or 'never'}")
    return 0


def cmd_reset(conn, cfg, args) -> int:
    _seed(conn, cfg)
    if args.all:
        targets = db.fetch_franchise_users(conn)
    else:
        user = db.find_franchise_user(conn, auth.normalize_username(args.franchise or ""))
        if user is None:
            print(f"No franchise called {args.franchise!r}. Try: credentials.py list")
            return 1
        targets = [user]

    for user in targets:
        password = auth.generate_password()
        db.set_franchise_password(conn, int(user["team_id"]), auth.hash_password(password))
        print(f"{user['username']}\t{password}")
    print(f"\n{len(targets)} password(s) reset. Any open session for them is now signed out.",
          file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="show every franchise login, seeding any that are missing")
    reset = sub.add_parser("reset", help="issue a new random password")
    reset.add_argument("franchise", nargs="?", help="franchise name, as shown by `list`")
    reset.add_argument("--all", action="store_true", help="reset every franchise")
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 2
    if args.command == "reset" and not args.all and not args.franchise:
        reset.error("give a franchise name or --all")

    cfg = get_config()
    with db.session(cfg.db_path) as conn:
        db.init_db(conn)
        return (cmd_list if args.command == "list" else cmd_reset)(conn, cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())

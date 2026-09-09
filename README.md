# League Monitor

A private site for one ESPN fantasy football league: a live scoreboard, a
durable local history, and the rivalry math ESPN never shows you.

- **Scoreboard** — live scores during games, refreshed in place. Your own
  franchise's matchup leads, centred and full size, with both complete starting
  lineups; the rest of the week follows in a compact grid showing each team's
  top three. Every lineup row is tinted in the player's NFL team colour, and
  names it too wherever the row is wide enough for both that and a kickoff.
  Each player's number shows a faded
  projection before kickoff, light green while their game is live, and bold
  white once it is final; a 25+ point week burns. Kickoff times are labelled
  with the day the game is played on (`Th`, `Su`) and the time in your clock
  (`2:35`, `19:00`). Out and Questionable players wear badges. Tapping any
  matchup opens it over a darkened page with the full roster — starters, bench,
  and injured reserve. Before the season starts this becomes a
  **pre-season hub**: draft countdown, managers, week-1 matchups and the full
  schedule grid. Your franchise is highlighted wherever it appears in a table
  or grid — schedule, all-time, standings, head-to-head, draft board.
- **All-time** — franchise records across every season ESPN has, with titles,
  average finish, best and worst finish, points for per season, season span and
  playoff seed — a season-by-season grid.
- **Standings** — real record beside **all-play** and **luck**, plus power
  rankings (all-play win pct, scoring rate, recent form), consistency metrics,
  projection bias, and a **lineup calls** table showing points left on the bench
  for each team. Best and worst week per franchise.
- **Head-to-head** — W-L matrix between every pair of teams.
- **Players** — every player per lineup position for a given week, each showing
  last week's actual score beside this week's projection, with an
  "available only" filter and rookie badges. Choosing a single position adds
  opponent-matchup columns to each row: the NFL team faced, then four league
  ranks — **Run**, **Pass**, **Kick** and **Def**, where 1 is softest and 32
  is toughest, with the governing rank highlighted. Run, Pass and Kick rank a
  defence on what it concedes per game; **Def** rates the opponent's *offence*
  on the fumbles, interceptions, sacks and tackles it gives up, which is what a
  fantasy defence scores on. Hover a rank to see the per-game stats behind it. Ranks come from the most recent season nflverse
  has published, which before the current season's week 1 is played is last
  year's — the page says so when that is the case. A phone shows only the
  governing column. Works before the draft.
- **Draft** — the board once ESPN marks the draft complete, with **value
  analysis** (who drafted best, best and worst value picks, positional runs),
  round.pick and auction bid displayed per cell.
- **NFL usage** — per-player real NFL usage for every rostered player: snap
  share, target share, air-yards share, WOPR, touches, IDP columns (tackles,
  sacks, INT+PD), boom/bust/floor/ceiling from the weekly points distribution,
  and targets and carries broken out by down (1st/2nd/3rd/4th). Organised per
  franchise with a section each (yours first and highlighted); a **roster
  strength** table summarises the league below. A range selector shows one week,
  the last four weeks, or the full season. The franchise name in the top corner
  links to your own roster section. Falls back to the last completed season when
  the current one has no games.
- **Activity** — league transactions (adds, waiver claims, drops, trades) with a
  churn table per franchise (moves, FAAB spent).
- **News** — reporter-attributed NFL news from ESPN's public feed, with roster
  hits from your franchise and the rest of the league floating to the top, and
  filterable by reporter name client-side.
- **League rules** — every ESPN setting in plain language, with instant search.

Navigation is a slide-out side menu behind the hamburger button (Escape or a
click outside closes it).

All configuration and state live **outside** the image, bind-mounted in, so
moving to another host is copy-the-directory-and-up.

## Setup

```bash
cp config/config.yml.example  config/config.yml
cp config/secrets.env.example config/secrets.env
cp .env.example .env                    # optional: HOST_PORT
chmod 600 config/secrets.env
```

Edit `config/config.yml` with your league id. Leave `seasons: auto` — the real
season list is read from ESPN at runtime. Then get your cookies:
log in at fantasy.espn.com, open devtools → Application → Cookies →
`https://fantasy.espn.com`, and copy `espn_s2` and `SWID` into
`config/secrets.env`. Paste them raw — do not quote them or URL-decode `espn_s2`.

Your league id is in the URL of your league page (`...?leagueId=XXXXXXX`).

```bash
mkdir -p data && sudo chown -R 1000:1000 data   # Linux hosts only
docker compose up -d
docker compose run --rm fantasy python scripts/backfill.py
```

Then open http://localhost:8080 (or your `HOST_PORT`) and sign in — see below
for where the first passwords come from.

## Logins

Every page except `/health`, `/login`, `/logout` and `/static/*` needs a
session. There is one account per franchise and no signup: the username *is*
the team's current name (matched ignoring case, spacing and accents), and the
password is machine-generated — nobody picks one.

Accounts are seeded from the current season's teams at startup, and again every
time the login page is loaded, so a fresh database that only learns its teams on
the first poll does not need a restart to hand out logins. Each new password is
printed once to the log and appended to `data/credentials.txt` (mode 0600, in
the already-gitignored `data/` directory). Hand them out, then delete that file.

Passwords are stored only as PBKDF2-SHA256 digests, so a forgotten one can be
**reset but never recovered**:

```bash
docker compose exec fantasy python scripts/credentials.py list
docker compose exec fantasy python scripts/credentials.py reset "Juhu"
```

A reset also signs that franchise out everywhere — the session cookie carries a
fingerprint of the stored digest, so every cookie issued against the old
password stops verifying.

The session is a `fl_session` cookie signed with HMAC-SHA256 (30 days, HttpOnly,
SameSite=Lax, `Secure` only over HTTPS) — stdlib only, because a session library
for eight users would buy nothing. It is signed with `SECRET_KEY` if that is set
in the environment, otherwise with a key generated once and kept in the
`league_meta` table; persisting it is what stops a deploy from silently signing
everyone out.

If a franchise renames itself in ESPN, its login follows the new name and keeps
the same password — the name people will type is whatever the site now calls
their team.

## Operating it

| Task | Command |
|---|---|
| Start / apply any config change | `docker compose up -d` |
| Backfill history | `docker compose run --rm fantasy python scripts/backfill.py` |
| Backfill one season's weeks | `... scripts/backfill.py 2026` |
| Backfill NFL data | `... scripts/backfill.py --nflverse [season ...]` |
| List logins and last-seen times | `docker compose exec fantasy python scripts/credentials.py list` |
| Reset a password | `... scripts/credentials.py reset "<franchise>"` (or `--all`) |
| Logs | `docker compose logs -f` |
| Health | `curl localhost:8080/health` |

### When ESPN stops accepting your cookies

This is the failure you should expect. `espn_s2` and `SWID` have no refresh
flow — they expire roughly annually and die immediately on a password change or
logout. The site keeps serving the last known-good data and shows a red banner.

To recover: pull fresh cookies, update `config/secrets.env`, then

```bash
docker compose up -d
```

**Not `docker compose restart`** — restart reuses the container's existing
environment and never re-reads `env_file`, so the old cookies stay loaded and
nothing appears to change. No image rebuild is needed either way.

`/health` reports `degraded` with a `reasons` array whenever credentials are
missing, cookies are rejected, or the last poll failed. It always returns HTTP
200: restarting the container cannot fix an expired cookie, so failing the
healthcheck would only cause pointless restarts.

## Configuration

`config/config.yml` — league id, seasons (`auto` recommended), poll cadence,
live windows, `display_timezone`, and an optional `news:` block.

Two timezones, deliberately separate: **live-window polling is always
America/New_York**, because that is when NFL games are played, while
`display_timezone` (default `Europe/Budapest`) controls times shown to you —
the draft countdown and "updated N minutes ago". Changing one never affects the
other.

The `news:` block is optional; if omitted, the whole config.yml still works and
you get the defaults. `reporters` is a list of byline names to offer as filter
chips on `/news` (default: Adam Schefter, Jeremy Fowler, Dan Graziano, Mike
Clay, Field Yates, ESPN Fantasy). `keep_days` controls how long articles are
stored (default: 21). The fetch itself stores every article regardless of
reporters — the page does the filtering.

`config/secrets.env` — `ESPN_S2` and `SWID` only.

`.env` (repo root) — `HOST_PORT`, read by Compose itself, not the container.

## Notes on the data

ESPN has no official API; everything here is built on the endpoints its own web
app uses, wrapped in `app/espn_client.py` so breakage stays in one place.

- **The NFL game state comes from the public scoreboard endpoint, not the fantasy
  API.** `site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard` returns
  `status.type.state` as `pre`/`in`/`post`, the quarter and clock, and kickoff
  times, all without needing the fantasy session cookies. This is the same
  reasoning already written for the NFL roster and news endpoints — kickoff times
  and live/final state survive expired cookies, so the scoreboard stays useful
  when authentication fails.
- **A kickoff is shown with the NFL's day and the reader's clock.** Thursday
  Night Football kicks off 20:35 ET, which is 02:35 Friday in Budapest. It is
  labelled `Th 2:35`, not `Fr 2:35`, because the day names the game everyone
  talks about while the clock has to be the one the reader lives in. This is a
  third position in the two-timezone split that `app/config.py` and
  `app/nflweeks.py` already describe: `display_timezone` handles the time, a
  constant `LEAGUE_TZ` handles the day.
- **The injury report is `player.injuryStatus`, not the roster entry's.** Both
  keys exist on every matchup entry. The entry's describes the roster slot and
  reads `NORMAL` for all 224 players — including the four who are out. The
  player's carries `ACTIVE`/`QUESTIONABLE`/`OUT`. Reading the wrong one is
  silent — it stores a real-looking value that is never the one anybody wants —
  which is exactly how it went unnoticed until something finally rendered it.
- **Bench and IR sort themselves.** `BE`, `IR`, `RES` are absent from
  `players.SLOT_ORDER`, so `_slot_rank` puts them after every starter with no
  extra ordering code — which is what makes the modal's starters-then-bench-then-IR
  order free.
- **The modal is rendered into the page, not fetched.** Every roster row is
  already in the context, so opening a matchup costs no request. Because the
  dialog lives inside the swapped `#scoreboard`, the 30-second live refresh
  re-renders it with fresh numbers while it is open. It is reopened by id after
  the swap, the same trick the old inline expansion used.
- **History comes at a coarser grain than the current season.** Past seasons are
  served as season *totals* only — W-L-T, points for/against, final rank. No
  weekly schedule is retrievable for them by any means (four different requests
  were tried). So:
  - **All-time standings, titles, finishes** — every season. Available now.
  - **Head-to-head, all-play, luck** — need per-week opponents, so they begin
    with the current season and fill in as it runs.

  An empty head-to-head matrix before the season starts is correct, not a bug;
  the pages say so rather than showing a blank grid.
- **The history endpoint needs no `seasonId`.** `leagueHistory/{id}` returns
  every past season as a list. *Passing* `seasonId` returns HTTP 200 with an
  empty shell, which looks exactly like "this league has no history" — the most
  misleading behaviour in this API.
- **Scores are mutable.** ESPN issues stat corrections for days after games.
  Storage is snapshot-and-revise: re-polling identical data is a no-op, and a
  changed score bumps `revision`. Revised weeks are listed on the dashboard.
- **Polling only**, no push, and the cadence follows the season state: every 6h
  before the draft, every 5 min around draft time, 45s during game windows, plus
  a daily sweep of recent weeks to catch corrections.
- **Fetching uses `scoreboard()`, not `box_scores()`.** box_scores reads rosters
  unguarded, so it raises `KeyError` for the entire pre-draft period, and it
  honours its `week` argument only when `week <= current_week` — past that it
  silently returns the *current* week's data, which would write today's scores
  under future week numbers. box_scores is used only to add projected scores to
  the live week.
- **All-play and luck ignore weeks still in progress**, so standings don't swing
  mid-Sunday and then settle.
- **Franchises are keyed by team id, not name.** Ids are stable across seasons
  while names are not, so a rename doesn't split one franchise into two.
- **A slot that changed hands is not a rename.** ESPN reuses the same team id
  when a franchise passes to a new manager, so its old record would otherwise be
  inherited. `league.franchise_since` in `config/config.yml` sets the season a
  slot took its current identity; earlier seasons are excluded from all-time
  totals, the season grid, name history and hover records. It is a filter, not a
  deletion — the rows stay on disk, so removing the setting restores them (and a
  deletion would be undone by the next history sync anyway).
- **Players are keyed by player id, not name.** Names are not unique — ESPN's
  universe holds a wide receiver *and* a linebacker named Justin Jefferson, and
  a quarterback and a cornerback named Lamar Jackson. A name key merges them.
- **Player position comes from `defaultPositionId`, not `POSITION_MAP`.** That
  constant maps *lineup slots*, a different enum; running one through the other
  files tight ends under WR and leaves QB, TE and K empty. The correct map lives
  in `app/players.py` and was derived against players espn-api had resolved.
- **Rookie status comes from the NFL team rosters, not the fantasy API.**
  `site.api.espn.com/.../teams/{ABBR}/roster` carries `experience.years` per
  athlete, and those athlete ids are the same id space as fantasy player ids, so
  the join is direct. Inferring rookies from missing prior-season stats would
  misread anyone who sat out a season. That endpoint is public, so it is fetched
  **without** the ESPN session cookies, and only once a day — rookie status
  cannot change mid-season.
- **Projection syncs are throttled to 30 minutes.** Each pull is several MB and
  the poller ticks every 45s during games; without the throttle a live Sunday
  would fetch ~12MB a tick. Projections do not move minute to minute.
- **Last week's actuals need a re-sync of that week.** The row written before
  kickoff has a null actual, so the poller refreshes the previous week as well
  as the current one — that back-fill is what fills the "Last" column.
- **Matchup cards show starters only.** A benched player can hold the highest
  projection on a roster; billing them as the team's best player for a week they
  do not play would be wrong. The whole starting lineup is stored, not a top-N
  slice — the featured card lists all 16, and fetching the rest at render time
  would put ESPN back on the page path. The compact cards slice the same stored
  rows to a top three, which is why their lineups cost no extra query.
- **A player's NFL team comes from the matchup payload, not the roster sync.**
  The daily roster sync only knows players signed to an NFL team when it ran, so
  it has no row at all for a team defence and misses anyone signed since;
  `proTeamId` is on every matchup entry, D/ST included. Watch for espn-api
  mapping `proTeamId` 0 to the **string** `"None"` — it is truthy, so every `or`
  fallback lets it through and an unsigned player ends up playing for a team
  called None.
- **Team colours are keyed on ESPN's abbreviations**, which are not always the
  NFL's: `WSH` not `WAS`, `JAX` not `JAC`. A mismatch is silent — the row simply
  renders untinted — so a test asserts every abbreviation espn-api can return
  has a colour.
- **Projections need `ONTEAM` in the status filter.** Without it the Players page
  silently becomes free-agents-only the moment the draft happens — and looks
  perfectly fine beforehand, when everyone is a free agent.
- **ESPN fantasy player ids join to nflverse through `players.csv`.** The chain
  is espn_id → gsis_id for stats, espn_id → pfr_id for snap counts. Measured
  on the real roster: 120 of 128 rostered players match, and 100% of the eight
  that do not are team defences.
- **Team defences never join by player id.** They have no espn_id and no
  gsis_id in any NFL dataset, so they route through the team abbreviation into
  the team-week file instead.
- **The team-week file's `fantasy_points` belongs to the offence, not the
  D/ST.** Showing it beside a fantasy defence would be a confidently wrong
  number, so the D/ST table reports sacks, takeaways and defensive touchdowns
  instead and no points column at all.
- **nflverse and ESPN disagree on exactly two team abbreviations.** nflverse
  writes `LA` and `WAS`, ESPN writes `LAR` and `WSH`. Normalised once at the
  parse boundary. A mismatch is silent (the row just renders untinted and the
  D/ST join returns nothing), so a test asserts it.
- **Snap share must be read off the right side of the ball.** A linebacker's
  `offense_pct` is 0; using it would report the league's seven defensive
  starters as barely playing. The column is chosen by position.
- **Boom and bust are relative to the player, not to a fixed points line.** 12
  points is a great week for a kicker and a disaster for a workhorse back; one
  absolute threshold would call every kicker consistent and every RB1 volatile.
  Boom is 1.5x a player's own average, bust is half of it.
- **A missing season file is "not yet", not an error.** nflverse builds
  `stats_player_week_{year}` only once that season's week 1 has been played, so
  asking for the current season in August is normal. It raises `NotPublished`,
  which the poller logs and moves past.
- **Each nflverse release carries a `timestamp.json`.** Reading it costs a few
  bytes and tells us whether the release has been rebuilt, so an unchanged
  release costs no download. That is what makes it safe to hang this off the
  ordinary poll loop.
- **Play-by-play is streamed, not read whole.** `fetch_csv` deliberately reads
  whole files (its docstring says why: the others are ≤2.5MB and a truncated
  parse would look like missing weeks). Play-by-play is the exception: 48,771
  rows × 372 columns, 18MB gzipped, 93MB decompressed. `gzip.decompress()` plus
  `list(csv.DictReader(...))` would hold millions of live strings to keep eight
  counters; streaming through `GzipFile` → `TextIOWrapper` → `DictReader`
  measures 1.7s and 13MB peak RSS for a season.
- **Per-down counters live in their own table.** `nfl_player_weeks` is replaced
  wholesale from a different nflverse file by `sync_nflverse`; merging two
  independently published sources into one wholesale replace creates a sync order
  somebody has to remember. The asymmetry is deliberate too:
  `nfl_player_down_weeks` has no `season_type` column, because the aggregation
  has already dropped every non-REG play -- which is what makes it agree with
  `fetch_nfl_player_weeks`, whose reads are REG-only for the same reason.
- **A down is counted off the play, so kickoffs and extra points are skipped.**
  `down` is blank on them — and a sack counts for nobody, since there is no
  `receiver_player_id`.
- **The range selector is row filtering, not new maths.** `player_summary`
  computes over whatever rows it is given, so one week, four weeks and a season
  are the same code path; `weeks_in_range` takes the weeks actually stored rather
  than a season length, because "the last four weeks" in September is however many
  have been played. Note that the trend column is shown only for the full-season
  range, since last-4-vs-season form says nothing when the page is already showing
  four weeks or one.
- **2026 play-by-play does not exist until Week 1 is played** — `NotPublished`,
  the same normal-not-an-error path as the weekly stats file.
- **The per-down counts reconcile with the weekly totals.** On 2025 data the
  four per-down target counters sum exactly to `nfl_player_weeks.targets` for
  the same player-week. Two independently parsed nflverse files agreeing to the
  target is the check that the down filter is not quietly dropping plays.
- **Bench players are stored now, flagged rather than filtered.** You cannot
  know the lineup someone should have started without knowing who was available,
  so "points left on the bench" needs the whole roster. Every read defaults to
  starters only, which is why it is a flag and not a separate table.
- **The best-possible-lineup is greedy by slot, not a full optimisation.**
  Slots are filled in order, each taking the best eligible player still unused.
  For a fixed lineup this matches what a manager could actually have set, and
  it cannot invent a lineup the rules forbid.
- **`league.refresh_draft()` appends, it does not replace.** espn-api's
  `_fetch_draft` adds to `self.draft` without clearing it, and a freshly
  constructed `League` has already fetched the board (with names — the player
  map is loaded first). Calling `refresh_draft()` on top of that stores every
  pick twice: a 224-pick board became 448 rows, i.e. a 56-round draft in which
  everyone was picked twice.
- **The transaction feed is read directly, not through espn-api.** Its
  `recent_activity()` resolves any player not on a current roster with a
  separate request — and a dropped player is by definition not on a roster, so a
  feed of drops would cost one extra round trip each. Ids come back raw and are
  named from the players we already store.
- **espn-api maps three different message ids to "dropped".** (179, 181, 239)
  — manual, roster-limit and failed-waiver drops. Nothing downstream cares
  about the distinction.
- **The news feed is hard-capped at 50 articles regardless of `limit`.** 100,
  200 and 1000 all return 50. It is a rolling ~36-hour window with no cursor, so
  every poll re-reads what is already stored and there is no backfill — the panel
  fills in as the poller runs. Unlike the transaction feed it upserts rather than
  ignoring duplicates, because ESPN retitles articles after publishing; only
  genuinely new ids count towards the "rows changed" log.
- **`?athlete=<id>` on the news endpoint is silently ignored.** ESPN returns the
  unfiltered feed regardless, so athlete filtering happens at store time off
  each article's `categories` rather than by asking ESPN for it.
- **Article categories of type `athlete` carry an `athleteId` in the same id
  space as fantasy player ids**, which makes the roster join direct with no name
  matching. (This is the same ESPN id space mentioned for the NFL roster
  endpoint.)
- **The news endpoint is public and is fetched without the ESPN session
  cookies**, same as the NFL roster endpoint. A consequence: the news sync runs
  before the fantasy API call in the poll cycle, so expired cookies do not blank
  a panel that never needed them.
- **Every article is stored, not just ones matching a configured reporter.**
  Roster-relevant injury and transaction items frequently have no byline at all
  (16 of 50 in a live sample), and a reporter filter at fetch time would drop
  exactly those. The page separates them, the fetch does not.

nflverse data is published under CC-BY-4.0, which requires attribution — it is
printed on the NFL usage page.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
CONFIG_PATH=config/config.yml DB_PATH=data/league.db \
  .venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

Outside a container, `config/secrets.env` is read as a fallback (it never
overrides a variable already set in the environment). Inside a container that
fallback is disabled — Compose's `env_file` is the only source, which avoids a
dotenv loader silently shadowing it.

### Tests

```bash
.venv/bin/pip install pytest && .venv/bin/python -m pytest tests/ -q
```

The suite is 135 tests.

`tests/test_analytics.py` checks the week-level analytics against a four-team,
three-week fixture with every expected value computed by hand. That matters more
than it looks: an off-by-one in all-play or luck still produces numbers that look
entirely plausible on the page.

`app/rules.py` turns ESPN's `mSettings` payload — enum strings, epoch
milliseconds, slot-id maps and `-1` sentinels — into labelled rows grouped by
topic. Search filters client-side over the whole list, so typing is instant and
needs no round trip. Bench and IR are excluded from the starter count; counting
them would claim a 30-man starting lineup for a 16-man one.

`tests/test_auth.py` covers the login surface, weighted towards the failures
that would pass unnoticed: a session that outlives a password reset, a rename
that orphans someone's login, two franchises sharing a name colliding on the
unique key, and a scoreboard that leads with the wrong person's game.

`tests/test_season.py` covers the season-state machine, the snapshot-and-revise
contract for season totals, franchise renames, and the pre-draft guards — in
particular that a schedule stored with 0.0 points does not count as played, and
that a completed draft is detected from ESPN's `drafted` flag rather than from
`len(picks)` (ESPN returns a full array of *empty placeholder* picks before the
draft, 224 of them for an 8-team league).

`tests/test_news.py` covers parsing ESPN's news payload offline, co-byline matching,
the insert-and-ignore-duplicates contract, and the retention prune.

`tests/test_nflverse.py` covers parsing and storage of nflverse datasets, offline
— the fetch layer is exercised by handing it bytes, never by touching the network.

`tests/test_nflstats.py` checks usage analytics against a hand-computed three-week
fixture.

## Porting to another host

The image holds no configuration or state. Copy the directory, then:

```bash
sudo chown -R 1000:1000 data     # container runs as uid 1000
docker compose up -d
```

Building on Apple Silicon for an amd64 host produces an unrunnable image —
build on the target host, or `docker buildx build --platform linux/amd64`.

## Scope

Deliberately single-league and per-franchise: franchises log in with their team
name and a machine-generated password — the gate is for people already in the
league, not a hardened public-internet auth. No signup, no storing anyone else's
cookies. ESPN's terms prohibit automated access and redistribution; a personal
tool for your own league is the intended use. Even with per-franchise login,
exposing this on the open internet warrants a reverse proxy with TLS in front —
the session cookie sets Secure only over HTTPS.

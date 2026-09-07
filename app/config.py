"""Configuration loading.

Two sources, deliberately separate:
  - config.yml  -- league settings, bind-mounted read-only
  - environment -- secrets (cookies), supplied by Compose's env_file

We never call load_dotenv() unconditionally. Inside the container Compose has
already populated the environment, and a dotenv loader layered on top silently
shadows it -- the classic "my .env is ignored" bug. The file fallback below is
gated on running outside a container and never overrides an existing variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

# NFL games happen on US Eastern time; live-window scheduling is anchored here
# and must never be swapped for the display timezone below.
LEAGUE_TZ = ZoneInfo("America/New_York")
DEFAULT_DISPLAY_TZ = "Europe/Budapest"

# `news` is the only optional block in config.yml: unlike league id, a missing
# section must still boot, so these are what an existing deployment gets for
# free rather than a startup failure.
DEFAULT_NEWS_REPORTERS = (
    "Adam Schefter", "Jeremy Fowler", "Dan Graziano",
    "Mike Clay", "Field Yates", "ESPN Fantasy",
)
DEFAULT_NEWS_KEEP_DAYS = 21

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class ConfigError(RuntimeError):
    """Raised at startup for missing or malformed configuration."""


def in_container() -> bool:
    return os.environ.get("RUNNING_IN_CONTAINER") == "1"


def _load_dev_secrets() -> None:
    """Outside a container only: seed os.environ from config/secrets.env.

    Never overrides a variable that is already set, so the real environment
    always wins.
    """
    if in_container():
        return
    path = Path(__file__).resolve().parent.parent / "config" / "secrets.env"
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class LiveWindow:
    day: int  # 0=Mon .. 6=Sun
    start: time
    end: time

    def contains(self, moment) -> bool:
        return self.day == moment.weekday() and self.start <= moment.time() <= self.end


@dataclass(frozen=True)
class Config:
    league_id: int
    current_season: int
    seasons: tuple[int, ...]
    live_interval: int
    idle_interval: int
    correction_sweep_hour: int
    correction_weeks_back: int
    live_windows: tuple[LiveWindow, ...]
    espn_s2: str
    swid: str
    db_path: Path
    display_tz_name: str
    seasons_auto: bool
    franchise_since: dict[int, int]
    news_reporters: tuple[str, ...]
    news_keep_days: int

    @property
    def has_credentials(self) -> bool:
        return bool(self.espn_s2 and self.swid)

    @property
    def display_tz(self) -> ZoneInfo:
        """Timezone for times shown to people -- draft countdown, timestamps.

        Separate from LEAGUE_TZ on purpose: when games kick off is a fact about
        the NFL, what time it is for the reader is a fact about the reader.
        """
        try:
            return ZoneInfo(self.display_tz_name)
        except Exception:  # noqa: BLE001 - bad tz name must not break startup
            return LEAGUE_TZ


def _parse_time(value: str, label: str) -> time:
    try:
        hour, minute = (int(part) for part in str(value).split(":", 1))
        return time(hour, minute)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"live_windows: {label} is not HH:MM -- got {value!r}") from exc


def _require(mapping: dict, key: str, where: str):
    if key not in mapping or mapping[key] in (None, ""):
        raise ConfigError(f"config.yml: missing required key '{where}.{key}'")
    return mapping[key]


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load and validate configuration, failing fast with an actionable message.

    A container that boots and then quietly polls nothing is far worse than one
    that refuses to start, so every problem here is fatal.
    """
    _load_dev_secrets()

    config_path = Path(path or os.environ.get("CONFIG_PATH", "/app/config/config.yml"))
    if not config_path.is_file():
        raise ConfigError(
            f"No config file at {config_path}. Copy config/config.yml.example to "
            "config/config.yml, or set CONFIG_PATH. In Docker this means the "
            "./config bind mount is missing."
        )

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yml is not valid YAML: {exc}") from exc

    league = raw.get("league") or {}
    poll = raw.get("poll") or {}

    league_id = _require(league, "id", "league")
    current_season = _require(league, "current_season", "league")
    # "auto" (the default) means: discover seasons from ESPN's own
    # status.previousSeasons at runtime rather than hardcoding a list that goes
    # stale or, worse, lists seasons the league never played.
    # A franchise slot that changed hands: count its record only from the season
    # it took its current identity. Enforced in config rather than by deleting
    # rows, because the poller re-syncs history every cycle and would undo that.
    franchise_since = {
        int(k): int(v) for k, v in (league.get("franchise_since") or {}).items()
    }

    raw_seasons = league.get("seasons")
    seasons_auto = raw_seasons in (None, "auto", [], "")
    seasons = [current_season] if seasons_auto else list(raw_seasons)

    windows: list[LiveWindow] = []
    for entry in raw.get("live_windows") or []:
        day = str(entry.get("day", "")).lower()[:3]
        if day not in _DAYS:
            raise ConfigError(f"live_windows: unknown day {entry.get('day')!r}")
        windows.append(
            LiveWindow(
                day=_DAYS[day],
                start=_parse_time(entry.get("start", "00:00"), f"{day}.start"),
                end=_parse_time(entry.get("end", "23:59"), f"{day}.end"),
            )
        )

    espn_s2 = os.environ.get("ESPN_S2", "").strip()
    swid = os.environ.get("SWID", "").strip()

    news = raw.get("news") or {}
    news_reporters = news.get("reporters")
    if not news_reporters:
        news_reporters = DEFAULT_NEWS_REPORTERS

    return Config(
        league_id=int(league_id),
        current_season=int(current_season),
        seasons=tuple(sorted({int(s) for s in seasons}, reverse=True)),
        live_interval=int(poll.get("live_interval_seconds", 45)),
        idle_interval=int(poll.get("idle_interval_seconds", 21600)),
        correction_sweep_hour=int(poll.get("correction_sweep_hour", 6)),
        correction_weeks_back=int(poll.get("correction_weeks_back", 2)),
        live_windows=tuple(windows),
        espn_s2=espn_s2,
        swid=swid,
        db_path=Path(os.environ.get("DB_PATH", "/app/data/league.db")),
        display_tz_name=str(raw.get("display_timezone") or DEFAULT_DISPLAY_TZ),
        seasons_auto=seasons_auto,
        franchise_since=franchise_since,
        news_reporters=tuple(news_reporters),
        news_keep_days=int(news.get("keep_days", DEFAULT_NEWS_KEEP_DAYS)),
    )


_cached: Config | None = None


def get_config(reload: bool = False) -> Config:
    global _cached
    if _cached is None or reload:
        _cached = load_config()
    return _cached

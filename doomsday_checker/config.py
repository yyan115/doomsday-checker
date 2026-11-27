"""TOML configuration for the watcher."""

import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Schedule:
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("Asia/Singapore"))
    trailer_minutes: int = 20
    busy_period: tuple[time, time] | None = None


@dataclass(frozen=True)
class SeatZone:
    rows: tuple[float, float]
    columns: tuple[float, float]


@dataclass(frozen=True)
class SeatPreferences:
    good: SeatZone = SeatZone((0.35, 0.80), (0.30, 0.75))
    okay: SeatZone = SeatZone((0.25, 0.90), (0.20, 0.85))
    prefer_right: bool = False
    notify_outside_schedule: bool = True
    shaw_premium_halls: tuple[str, ...] = ("LUMRG", "LUMR", "PREM")
    gv_premium_halls: tuple[str, ...] = ("Gold Class",)


@dataclass(frozen=True)
class Sources:
    shaw_access: str = "browser"
    gv_enabled: bool = True
    gv_access: str = "listings"
    gv_movie_slugs: tuple[str, ...] = (
        "marvel-studios-avengers-doomsday",
        "marvel-studios-avengers-endgame-x-doomsday-marathon",
    )
    gv_popcorn_path: str = "avengers-doomsday/movie/21530"
    chromium_executable: Path | None = None


@dataclass(frozen=True)
class Config:
    directory: Path
    title: str = "Doomsday"
    state_path: Path = Path("state.db")
    schedule: Schedule = field(default_factory=Schedule)
    seats: SeatPreferences = field(default_factory=SeatPreferences)
    sources: Sources = field(default_factory=Sources)


def _table(data: dict, key: str) -> dict:
    table = data.get(key, {})
    if not isinstance(table, dict):
        raise TypeError(f"{key} must be a table")
    return table


def _value(data: dict, key: str, default: Any) -> Any:
    value = data.get(key, default)
    if type(value) is not type(default):
        raise ValueError(f"{key} must be a {type(default).__name__}")
    return value


def _clock_time(value: object) -> time:
    if isinstance(value, str):
        value = time.fromisoformat(value)
    if not isinstance(value, time) or value.tzinfo is not None:
        raise ValueError("Weekday times must be local times")
    return value


def _schedule(data: dict) -> Schedule:
    defaults = Schedule()
    busy_period = None
    if "busy_from" in data or "busy_until" in data:
        if "busy_from" not in data or "busy_until" not in data:
            raise ValueError("Set both busy_from and busy_until to restrict weekday hours")
        start = _clock_time(data["busy_from"])
        end = _clock_time(data["busy_until"])
        if start >= end:
            raise ValueError("busy_from must precede busy_until")
        busy_period = start, end
    trailer_minutes = _value(data, "trailer_minutes", defaults.trailer_minutes)
    if trailer_minutes < 0:
        raise ValueError("trailer_minutes must be nonnegative")
    return Schedule(
        timezone=ZoneInfo(_value(data, "timezone", defaults.timezone.key)),
        trailer_minutes=trailer_minutes,
        busy_period=busy_period,
    )


def _range(data: dict, key: str, default: tuple[float, float]) -> tuple[float, float]:
    values = _value(data, key, list(default))
    if len(values) != 2 or any(type(value) not in (int, float) for value in values):
        raise ValueError(f"{key} must contain two numbers")
    lower, upper = values
    if not 0 <= lower <= upper <= 1:
        raise ValueError(f"{key} must be an ordered range within 0 and 1")
    return float(lower), float(upper)


def _strings(data: dict, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    values = _value(data, key, list(default))
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"{key} must contain nonempty strings")
    return tuple(value.strip() for value in values)


def _preferences(data: dict) -> SeatPreferences:
    defaults = SeatPreferences()
    good = _table(data, "good")
    okay = _table(data, "okay")
    return SeatPreferences(
        good=SeatZone(
            _range(good, "rows", defaults.good.rows),
            _range(good, "columns", defaults.good.columns),
        ),
        okay=SeatZone(
            _range(okay, "rows", defaults.okay.rows),
            _range(okay, "columns", defaults.okay.columns),
        ),
        prefer_right=_value(data, "prefer_right", defaults.prefer_right),
        notify_outside_schedule=_value(
            data, "notify_outside_schedule", defaults.notify_outside_schedule
        ),
        shaw_premium_halls=_strings(data, "shaw_premium_halls", defaults.shaw_premium_halls),
        gv_premium_halls=_strings(data, "gv_premium_halls", defaults.gv_premium_halls),
    )


def _sources(data: dict, directory: Path) -> Sources:
    defaults = Sources()
    shaw_access = _value(data, "shaw_access", defaults.shaw_access)
    gv_access = _value(data, "gv_access", defaults.gv_access)
    if shaw_access not in {"browser", "http"}:
        raise ValueError("shaw_access must be browser or http")
    if gv_access not in {"listings", "http"}:
        raise ValueError("gv_access must be listings or http")
    executable = _value(data, "chromium_executable", "")
    return Sources(
        shaw_access=shaw_access,
        gv_enabled=_value(data, "gv_enabled", True),
        gv_access=gv_access,
        gv_movie_slugs=_strings(data, "gv_movie_slugs", defaults.gv_movie_slugs),
        gv_popcorn_path=_value(data, "gv_popcorn_path", defaults.gv_popcorn_path),
        chromium_executable=directory / Path(executable).expanduser() if executable else None,
    )


def load_config(path: Path | None = None) -> Config:
    """Load the selected file, with optional local overrides in the current directory."""
    config_path = (path or Path("config.local.toml")).expanduser().resolve()
    try:
        with config_path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        if path is not None:
            raise
        data = {}
    watch = _table(data, "watch")
    title = _value(watch, "title", "Doomsday").strip()
    if not title:
        raise ValueError("title must be nonempty")
    state_path = _value(watch, "state_path", "state.db")
    if not state_path.strip():
        raise ValueError("state_path must be nonempty")
    return Config(
        directory=config_path.parent,
        title=title,
        state_path=config_path.parent / Path(state_path).expanduser(),
        schedule=_schedule(_table(data, "schedule")),
        seats=_preferences(_table(data, "seats")),
        sources=_sources(_table(data, "sources"), config_path.parent),
    )

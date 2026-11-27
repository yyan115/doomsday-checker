"""Polling and response handling shared by cinema sources."""

import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import Config
from ..models import Cinema, Listing, SeatMap, Session
from ..rules import check_timing

SINGAPORE = ZoneInfo("Asia/Singapore")
JSON_ACCEPT = "application/json, text/plain, */*"


class SourceError(Exception):
    """A source did not return usable data."""


class Challenge(SourceError):
    """A source requires interactive verification."""


class BackOff(SourceError):
    """A source requested a lower request rate or refused access."""


def check_response(status: int, challenge_header: str, body: str) -> None:
    if challenge_header == "challenge" or re.search(
        r"<title>\s*Just a moment", body, flags=re.IGNORECASE
    ):
        raise Challenge("Cloudflare verification required")
    if status in (403, 429):
        raise BackOff(f"HTTP {status}")
    if not 200 <= status < 300:
        raise SourceError(f"HTTP {status}")


def read_json(body: str, expected_type: type):
    try:
        data = json.loads(body)
    except ValueError:
        raise SourceError("Expected JSON") from None
    if not isinstance(data, expected_type):
        raise SourceError(f"Expected a JSON {expected_type.__name__}")
    return data


def jitter(seconds: float) -> float:
    return seconds * random.uniform(0.8, 1.2)


def fraction(value: float, low: float, high: float) -> float:
    return (value - low) / (high - low) if high != low else 0.5


def matches_title(title: str, config: Config) -> bool:
    return config.title.casefold() in title.casefold()


@dataclass
class Snapshot:
    listings: tuple[Listing, ...] | None = None
    sessions: tuple[Session, ...] | None = None
    layouts: dict[str, SeatMap] = field(default_factory=dict)
    error: SourceError | None = None


class PollingSource:
    cinema: Cinema
    listing_interval: float
    layout_interval: float | None
    sold_out_interval: float | None = None
    layout_on_discovery = False

    def __init__(self, config: Config):
        self.config = config
        self.sessions: tuple[Session, ...] = ()
        self._listings_at = 0.0
        self._layouts_at: dict[str, float] = {}
        self._sold_out_layouts: bool | None = None

    @property
    def next_poll(self) -> float:
        return min(self._listings_at, min(self._layouts_at.values(), default=self._listings_at))

    def _load_listings(self, now: float) -> tuple[tuple[Listing, ...], tuple[Session, ...]]:
        raise NotImplementedError

    def _load_layout(self, session: Session) -> SeatMap | None:
        raise NotImplementedError

    def _layout_interval(self, session: Session, wall_time: datetime) -> float | None:
        if not session.seat_maps_supported or session.starts_at <= wall_time:
            return None
        if (
            not self.config.seats.notify_outside_schedule
            and not check_timing(session, self.config.schedule).qualifies
        ):
            return None
        if session.sold_out:
            return self.sold_out_interval if self._sold_out_layouts is not False else None
        return self.layout_interval

    def poll(self, now: float, wall_time: datetime) -> Snapshot:
        snapshot = Snapshot()
        discovered = set()
        try:
            if now >= self._listings_at:
                snapshot.listings, snapshot.sessions = self._load_listings(now)
                existing = {session.id: session.sold_out for session in self.sessions}
                for session in snapshot.sessions:
                    if session.id not in existing:
                        discovered.add(session.id)
                    if existing.get(session.id) != session.sold_out:
                        self._layouts_at.pop(session.id, None)
                self.sessions = snapshot.sessions
                self._listings_at = now + jitter(self.listing_interval)
            eligible = {
                session.id
                for session in self.sessions
                if self._layout_interval(session, wall_time) is not None
            }
            self._layouts_at = {
                key: value for key, value in self._layouts_at.items() if key in eligible
            }
            for session in self.sessions:
                interval = self._layout_interval(session, wall_time)
                first_layout = (
                    self.layout_on_discovery
                    and session.id in discovered
                    and session.seat_maps_supported
                    and session.starts_at > wall_time
                )
                if (interval is None and not first_layout) or now < self._layouts_at.get(
                    session.id, 0
                ):
                    continue
                try:
                    layout = self._load_layout(session)
                except (BackOff, Challenge):
                    raise
                except SourceError as error:
                    snapshot.error = snapshot.error or error
                    continue
                if (
                    session.sold_out
                    and self.sold_out_interval is not None
                    and self._sold_out_layouts is None
                ):
                    self._sold_out_layouts = layout is not None
                if layout is not None:
                    snapshot.layouts[session.id] = layout
                if interval is not None:
                    self._layouts_at[session.id] = now + jitter(interval)
        except SourceError as error:
            snapshot.error = error
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as error:
            snapshot.error = SourceError(f"Unexpected data ({type(error).__name__})")
        return snapshot

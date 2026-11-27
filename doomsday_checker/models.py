"""Cinema-independent showtimes and seat maps."""

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum


class Cinema(StrEnum):
    SHAW = "Shaw"
    GV = "Golden Village"


class SeatLevel(IntEnum):
    OKAY = 1
    GOOD = 2


@dataclass(frozen=True)
class Session:
    cinema: Cinema
    id: str
    title: str
    venue: str
    format: str
    starts_at: datetime
    runtime_minutes: int
    sold_status: str
    sold_out: bool | None
    booking_url: str
    premium_hall: bool = False
    seat_maps_supported: bool = True


@dataclass(frozen=True)
class Listing:
    id: str
    title: str
    on_sale: bool
    standard: bool


@dataclass(frozen=True)
class Seat:
    label: str
    row: str
    x: float
    y: float
    row_fraction: float
    column_fraction: float
    max_gap: float
    available: bool


@dataclass(frozen=True)
class SeatMap:
    seats: tuple[Seat, ...]
    row_tolerance: float = 0


@dataclass(frozen=True)
class SeatPair:
    first: str
    second: str
    level: SeatLevel

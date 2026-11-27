"""Schedule and seat preferences shared by cinema sources."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import time, timedelta
from itertools import pairwise

from .config import Schedule, SeatPreferences, SeatZone
from .models import Seat, SeatLevel, SeatMap, SeatPair, Session


@dataclass(frozen=True)
class Timing:
    qualifies: bool
    late_night: bool
    reason: str


def check_timing(session: Session, schedule: Schedule) -> Timing:
    start = session.starts_at.astimezone(schedule.timezone)
    late_night = start.time() >= time(22, 30) or start.time() < time(7)
    if start.weekday() >= 5:
        return Timing(True, late_night, "Weekend")
    if schedule.busy_period is None:
        return Timing(True, late_night, "Open schedule")
    busy_from, busy_until = schedule.busy_period
    if start.time() >= busy_until:
        return Timing(True, late_night, f"Starts at or after {busy_until:%H:%M}")
    end = start + timedelta(minutes=session.runtime_minutes + schedule.trailer_minutes)
    if end <= start.replace(
        hour=busy_from.hour,
        minute=busy_from.minute,
        second=busy_from.second,
        microsecond=busy_from.microsecond,
    ):
        return Timing(True, late_night, f"Ends by {busy_from:%H:%M}")
    return Timing(False, late_night, f"Overlaps weekday hours {busy_from:%H:%M}–{busy_until:%H:%M}")


def _in_zone(seat: Seat, zone: SeatZone, shift: float = 0) -> bool:
    return (
        zone.rows[0] <= seat.row_fraction <= zone.rows[1]
        and zone.columns[0] + shift <= seat.column_fraction <= zone.columns[1] + shift
    )


def find_pairs(
    layout: SeatMap, preferences: SeatPreferences, premium: bool = False
) -> list[SeatPair]:
    rows: dict[str, list[Seat]] = defaultdict(list)
    for seat in layout.seats:
        rows[seat.row].append(seat)
    shift = 0.05 if preferences.prefer_right else 0
    pairs = []
    for row in rows.values():
        for left, right in pairwise(sorted(row, key=lambda seat: seat.x)):
            if not left.available or not right.available:
                continue
            if abs(left.y - right.y) > layout.row_tolerance:
                continue
            if not 0 < right.x - left.x <= min(left.max_gap, right.max_gap):
                continue
            if premium or all(_in_zone(seat, preferences.good, shift) for seat in (left, right)):
                pairs.append(SeatPair(left.label, right.label, SeatLevel.GOOD))
            elif all(_in_zone(seat, preferences.okay) for seat in (left, right)):
                pairs.append(SeatPair(left.label, right.label, SeatLevel.OKAY))
    return sorted(pairs, key=lambda pair: pair.level, reverse=True)

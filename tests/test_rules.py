from dataclasses import replace
from datetime import datetime, time

import pytest

from doomsday_checker.config import Schedule, SeatPreferences
from doomsday_checker.models import Cinema, Session
from doomsday_checker.rules import check_timing, find_pairs
from doomsday_checker.sources import gv, shaw
from doomsday_checker.sources.common import SINGAPORE


@pytest.mark.parametrize(
    ("day", "hour", "minute", "qualifies", "late_night"),
    [
        (17, 0, 1, True, True),
        (17, 5, 0, True, True),
        (17, 6, 19, True, True),
        (17, 6, 20, False, True),
        (17, 7, 0, False, False),
        (17, 12, 30, False, False),
        (17, 17, 45, False, False),
        (18, 18, 0, True, False),
        (17, 22, 25, True, False),
        (17, 22, 30, True, True),
        (19, 10, 0, True, False),
    ],
)
def test_time_rules(day, hour, minute, qualifies, late_night):
    start = datetime(2026, 12, day, hour, minute, tzinfo=SINGAPORE)
    session = Session(
        Cinema.SHAW, "1", "Example", "Hall 1", "2D", start, 151, "Available", False, ""
    )
    timing = check_timing(session, Schedule(busy_period=(time(9, 10), time(18))))
    assert timing.qualifies == qualifies
    assert timing.late_night == late_night


def _shaw_seat(number, x, *, status="AV", code="EL_STS", house=False):
    return {
        "elementCategoryCode": "SEAT",
        "elementCode": code,
        "elementStatusCodeCurrent": status,
        "rowReference": "C",
        "columnReference": number,
        "anchorCoordinateX": x,
        "anchorCoordinateY": 100,
        "elementWidthX": 20,
        "isHouseSeat": house,
    }


def test_shaw_pairs_exclude_aisles_house_seats_and_wheelchair_spaces():
    raw = [
        _shaw_seat(6, 0, code="EL_HDS"),
        _shaw_seat(5, 23),
        _shaw_seat(4, 76),
        _shaw_seat(2, 99),
        _shaw_seat(1, 122, house=True),
    ]
    pairs = find_pairs(shaw.parse_layout(raw), SeatPreferences(), premium=True)
    assert [(pair.first, pair.second) for pair in pairs] == [("C4", "C2")]


def _gv_cell(column, status="L", seat_type="N"):
    return {
        "id": f"C:{column}",
        "rowId": "C",
        "columnId": str(column),
        "colNumber": column,
        "type": seat_type,
        "status": status,
    }


def test_gv_pairs_use_grid_adjacency_and_available_normal_seats():
    raw = [
        [
            _gv_cell(1),
            _gv_cell(2),
            {"id": None},
            _gv_cell(4),
            _gv_cell(5, "T"),
            _gv_cell(6, seat_type="W"),
            _gv_cell(7),
            _gv_cell(8),
        ]
    ]
    pairs = find_pairs(gv.parse_layout(raw), SeatPreferences(), premium=True)
    assert [(pair.first, pair.second) for pair in pairs] == [("C1", "C2"), ("C7", "C8")]


def test_right_preference_shifts_good_zone_only():
    raw = [[_gv_cell(1, "B"), _gv_cell(4), _gv_cell(5), _gv_cell(11, "B")]]
    preferences = SeatPreferences()
    layout = gv.parse_layout(raw)
    assert [pair.level.name for pair in find_pairs(layout, preferences)] == ["GOOD"]
    assert [
        pair.level.name for pair in find_pairs(layout, replace(preferences, prefer_right=True))
    ] == ["OKAY"]

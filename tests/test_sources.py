import json
from datetime import datetime
from unittest.mock import Mock

import httpx
import pytest

from doomsday_checker.config import Config
from doomsday_checker.sources import gv, shaw
from doomsday_checker.sources.common import SINGAPORE, Challenge
from doomsday_checker.sources.shaw_browser import ShawBrowser


def test_shaw_discovers_matching_homepage_listings(tmp_path):
    cards = [
        {"movieReleaseId": 100, "primaryTitle": "Avengers: Doomsday"},
        {"movieReleaseId": 200, "primaryTitle": "Another film"},
    ]
    html = f"<script>self.__next_f.push({json.dumps([1, json.dumps(cards)])})</script>"
    assert shaw.movie_cards(html, Config(tmp_path)) == {"100": "Avengers: Doomsday"}


def test_shaw_uses_movie_showtimes_date_control():
    browser = ShawBrowser(None)
    browser._active_page = "/movie-details/2034"
    browser._page = Mock()

    browser._choose_date("2026-12-17")

    browser._page.locator.assert_called_once_with('#showtimes [data-role="carousel-date-item"]')
    dates = browser._page.locator.return_value
    label = dates.filter.call_args.kwargs["has_text"]
    assert label.fullmatch("17 Dec\nTHU")
    assert not label.fullmatch("Thu, 17 Dec 2026")
    dates.filter.return_value.click.assert_called_once_with(timeout=8000)


def test_shaw_showtimes_use_display_date_and_hall_brand(tmp_path):
    raw = [
        {
            "primaryTitle": "Example (2D | INFINITY VISION)",
            "duration": 150,
            "showTimes": [
                {
                    "performanceId": 100,
                    "displayDate": "2026-12-17",
                    "displayTime": "12:01 AM",
                    "locationVenueName": "Example Hall",
                    "locationVenueBrandCode": "LUMRG",
                    "seatingStatus": "SO",
                    "formatCode": "DGSD",
                }
            ],
        }
    ]
    (session,) = shaw.parse_showtimes(raw, Config(tmp_path))
    assert session.starts_at == datetime(2026, 12, 17, 0, 1, tzinfo=SINGAPORE)
    assert session.sold_out
    assert session.premium_hall
    assert session.format == "2D | INFINITY VISION"


def test_gv_deduplicates_vivocity_and_preserves_midnight(tmp_path):
    date = datetime(2026, 12, 17, tzinfo=SINGAPORE)
    times = [
        {
            "time24": "0",
            "showDate": int(date.timestamp() * 1000),
            "hallNumber": "1",
            "soldPercent": 1.0,
        }
    ]
    raw = {
        "locations": [
            {"id": cinema_id, "name": name, "dates": [{"times": times}]}
            for cinema_id, name in (("051", "GVmax, VivoCity"), ("05", "GV VivoCity"))
        ]
    }
    film = gv.GvFilm("100", "Example", "ATMIV", 150, True)
    (session,) = gv.parse_sessions(raw, film, Config(tmp_path))
    assert session.starts_at == date
    assert session.sold_out
    assert "cinemaId/05/" in session.booking_url
    assert session.format == "Dolby Atmos Infinity Vision"
    assert gv.parse_sessions(None, film, Config(tmp_path)) == ()


@pytest.mark.parametrize("header", [True, False])
def test_gv_stops_at_challenge(tmp_path, header):
    calls = []

    def handle(request):
        calls.append(request)
        headers = {"cf-mitigated": "challenge"} if header else {}
        return httpx.Response(403, headers=headers, text="<title>Just a moment...</title>")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = gv.GoldenVillage(Config(tmp_path), gv.GvHttp(client)).poll(
            1, datetime(2026, 11, 24, tzinfo=SINGAPORE)
        )
    assert isinstance(result.error, Challenge)
    assert len(calls) == 1
    assert calls[0].url.path == "/.gv-api/advancesales"

from datetime import datetime

import httpx

from doomsday_checker.config import Config, Sources
from doomsday_checker.sources.common import SINGAPORE
from doomsday_checker.sources.gv_listings import GvListings, read_nowshowing, read_popcorn_status

MOVIE = """
<script type="application/ld+json">
{"@type":"Movie","name":"Avengers Doomsday","duration":"PT2H31M"}
</script>
<li class="cinemaRow">
    <div class="cinema-name"><a>GV - VivoCity</a></div>
    <a class="showtime-link" href="https://www.gv.com.sg/GVSeatSelection#/cinemaId/05/filmCode/1471/showDate/16-12-2026/showTime/0/hallNumber/4">00:00</a>
    <a class="showtime-link" href="https://www.gv.com.sg/GVSeatSelection#/cinemaId/05/filmCode/1490/showDate/17-12-2026/showTime/2225/hallNumber/1">22:25</a>
</li>
<li class="cinemaRow">
    <div class="cinema-name"><a>GV - GVmax, VivoCity</a></div>
    <a class="showtime-link" href="https://www.gv.com.sg/GVSeatSelection#/cinemaId/051/filmCode/1490/showDate/17-12-2026/showTime/2225/hallNumber/1">22:25</a>
</li>
"""

STATUSES = """
<div id="myShowtimes">
    <a data-toggle="tab" href="#day"><strong>17 Dec</strong></a>
    <div id="day"><div class="panel-body">
        <div class="row">
            <div class="cinemaname">Golden Village - VivoCity</div>
            <a class="showtime_btn seat_3">12:00AM<br><small>2D</small></a>
        </div>
    </div></div>
</div>
"""


def test_listings_correct_midnight_and_use_matching_availability(tmp_path):
    first, last = read_nowshowing(MOVIE, Config(tmp_path), read_popcorn_status(STATUSES))
    assert first.starts_at == datetime(2026, 12, 17, tzinfo=SINGAPORE)
    assert "/showDate/17-12-2026/" in first.booking_url
    assert first.sold_out is True
    assert last.sold_out is None
    assert last.sold_status == "Unknown"
    assert last.runtime_minutes == 151
    assert not last.seat_maps_supported


def test_listings_poll_only_requests_public_pages(tmp_path):
    requests = []

    def handle(request):
        requests.append((request.method, request.url.host))
        return httpx.Response(200, text=STATUSES if "popcorn" in request.url.host else MOVIE)

    sources = Sources(gv_movie_slugs=("doomsday",))
    config = Config(tmp_path, sources=sources)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = GvListings(config, client).poll(0, datetime(2026, 11, 24, tzinfo=SINGAPORE))
    assert result.error is None
    assert len(result.sessions) == 2
    assert not result.layouts
    assert requests == [("GET", "www.popcorn.app"), ("GET", "nowshowing.app")]

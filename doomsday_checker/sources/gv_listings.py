"""Golden Village showtimes and availability labels from public listings."""

import json
import logging
import re
from datetime import datetime, time, timedelta
from urllib.parse import quote, urlsplit

import httpx
from bs4 import BeautifulSoup

from ..config import Config
from ..models import Cinema, Listing, Session
from .common import (
    SINGAPORE,
    BackOff,
    Challenge,
    PollingSource,
    SourceError,
    check_response,
    matches_title,
)
from .gv import BASE_URL, SEAT_FIELDS, canonical_cinema_id, seat_url

NOWSHOWING_URL = "https://nowshowing.app/movies"
POPCORN_URL = "https://www.popcorn.app/sg"
BOOKING_PATH = re.compile(
    r"/cinemaId/([0-9]+)/filmCode/([0-9]+)/showDate/([0-9]{2}-[0-9]{2}-[0-9]{4})"
    r"/showTime/([0-9]{1,4})/hallNumber/([0-9]+)"
)
DOOMSDAY_FORMATS = {
    "1395": ("2D", 150),
    "1471": ("2D Infinity Vision", 151),
    "1490": ("Dolby Atmos Infinity Vision", 151),
    "7508": ("Dolby Atmos Infinity Vision Marathon", 371),
    "7533": ("2D Fan Screening", 150),
}
POPCORN_STATUS = {
    "seat_0": "Available",
    "seat_1": "Filling up",
    "seat_2": "Selling out",
    "seat_3": "Sold out",
}
LOG = logging.getLogger(__name__)
ShowKey = tuple[str, int, int, time]


def _show_key(venue: str, start: datetime) -> ShowKey:
    venue = venue.casefold().removeprefix("golden village - ").removeprefix("gv - ")
    venue = venue.removesuffix(", great world city").removesuffix(", great world")
    return re.sub(r"[^a-z0-9]", "", venue), start.month, start.day, start.time()


def read_popcorn_status(html: str) -> dict[ShowKey, str | None]:
    page = BeautifulSoup(html, "html.parser")
    statuses: dict[ShowKey, str | None] = {}
    for tab in page.select("#myShowtimes [data-toggle=tab]"):
        label = tab.select_one("strong")
        panel_id = str(tab.get("href", "")).removeprefix("#")
        panel = page.find(id=panel_id)
        if label is None or panel is None:
            continue
        day = datetime.strptime(f"2000 {label.get_text(strip=True)}", "%Y %d %b").date()
        for row in panel.select(".panel-body > .row"):
            cinema = row.select_one(".cinemaname")
            if cinema is None:
                continue
            venue = cinema.get_text(" ", strip=True)
            if not venue.startswith("Golden Village - "):
                continue
            for show in row.select(".showtime_btn"):
                status = next(
                    (
                        POPCORN_STATUS[code]
                        for code in show.get("class", [])
                        if code in POPCORN_STATUS
                    ),
                    None,
                )
                label = next(show.stripped_strings, "").replace(" ", "")
                start = datetime.combine(day, datetime.strptime(label, "%I:%M%p").time())
                key = _show_key(venue, start)
                statuses[key] = status if statuses.get(key, status) == status else None
    return statuses


def _movie_details(page: BeautifulSoup) -> tuple[str, int | None]:
    for script in page.select("script[type='application/ld+json']"):
        try:
            data = json.loads(script.get_text())
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "Movie":
            continue
        title = str(data["name"])
        duration = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", str(data.get("duration")))
        minutes = 60 * int(duration[1] or 0) + int(duration[2] or 0) if duration else 0
        return title, minutes or None
    raise SourceError("NowShowing did not return movie information")


def read_nowshowing(
    html: str, config: Config, statuses: dict[ShowKey, str | None]
) -> tuple[Session, ...]:
    page = BeautifulSoup(html, "html.parser")
    title, runtime = _movie_details(page)
    if not matches_title(title, config):
        return ()
    sessions = {}
    for row in page.select(".cinemaRow"):
        cinema = row.select_one(".cinema-name")
        if cinema is None:
            continue
        venue = cinema.get_text(" ", strip=True)
        for link in row.select("a.showtime-link"):
            url = urlsplit(str(link.get("href", "")))
            if url.scheme != "https" or url.netloc != urlsplit(BASE_URL).netloc:
                continue
            if url.path != "/GVSeatSelection":
                continue
            match = BOOKING_PATH.fullmatch(url.fragment)
            if match is None:
                raise SourceError("NowShowing returned an invalid Golden Village showtime")
            cinema_id, film, show_date, show_time, hall = match.groups()
            day = datetime.strptime(show_date, "%d-%m-%Y").date()
            hour, minute = divmod(int(show_time), 100)
            if hour == minute == 0:
                # NowShowing uses GV's preceding listing date for midnight links.
                day += timedelta(days=1)
            starts_at = datetime.combine(day, time(hour, minute), SINGAPORE)
            body = (
                canonical_cinema_id(cinema_id),
                film,
                day.strftime("%d-%m-%Y"),
                str(int(show_time)),
                hall,
            )
            session_id = "|".join(body)
            status = statuses.get(_show_key(venue, starts_at))
            previous = sessions.get(session_id)
            if previous is not None and status is None:
                continue
            format_name, default_runtime = DOOMSDAY_FORMATS.get(film, ("Unknown", 151))
            sessions[session_id] = Session(
                cinema=Cinema.GV,
                id=session_id,
                title=title,
                venue=f"{venue.removeprefix('GV - ')}, Hall {hall}",
                format=format_name,
                starts_at=starts_at,
                runtime_minutes=runtime or default_runtime,
                sold_status=status or "Unknown",
                sold_out=status == "Sold out" if status is not None else None,
                booking_url=seat_url(dict(zip(SEAT_FIELDS, body, strict=True))),
                seat_maps_supported=False,
            )
    return tuple(sessions.values())


class GvListings(PollingSource):
    cinema = Cinema.GV
    listing_interval = 300
    layout_interval = None

    def __init__(self, config: Config, client: httpx.Client):
        super().__init__(config)
        self._client = client
        self._popcorn_paused = False

    def _get(self, url: str) -> str:
        try:
            response = self._client.get(url)
        except httpx.HTTPError as error:
            domain = urlsplit(url).hostname
            raise SourceError(f"{domain} connection failed ({type(error).__name__})") from None
        check_response(
            response.status_code, response.headers.get("cf-mitigated", ""), response.text
        )
        return response.text

    def _get_statuses(self) -> dict[ShowKey, str | None]:
        path = self.config.sources.gv_popcorn_path
        if not path or self._popcorn_paused:
            return {}
        try:
            return read_popcorn_status(self._get(f"{POPCORN_URL}/{quote(path, safe='/')}"))
        except (Challenge, BackOff) as error:
            self._popcorn_paused = True
            LOG.warning("Popcorn availability paused: %s", error)
        except (SourceError, ValueError) as error:
            LOG.warning("Popcorn availability unavailable: %s", error)
        return {}

    def _load_listings(self, now: float) -> tuple[tuple[Listing, ...], tuple[Session, ...]]:
        statuses = self._get_statuses()
        listings = {}
        sessions = {}
        for slug in self.config.sources.gv_movie_slugs:
            html = self._get(f"{NOWSHOWING_URL}/{quote(slug, safe='')}")
            for session in read_nowshowing(html, self.config, statuses):
                sessions[session.id] = session
                film = session.id.split("|")[1]
                listing = listings.get(film)
                on_sale = session.sold_out is not True or (listing is not None and listing.on_sale)
                listings[film] = Listing(film, session.title, on_sale, session.format == "2D")
        return tuple(listings.values()), tuple(sessions.values())

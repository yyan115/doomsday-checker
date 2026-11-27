"""Shaw listing, showtime and seat layout data."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlencode

import httpx

from ..config import Config
from ..models import Cinema, Listing, Seat, SeatMap, Session
from .common import (
    JSON_ACCEPT,
    SINGAPORE,
    PollingSource,
    SourceError,
    check_response,
    fraction,
    jitter,
    matches_title,
    read_json,
)
from .shaw_browser import BASE_URL


class ShawTransport(Protocol):
    def begin_poll(self) -> None: ...

    def get(self, path: str, referer: str = "/") -> str: ...


class ShawHttp:
    def __init__(self, client: httpx.Client):
        self._client = client

    def begin_poll(self) -> None:
        pass

    def get(self, path: str, referer: str = "/") -> str:
        try:
            response = self._client.get(
                BASE_URL + path,
                headers={"Accept": JSON_ACCEPT, "Referer": BASE_URL + referer},
            )
        except httpx.HTTPError as error:
            raise SourceError(f"Shaw connection failed ({type(error).__name__})") from None
        check_response(
            response.status_code, response.headers.get("cf-mitigated", ""), response.text
        )
        return response.text


@dataclass(frozen=True)
class ShawMovie:
    id: str
    release_id: str | None
    title: str

    @property
    def page(self) -> str:
        return f"/movie-details/{self.release_id}" if self.release_id else "/"


def movie_cards(html: str, config: Config) -> dict[str, str]:
    decoder = json.JSONDecoder()
    frames = []
    for fragment in html.split("self.__next_f.push(")[1:]:
        try:
            frame, _ = decoder.raw_decode(fragment)
        except ValueError:
            continue
        if isinstance(frame, list) and len(frame) >= 2 and isinstance(frame[1], str):
            frames.append(frame[1])
    cards = {}
    for match in re.finditer(r'\{[^{}]*"movieReleaseId"[^{}]*\}', "".join(frames)):
        try:
            card = json.loads(match.group())
        except ValueError:
            continue
        title = card.get("primaryTitle")
        if isinstance(title, str) and matches_title(title, config):
            cards[str(card["movieReleaseId"])] = title
    return cards


def parse_showtimes(raw: list, config: Config) -> tuple[Session, ...]:
    sessions = []
    premium_halls = {code.upper() for code in config.seats.shaw_premium_halls}
    for movie in raw:
        for show in movie["showTimes"]:
            session_id = str(show["performanceId"])
            start = datetime.strptime(
                f"{show['displayDate']} {show['displayTime']}", "%Y-%m-%d %I:%M %p"
            ).replace(tzinfo=SINGAPORE)
            sold = show["seatingStatus"]
            title = show.get("primaryTitle") or movie["primaryTitle"]
            suffix = re.search(r"\(([^()]+)\)\s*$", title)
            format_name = (
                suffix[1] if suffix else {"DGSD": "2D"}.get(show["formatCode"], show["formatCode"])
            )
            sessions.append(
                Session(
                    cinema=Cinema.SHAW,
                    id=session_id,
                    title=title,
                    venue=show["locationVenueName"],
                    format=format_name,
                    starts_at=start,
                    runtime_minutes=int(show.get("duration") or movie["duration"]),
                    sold_status={"AV": "Available", "SO": "Sold out"}.get(sold, "Unknown"),
                    sold_out={"AV": False, "SO": True}.get(sold),
                    booking_url=f"{BASE_URL}/showtimes/{session_id}",
                    premium_hall=show["locationVenueBrandCode"].upper() in premium_halls,
                )
            )
    return tuple(sessions)


def parse_layout(raw: list) -> SeatMap:
    elements = [element for element in raw if element["elementCategoryCode"] == "SEAT"]
    if not elements:
        return SeatMap(())
    xs = [element["anchorCoordinateX"] for element in elements]
    ys = [element["anchorCoordinateY"] for element in elements]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    screen = next((e for e in raw if e["elementCategoryCode"] == "SCREEN"), None)
    reversed_rows = screen is not None and screen["anchorCoordinateY"] > y_min
    seats = []
    for element, x, y in zip(elements, xs, ys, strict=True):
        row = element["rowReference"]
        depth = fraction(y, y_min, y_max)
        seats.append(
            Seat(
                label=f"{row}{element['columnReference']}",
                row=row,
                x=x,
                y=y,
                row_fraction=1 - depth if reversed_rows else depth,
                column_fraction=fraction(x, x_min, x_max),
                max_gap=1.5 * element["elementWidthX"],
                available=(
                    element["elementCode"] != "EL_HDS"
                    and element["isHouseSeat"] is False
                    and element["elementStatusCodeCurrent"] == "AV"
                ),
            )
        )
    return SeatMap(tuple(seats), row_tolerance=3)


class Shaw(PollingSource):
    cinema = Cinema.SHAW
    listing_interval = 75
    layout_interval = 60
    layout_on_discovery = True

    def __init__(self, config: Config, transport: ShawTransport):
        super().__init__(config)
        self._transport = transport
        self._home_at = self._selectors_at = 0.0
        self._home_movies: dict[str, ShawMovie] = {}
        self._selector_movies: dict[str, ShawMovie] = {}
        self._release_movie_ids: dict[str, str] = {}

    def poll(self, now: float, wall_time: datetime):
        self._transport.begin_poll()
        return super().poll(now, wall_time)

    def _get_json(self, endpoint: str, expected: type, referer: str = "/", **params):
        query = "?" + urlencode(params) if params else ""
        body = self._transport.get(f"/internal/{endpoint}{query}", referer)
        return read_json(body, expected)

    def _resolve_cards(self, cards: dict[str, str]) -> dict[str, ShawMovie]:
        movies = {}
        for release_id, title in cards.items():
            if release_id not in self._release_movie_ids:
                release = self._get_json(
                    "get_movie_release", dict, f"/movie-details/{release_id}", id=release_id
                )
                self._release_movie_ids[release_id] = str(release["movieId"])
            movie_id = self._release_movie_ids[release_id]
            movies[movie_id] = ShawMovie(movie_id, release_id, title)
        return movies

    def _discover(self, now: float) -> dict[str, ShawMovie]:
        refresh_home = now >= self._home_at
        refresh_selectors = now >= self._selectors_at
        cards = movie_cards(self._transport.get("/"), self.config) if refresh_home else {}
        if refresh_selectors:
            selectors = self._get_json("api/v2/performance-selectors", list)
            self._selector_movies = {
                str(item["code"]): ShawMovie(str(item["code"]), None, item["name"])
                for item in selectors
                if item["type"] == "Movie" and matches_title(item["name"], self.config)
            }
            self._selectors_at = now + jitter(300)
        if refresh_home:
            self._home_movies = self._resolve_cards(cards)
            self._home_at = now + jitter(600)
        if (refresh_home or refresh_selectors) and self._selector_movies.keys() - self._home_movies:
            movies = movie_cards(self._transport.get("/movie"), self.config)
            self._home_movies.update(self._resolve_cards(movies))
        return self._selector_movies | self._home_movies

    def _load_listings(self, now: float) -> tuple[tuple[Listing, ...], tuple[Session, ...]]:
        listings = []
        sessions = {}
        for movie in self._discover(now).values():
            dates = self._get_json("get_date_selectors", list, movie.page, movieId=movie.id)
            on_sale = bool(dates)
            release_id = movie.release_id
            if release_id:
                status = self._get_json(f"get_visual_status/{release_id}", dict, movie.page)
                on_sale = on_sale or status["status"] != "ComingSoon"
            for date in dates:
                shows = self._get_json(
                    "get_show_times",
                    list,
                    movie.page,
                    date=date["code"],
                    movieId=movie.id,
                    locationId=0,
                    promotionId=0,
                )
                if release_id is None and shows:
                    release_id = str(shows[0]["movieReleaseId"])
                sessions.update(
                    (session.id, session) for session in parse_showtimes(shows, self.config)
                )
            special = ("infinity vision", "imax", "marathon", "fan screening", "fans' screening")
            listings.append(
                Listing(
                    release_id or f"movie:{movie.id}",
                    movie.title,
                    on_sale,
                    not any(label in movie.title.casefold() for label in special),
                )
            )
        return tuple(listings), tuple(sessions.values())

    def _load_layout(self, session: Session) -> SeatMap:
        return parse_layout(
            self._get_json(
                "get_layouts", list, f"/showtimes/{session.id}", performanceId=session.id
            )
        )

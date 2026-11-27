"""Golden Village public film lists, showtimes and seat plans."""

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Protocol

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
    matches_title,
    read_json,
)

BASE_URL = "https://www.gv.com.sg"
FORMATS = {"2D": "2D", "2DIV": "2D Infinity Vision", "ATMIV": "Dolby Atmos Infinity Vision"}
CATALOGS = ("advancesales", "nowshowing", "comingsoon")
SEAT_FIELDS = ("cinemaId", "filmCode", "showDate", "showTime", "hallNumber")


def seat_url(body: dict[str, str]) -> str:
    parameters = "/".join(f"{field}/{body[field]}" for field in SEAT_FIELDS)
    return f"{BASE_URL}/GVSeatSelection#/{parameters}"


def canonical_cinema_id(cinema_id: str) -> str:
    return "05" if cinema_id == "051" else cinema_id


@dataclass(frozen=True)
class GvFilm:
    code: str
    title: str
    format_code: str
    runtime_minutes: int
    on_sale: bool


def parse_sessions(raw: dict | None, film: GvFilm, config: Config) -> tuple[Session, ...]:
    if raw is None:
        return ()
    sessions = {}
    format_name = FORMATS.get(film.format_code, film.format_code)
    for venue in raw["locations"]:
        cinema_id = canonical_cinema_id(venue["id"])
        for date in venue["dates"]:
            for show in date["times"]:
                day = datetime.fromtimestamp(int(show["showDate"]) / 1000, SINGAPORE).date()
                hour, minute = divmod(int(show["time24"]), 100)
                starts_at = datetime.combine(day, time(hour, minute), SINGAPORE)
                hall = show["hallNumber"]
                body = (cinema_id, film.code, day.strftime("%d-%m-%Y"), show["time24"], hall)
                session_id = "|".join(body)
                if session_id in sessions:
                    continue
                sold = float(show["soldPercent"])
                if not 0 <= sold <= 1:
                    raise SourceError("Golden Village returned an invalid sold percentage")
                booking_url = seat_url(dict(zip(SEAT_FIELDS, body, strict=True)))
                hall_description = f"{venue['name']} {film.title} {format_name}".casefold()
                sessions[session_id] = Session(
                    cinema=Cinema.GV,
                    id=session_id,
                    title=film.title,
                    venue=f"{venue['name']}, Hall {hall}",
                    format=format_name,
                    starts_at=starts_at,
                    runtime_minutes=film.runtime_minutes,
                    sold_status="Sold out" if sold == 1 else f"{sold:.0%} sold",
                    sold_out=sold == 1,
                    booking_url=booking_url,
                    premium_hall=any(
                        hall_name.casefold() in hall_description
                        for hall_name in config.seats.gv_premium_halls
                    ),
                )
    return tuple(sessions.values())


def parse_layout(raw: list) -> SeatMap:
    cells = [(index, cell) for index, row in enumerate(raw) for cell in row if cell["id"]]
    if not cells:
        return SeatMap(())
    columns = [int(cell["colNumber"]) for _, cell in cells]
    left, right = min(columns), max(columns)
    return SeatMap(
        tuple(
            Seat(
                label=f"{cell['rowId']}{cell['columnId']}",
                row=str(index),
                x=column,
                y=index,
                row_fraction=fraction(index, 0, len(raw) - 1),
                column_fraction=fraction(column, left, right),
                max_gap=1,
                available=cell["type"] == "N" and cell["status"] == "L",
            )
            for (index, cell), column in zip(cells, columns, strict=True)
        )
    )


class GvTransport(Protocol):
    def catalogs(self) -> dict[str, dict]: ...

    def sessions(self, film_code: str) -> dict: ...

    def layout(self, body: dict[str, str]) -> dict: ...


class GvHttp:
    def __init__(self, client: httpx.Client):
        self._client = client

    def _post(self, endpoint: str, body: dict[str, str]) -> dict:
        try:
            response = self._client.post(
                f"{BASE_URL}/.gv-api/{endpoint}",
                json=body,
                headers={
                    "Accept": JSON_ACCEPT,
                    "Origin": BASE_URL,
                    "Referer": f"{BASE_URL}/GVMovies",
                },
            )
        except httpx.HTTPError as error:
            raise SourceError(
                f"Golden Village connection failed ({type(error).__name__})"
            ) from None
        check_response(
            response.status_code, response.headers.get("cf-mitigated", ""), response.text
        )
        return read_json(response.text, dict)

    def catalogs(self) -> dict[str, dict]:
        return {endpoint: self._post(endpoint, {}) for endpoint in CATALOGS}

    def sessions(self, film_code: str) -> dict:
        return self._post("sessionforfilm", {"filmCode": film_code})

    def layout(self, body: dict[str, str]) -> dict:
        return self._post("seatplan", body)


def _data(result: dict, endpoint: str) -> Any:
    if result.get("success") is not True:
        raise SourceError(f"Golden Village rejected {endpoint}")
    data = result.get("data")
    if data is None and result.get("errorMessage") != "No record found.":
        raise SourceError(f"Golden Village returned no data for {endpoint}")
    return data


class GoldenVillage(PollingSource):
    cinema = Cinema.GV
    listing_interval = 150
    layout_interval = 180
    sold_out_interval = 300

    def __init__(self, config: Config, transport: GvTransport):
        super().__init__(config)
        self._transport = transport

    def _load_listings(self, now: float) -> tuple[tuple[Listing, ...], tuple[Session, ...]]:
        films: dict[str, GvFilm] = {}
        for endpoint, payload in self._transport.catalogs().items():
            for item in _data(payload, endpoint) or []:
                if not matches_title(item["filmTitle"], self.config):
                    continue
                film = GvFilm(
                    code=item["filmCd"],
                    title=item["filmTitle"].strip().removesuffix("*").rstrip(),
                    format_code=item["formatCode"],
                    runtime_minutes=int(item["duration"]),
                    on_sale=endpoint != "comingsoon" and item["buyTicket"] is True,
                )
                if film.code not in films or film.on_sale:
                    films[film.code] = film
        listings = []
        sessions = []
        for film in films.values():
            raw = _data(self._transport.sessions(film.code), "sessionforfilm")
            shows = parse_sessions(raw, film, self.config)
            sessions.extend(shows)
            special = ("marathon", "fan screening", "fans' screening", "gold class")
            standard = film.format_code == "2D" and not any(
                label in film.title.casefold() for label in special
            )
            listings.append(Listing(film.code, film.title, film.on_sale or bool(shows), standard))
        return tuple(listings), tuple(sessions)

    def _load_layout(self, session: Session) -> SeatMap | None:
        body = dict(zip(SEAT_FIELDS, session.id.split("|"), strict=True))
        raw = _data(self._transport.layout(body), "seatplan")
        return parse_layout(raw) if raw else None

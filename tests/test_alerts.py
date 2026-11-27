import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image, ImageColor

from doomsday_checker.config import Config, Schedule
from doomsday_checker.main import Monitor, _deliver, _queue_current_showtimes
from doomsday_checker.models import Cinema, Listing, Seat, SeatMap, Session
from doomsday_checker.notify import Telegram
from doomsday_checker.seat_image import PAIR
from doomsday_checker.sources.common import PollingSource, Snapshot, SourceError
from doomsday_checker.state import CHANNEL, PRIVATE, State


def showtime(cinema, *, sold_out, status, supports_layout):
    return Session(
        cinema=cinema,
        id="100",
        title="Example",
        venue="City Hall",
        format="2D | INFINITY VISION",
        starts_at=datetime(2026, 12, 16, 20, tzinfo=Schedule().timezone),
        runtime_minutes=150,
        sold_status=status,
        sold_out=sold_out,
        booking_url="https://example.com/book/100",
        seat_maps_supported=supports_layout,
    )


def monitor_for(session, layout, config, state):
    layouts = {session.id: layout} if layout is not None else {}
    snapshot = Snapshot(sessions=(session,), layouts=layouts)
    source = SimpleNamespace(
        cinema=session.cinema,
        next_poll=0,
        sessions=(session,),
        poll=lambda *_: snapshot,
    )
    return Monitor(config, state, [source])


@pytest.mark.parametrize(
    ("sold_out", "status"),
    [(True, "Sold out"), (None, "Unknown"), (False, "Selling out")],
)
def test_new_showtimes_are_alerted_with_or_without_availability(tmp_path, sold_out, status):
    session = showtime(Cinema.GV, sold_out=sold_out, status=status, supports_layout=False)
    config = Config(tmp_path, schedule=Schedule(busy_period=(time(9), time(21))))
    with closing(State(":memory:")) as state:
        monitor = monitor_for(session, None, config, state)
        assert monitor.poll()
        _, caption, photo, mode = state.next_message()
        assert caption.splitlines() == [
            "*NEW:* Wed 16 Dec, 8:00 PM · Outside schedule",
            "*Location: Golden Village · City Hall*",
            "Type: 2D Infinity Vision",
            f"Availability: {status}",
            "Link: [Book tickets](https://example.com/book/100)",
        ]
        assert photo is None
        assert mode == "MarkdownV2"
        with state.transaction():
            state.confirm_message(state.next_message()[0])
        assert monitor.poll()
        assert state.next_message() is None


def test_verified_pair_is_highlighted_and_uploaded_with_caption(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:example")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    session = showtime(Cinema.SHAW, sold_out=False, status="Available", supports_layout=True)
    session = replace(
        session, venue="City Hall (Grand)!", booking_url="https://example.com/book_(100)"
    )
    layout = SeatMap(
        (
            Seat("H11", "H", 0, 0, 0.5, 0, 1.5, False),
            Seat("H12", "H", 1, 0, 0.5, 0.4, 1.5, True),
            Seat("H13", "H", 2, 0, 0.5, 0.6, 1.5, True),
            Seat("H14", "H", 4, 0, 0.5, 1, 1.5, False),
        )
    )
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    path = tmp_path / "state.db"
    with closing(State(path)) as state:
        assert monitor_for(session, layout, Config(tmp_path), state).poll()
    with closing(State(path)) as state:
        _, caption, photo, mode = state.next_message()
        assert "Availability: Good pair · H12 and H13" in caption
        assert r"*Location: Shaw · City Hall \(Grand\)\!*" in caption
        assert r"[Book tickets](https://example.com/book_(100\))" in caption
        assert mode == "MarkdownV2"
        with Image.open(BytesIO(photo)) as image:
            assert image.format == "PNG"
            assert any(color == ImageColor.getrgb(PAIR) for _, color in image.getcolors(100000))
        with httpx.Client(transport=httpx.MockTransport(receive)) as client:
            assert _deliver(state, Telegram(client, tmp_path))
        assert state.next_message() is None
    (request,) = requests
    assert request.url.path.endswith("/sendPhoto")
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b"MarkdownV2" in request.content
    assert caption.encode() in request.content
    assert photo in request.content


def test_existing_text_outbox_survives_photo_migration(tmp_path):
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY, message TEXT NOT NULL)")
        db.execute("INSERT INTO outbox VALUES (1, 'Before upgrade')")
    with closing(State(path)) as state:
        assert state.next_message() == (1, "Before upgrade", None, None)
        assert state.next_message(PRIVATE) == state.next_message()
        assert state.next_message(CHANNEL) is None


def test_public_alerts_use_availability_without_personal_recommendations(tmp_path):
    session = showtime(Cinema.SHAW, sold_out=False, status="Available", supports_layout=True)
    session = replace(session, starts_at=datetime.now(UTC) + timedelta(days=1))
    config = Config(tmp_path, schedule=Schedule(busy_period=(time.min, time.max)))
    layout = SeatMap((Seat("H12", "H", 0, 0, 0.5, 0.5, 1.5, True),))
    source = SimpleNamespace(
        cinema=session.cinema,
        next_poll=0,
        sessions=(session,),
        poll=lambda *_: Snapshot(
            listings=(Listing("movie", "Example", True, True),),
            sessions=(session,),
            layouts={session.id: layout},
        ),
    )
    with closing(State(":memory:")) as state:
        monitor = Monitor(config, state, [source], public=True)
        assert monitor.poll()
        assert state.next_message(PRIVATE) is None
        message_id, caption, photo, mode = state.next_message(CHANNEL)
        assert caption.startswith("*NEW:*")
        assert "Availability: 1 seat available" in caption
        assert "Outside schedule" not in caption
        assert "pair" not in caption.lower()
        assert mode == "MarkdownV2"
        with Image.open(BytesIO(photo)) as image:
            assert all(color != ImageColor.getrgb(PAIR) for _, color in image.getcolors(100000))
        with state.transaction():
            state.confirm_message(message_id)
        assert monitor.poll()
        assert state.next_message() is None


def test_channel_history_is_queued_once_and_marks_showtimes_seen(tmp_path):
    session = showtime(Cinema.GV, sold_out=False, status="Available", supports_layout=False)
    session = replace(session, starts_at=datetime.now(UTC) + timedelta(days=1))
    config = Config(tmp_path)
    source = SimpleNamespace(
        cinema=session.cinema,
        next_poll=0,
        sessions=(session,),
        poll=lambda *_: Snapshot(sessions=(session,)),
    )
    path = tmp_path / "state.db"
    with closing(State(path)) as state:
        assert _queue_current_showtimes(config, state, [source], "-10079") == 0
        message_id, caption, _, _ = state.next_message(CHANNEL)
        assert caption.startswith("*SHOWTIME:*")
        with state.transaction():
            state.confirm_message(message_id)
    with closing(State(path)) as state:
        assert _queue_current_showtimes(config, state, [source], "-10079") == 0
        assert Monitor(config, state, [source], public=True).poll()
        assert state.next_message() is None


def test_private_and_channel_delivery_use_separate_destinations(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:example")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "-10079")
    monkeypatch.setattr("doomsday_checker.main.time.sleep", lambda _: None)
    calls = []

    def receive(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    with (
        closing(State(":memory:")) as state,
        httpx.Client(transport=httpx.MockTransport(receive)) as client,
    ):
        with state.transaction():
            state.enqueue("Showtime", b"PNG", audience=CHANNEL)
            state.enqueue("Record of a source error")
        telegram = Telegram(client, tmp_path)
        assert _deliver(state, telegram, PRIVATE)
        assert _deliver(state, telegram, CHANNEL)
        assert state.next_message() is None
    private, public = calls
    assert private.url.path.endswith("/sendMessage")
    assert b'"chat_id":"456"' in private.content
    assert public.url.path.endswith("/sendPhoto")
    assert b"\r\n\r\n-10079\r\n" in public.content


def test_discovery_attempts_sold_out_layout_and_continues_after_a_layout_error(tmp_path):
    unavailable = showtime(Cinema.SHAW, sold_out=True, status="Sold out", supports_layout=True)
    available = showtime(Cinema.SHAW, sold_out=False, status="Available", supports_layout=True)
    available = replace(available, id="200")
    calls = []

    class SampleSource(PollingSource):
        cinema = Cinema.SHAW
        listing_interval = 75
        layout_interval = 60
        layout_on_discovery = True

        def _load_listings(self, now):
            return (), (unavailable, available)

        def _load_layout(self, session):
            calls.append(session.id)
            if session.sold_out:
                raise SourceError("Layout unavailable")
            return SeatMap(())

    source = SampleSource(Config(tmp_path))
    earlier = datetime(2026, 12, 15, tzinfo=Schedule().timezone)
    first = source.poll(1000, earlier)
    assert calls == ["100", "200"]
    assert first.sessions == (unavailable, available)
    assert "200" in first.layouts
    assert first.error is not None
    source.poll(1200, earlier)
    assert calls == ["100", "200", "200"]

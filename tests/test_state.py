from contextlib import closing
from dataclasses import replace
from datetime import datetime

import httpx
import pytest

from doomsday_checker.config import Config
from doomsday_checker.main import Monitor, _deliver
from doomsday_checker.models import Cinema, Listing, SeatLevel, SeatPair, Session
from doomsday_checker.notify import Telegram
from doomsday_checker.sources.common import SINGAPORE, Challenge, Snapshot, SourceError
from doomsday_checker.state import State


def test_sales_open_only_after_standard_listing_goes_on_sale():
    with closing(State(":memory:")) as state, state.transaction():
        cinema = Cinema.SHAW
        state.observe_listings(cinema, (Listing("100", "Example", False, True),))
        assert state.next_message() is None
        state.observe_listings(cinema, (Listing("100", "Example", True, True),))
        assert state.next_message()[1].startswith("SALES OPENED")
        state.confirm_message(state.next_message()[0])
        state.observe_listings(cinema, (Listing("100", "Example", True, True),))
        assert state.next_message() is None


def test_pair_can_return_after_a_restart_and_disappearance(tmp_path):
    path = tmp_path / "state.db"
    session = Session(
        Cinema.SHAW,
        "100",
        "Example",
        "Hall 1",
        "2D",
        datetime(2026, 12, 17, tzinfo=SINGAPORE),
        150,
        "Available",
        False,
        "",
    )
    pair = SeatPair("C7", "C8", SeatLevel.GOOD)
    with closing(State(path)) as state, state.transaction():
        assert state.observe_pairs(session, [pair]) == [pair]
    with closing(State(path)) as state, state.transaction():
        assert state.observe_pairs(session, [pair]) == []
        assert state.observe_pairs(session, []) == []
        assert state.observe_pairs(session, [pair]) == [pair]


def test_unknown_status_does_not_reopen_a_sold_out_session():
    session = Session(
        Cinema.GV,
        "100",
        "Example",
        "Hall 1",
        "2D",
        datetime(2026, 12, 17, tzinfo=SINGAPORE),
        150,
        "Sold out",
        True,
        "",
    )
    with closing(State(":memory:")) as state, state.transaction():
        assert state.observe_session(session) == (True, False)
        assert state.observe_session(replace(session, sold_out=None)) == (False, False)
        assert state.observe_session(replace(session, sold_out=False)) == (False, True)


def test_heartbeat_reports_both_sources_and_marks_paused_polling():
    with closing(State(":memory:")) as state, state.transaction():
        state.set_session_count(Cinema.SHAW, 2)
        state.heartbeat(0, {Cinema.GV})
        state.heartbeat(21599, {Cinema.GV})
        assert state.next_message() is None
        state.heartbeat(21600, {Cinema.GV})
        assert "Shaw · 2 sessions seen" in state.next_message()[1]
        assert "Golden Village · 0 sessions seen · Paused" in state.next_message()[1]


def test_telegram_keeps_outbox_on_failure_and_clears_it_on_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:example")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    with (
        closing(State(":memory:")) as state,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        telegram = Telegram(client, tmp_path)
        with state.transaction():
            state.enqueue("Example alert")
        assert not _deliver(state, telegram)
        assert state.next_message()[1] == "Example alert"
        assert _deliver(state, telegram)
        assert state.next_message() is None
    assert len(requests) == 2
    assert requests[1].url.path == "/bot123:example/sendMessage"
    assert b"Example alert" in requests[1].content


def test_telegram_requires_local_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with httpx.Client() as client, pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        Telegram(client, tmp_path)


class FakeSource:
    next_poll = 0
    listing_interval = 1
    sessions = ()

    def __init__(self, cinema, snapshot):
        self.cinema = cinema
        self.snapshot = snapshot
        self.calls = 0

    def poll(self, now, wall_time):
        self.calls += 1
        return self.snapshot


def test_gv_challenge_pauses_only_gv_after_restart(tmp_path):
    gv = FakeSource(Cinema.GV, Snapshot(error=Challenge("Verification required")))
    shaw = FakeSource(Cinema.SHAW, Snapshot(listings=(), sessions=()))
    path = tmp_path / "state.db"
    with closing(State(path)) as state:
        monitor = Monitor(Config(tmp_path), state, [gv, shaw])
        assert not monitor.poll()
        assert state.next_message()[1].startswith("CHALLENGE")
    with closing(State(path)) as state:
        monitor = Monitor(Config(tmp_path), state, [gv, shaw])
        assert not monitor.poll()
    assert gv.calls == 1
    assert shaw.calls == 2


def test_reports_the_fifth_failure_and_keeps_polling_the_other_source(tmp_path, monkeypatch):
    gv = FakeSource(Cinema.GV, Snapshot(error=SourceError("Unavailable")))
    shaw = FakeSource(Cinema.SHAW, Snapshot(listings=(), sessions=()))
    now = iter(range(0, 100, 2))
    monkeypatch.setattr("doomsday_checker.main.time.monotonic", lambda: next(now))
    with closing(State(":memory:")) as state:
        monitor = Monitor(Config(tmp_path), state, [gv, shaw])
        for _ in range(4):
            assert not monitor.poll()
            assert state.next_message() is None
        assert not monitor.poll()
        assert state.next_message()[1].startswith("SOURCE FAILURE")
    assert shaw.calls == 5

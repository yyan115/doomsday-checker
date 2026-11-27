"""Command-line entry point and notification loop."""

import argparse
import logging
import os
import sqlite3
import time
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

import httpx

from .config import Config, load_config
from .models import Cinema, SeatLevel, SeatMap, SeatPair, Session
from .notify import MARKDOWN_V2, DeliveryError, Telegram, find_chat_id
from .rules import Timing, check_timing, find_pairs
from .seat_image import render_seat_map
from .sources.common import BackOff, Challenge, PollingSource, Snapshot, jitter
from .sources.gv import GoldenVillage, GvHttp
from .sources.gv_listings import GvListings
from .sources.shaw import Shaw, ShawHttp, ShawTransport
from .sources.shaw_browser import ShawBrowser
from .state import CHANNEL, PRIVATE, State

LOG = logging.getLogger(__name__)
MARKDOWN_ESCAPES = str.maketrans(
    {character: "\\" + character for character in "\\_*[]()~`>#+-=|{}.!"}
)


@dataclass(frozen=True)
class Observation:
    session: Session
    timing: Timing
    pairs: list[SeatPair] | None


def _time_label(moment: datetime) -> str:
    return f"{moment:%a} {moment.day} {moment:%b}, {moment.strftime('%I:%M %p').lstrip('0')}"


def _availability(
    session: Session, layout: SeatMap | None, pair: SeatPair | None, *, public: bool
) -> str:
    if session.sold_out:
        return "Sold out"
    if pair:
        return f"{pair.level.name.title()} pair · {pair.first} and {pair.second}"
    if layout is not None and layout.seats:
        count = sum(seat.available for seat in layout.seats)
        if count == 0:
            return "No seats available"
        if count == 1:
            return "1 seat available"
        return (
            f"{count} seats available" if public else f"{count} seats available · No suitable pair"
        )
    return session.sold_status or "Unknown"


def _showtime_message(
    event: str,
    session: Session,
    layout: SeatMap | None,
    pair: SeatPair | None,
    config: Config,
    *,
    public: bool,
    qualifies: bool = True,
) -> tuple[str, bytes | None]:
    start = _time_label(session.starts_at.astimezone(config.schedule.timezone))
    if not public and not qualifies:
        start += " · Outside schedule"
    if public:
        pair = None
    show_format = session.format.replace(" | ", " ").replace("INFINITY VISION", "Infinity Vision")
    location = f"{session.cinema} · {session.venue}"
    url = session.booking_url.replace("\\", "\\\\").replace(")", "\\)")
    availability = _availability(session, layout, pair, public=public)
    text = "\n".join(
        (
            f"*{event}:* {start.translate(MARKDOWN_ESCAPES)}",
            f"*Location: {location.translate(MARKDOWN_ESCAPES)}*",
            f"Type: {show_format.translate(MARKDOWN_ESCAPES)}",
            f"Availability: {availability.translate(MARKDOWN_ESCAPES)}",
            f"Link: [Book tickets]({url})",
        )
    )
    photo = None
    if layout is not None and layout.seats:
        checked = _time_label(datetime.now(config.schedule.timezone))
        try:
            photo = render_seat_map(layout, pair, checked)
        except (OSError, ValueError) as error:
            LOG.warning("Seat map image unavailable for %s: %s", session.id, error)
    return text, photo


def _matching_layout(session: Session, layout: SeatMap | None) -> SeatMap | None:
    if session.sold_out and layout is not None and any(seat.available for seat in layout.seats):
        return None
    return layout


class Monitor:
    def __init__(
        self, config: Config, state: State, sources: list[PollingSource], *, public: bool = False
    ):
        self.config = config
        self.state = state
        self.sources = sources
        self.public = public
        self.latest: dict[Cinema, dict[str, Observation]] = {}
        self._retry_at: dict[Cinema, float] = {}

    def _observation(self, session: Session, layout: SeatMap | None) -> Observation:
        timing = check_timing(session, self.config.schedule)
        pairs = None
        if layout is not None and not self.public:
            pairs = find_pairs(layout, self.config.seats, session.premium_hall)
        return Observation(session, timing, pairs)

    def _update_observations(self, source: PollingSource, snapshot: Snapshot) -> None:
        cinema = source.cinema
        if snapshot.listings is not None:
            self.state.observe_listings(cinema, snapshot.listings, announce=not self.public)
        previous = self.latest.get(cinema, {})
        current = {}
        if snapshot.sessions is not None:
            self.state.set_session_count(cinema, len(snapshot.sessions))
        now = datetime.now(UTC)
        for session in sorted(source.sessions, key=lambda session: session.starts_at):
            if self.public and session.starts_at <= now:
                continue
            initial = previous.get(session.id)
            layout = _matching_layout(session, snapshot.layouts.get(session.id))
            if snapshot.sessions is None and layout is None and initial is not None:
                current[session.id] = initial
                continue
            observed = self._observation(session, layout)
            if snapshot.sessions is not None:
                new_session, reopened = self.state.observe_session(session, layout is not None)
            else:
                new_session, reopened = False, False
            new_pairs = []
            if observed.pairs is not None:
                eligible = observed.pairs
                if not observed.timing.qualifies:
                    eligible = [
                        pair
                        for pair in eligible
                        if pair.level == SeatLevel.GOOD
                        and self.config.seats.notify_outside_schedule
                    ]
                new_pairs = self.state.observe_pairs(session, eligible)
            elif initial is not None and initial.session.sold_out == session.sold_out:
                observed = Observation(session, observed.timing, initial.pairs)
            current[session.id] = observed
            if new_session:
                event = "NEW"
                pair = observed.pairs[0] if observed.pairs and layout is not None else None
            elif reopened or new_pairs:
                event = "REOPENED" if self.public else "SEATS"
                pair = new_pairs[0] if new_pairs else None
            else:
                continue
            text, photo = _showtime_message(
                event,
                session,
                layout,
                pair,
                self.config,
                public=self.public,
                qualifies=observed.timing.qualifies,
            )
            self.state.enqueue(
                text, photo, MARKDOWN_V2, audience=CHANNEL if self.public else PRIVATE
            )
        self.latest[cinema] = current

    def _record_health(self, source: PollingSource, snapshot: Snapshot, now: float) -> None:
        cinema = source.cinema
        if snapshot.error is None:
            self.state.set_health(cinema, 0)
            self._retry_at.pop(cinema, None)
            return
        failures, _ = self.state.health(cinema)
        failures += 1
        challenged = isinstance(snapshot.error, Challenge)
        self.state.set_health(cinema, failures, challenged)
        LOG.warning("%s: %s", cinema, snapshot.error)
        if challenged:
            self.state.enqueue(f"CHALLENGE\n{cinema} requires verification. Polling paused.")
        else:
            delay = source.listing_interval
            if isinstance(snapshot.error, BackOff):
                delay *= 2 ** min(failures - 1, 5)
            self._retry_at[cinema] = now + min(900, jitter(delay))
            if failures == 5:
                self.state.enqueue(f"SOURCE FAILURE\n{cinema}: {snapshot.error}\n5 failed polls.")

    def poll(self) -> bool:
        healthy = True
        for source in self.sources:
            if self.state.health(source.cinema)[1]:
                healthy = False
                continue
            now = time.monotonic()
            if now < max(source.next_poll, self._retry_at.get(source.cinema, 0)):
                continue
            snapshot = source.poll(now, datetime.now(UTC))
            with self.state.transaction():
                self._update_observations(source, snapshot)
                self._record_health(source, snapshot, now)
            healthy = healthy and snapshot.error is None
        return healthy

    def rows(self) -> list[tuple[str, ...]]:
        rows = []
        results = (result for sessions in self.latest.values() for result in sessions.values())
        for observed in sorted(results, key=lambda result: result.session.starts_at):
            session, timing, pairs = observed.session, observed.timing, observed.pairs
            start = session.starts_at.astimezone(self.config.schedule.timezone)
            start_label = _time_label(start)
            if timing.late_night:
                start_label += " [LATE NIGHT]"
            best_pair = pairs[0].level.name if pairs else "None"
            if pairs is None and not session.sold_out:
                best_pair = "Unchecked" if session.seat_maps_supported else "Unavailable"
            rows.append(
                (
                    session.cinema,
                    session.venue,
                    session.format,
                    start_label,
                    session.sold_status,
                    "Yes" if timing.qualifies else "No",
                    best_pair,
                    timing.reason,
                )
            )
        return rows


def _print_table(rows: list[tuple[str, ...]]) -> None:
    if not rows:
        print("No matching showtimes returned.")
        return
    headers = ("Cinema", "Venue", "Format", "Start", "Sold", "Fits", "Best pair", "Reason")
    widths = [max(len(value) for value in column) for column in zip(headers, *rows, strict=True)]
    print("  ".join(value.ljust(width) for value, width in zip(headers, widths, strict=True)))
    for row in rows:
        print(
            "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip()
        )


def _deliver(state: State, telegram: Telegram, audience: str = PRIVATE) -> bool:
    try:
        while True:
            with state.transaction():
                pending = state.next_message(audience)
                if pending is None:
                    return True
                message_id, message, photo, parse_mode = pending
                started = time.monotonic()
                telegram.send(message, photo, parse_mode, public=audience == CHANNEL)
                state.confirm_message(message_id)
            if audience == CHANNEL:
                time.sleep(max(0, 1 - (time.monotonic() - started)))
    except DeliveryError as error:
        LOG.warning("%s Telegram delivery failed: %s", audience.capitalize(), error)
        return False


def _queue_current_showtimes(
    config: Config, state: State, sources: list[PollingSource], channel_id: str | None
) -> int:
    """Prepare the channel's first posts from a complete set of current listings."""
    if channel_id is not None and state.channel_history_queued(channel_id):
        print("Current showtimes have already been queued for this channel.")
        return 0

    listings = []
    for source in sources:
        snapshot = source.poll(time.monotonic(), datetime.now(UTC))
        if snapshot.error is not None or snapshot.sessions is None:
            LOG.error("Could not verify %s showtimes: %s", source.cinema, snapshot.error)
            return 1
        listings.extend(
            (session, _matching_layout(session, snapshot.layouts.get(session.id)))
            for session in snapshot.sessions
        )

    now = datetime.now(UTC)
    posts = []
    for session, layout in sorted(listings, key=lambda item: item[0].starts_at):
        if session.starts_at > now:
            text, photo = _showtime_message("SHOWTIME", session, layout, None, config, public=True)
            posts.append((session, text, photo))
    if not posts:
        LOG.error("No upcoming showtimes found")
        return 1

    count = len(posts)
    photos = sum(photo is not None for _, _, photo in posts)
    if channel_id is None:
        for _, text, photo in posts:
            print(f"{text}\nSeat map: {'included' if photo is not None else 'unavailable'}\n")
        print(f"Previewed {count} showtimes with {photos} seat maps.")
        return 0

    with state.transaction():
        if state.channel_history_queued(channel_id):
            print("Current showtimes have already been queued for this channel.")
            return 0
        for session, text, photo in posts:
            state.observe_session(session, has_layout=photo is not None)
            state.enqueue(text, photo, MARKDOWN_V2, audience=CHANNEL)
        state.record_channel_history(channel_id, now.timestamp())
    print(f"Queued {count} showtimes with {photos} seat maps for the Telegram channel.")
    return 0


def _run(args: argparse.Namespace, config: Config) -> int:
    with ExitStack() as stack:
        if args.resume_gv:
            state = State(config.state_path)
            stack.callback(state.close)
            with state.transaction():
                state.set_health(Cinema.GV, 0)
            print("Golden Village polling pause cleared.")
            return 0

        client = stack.enter_context(httpx.Client(timeout=12))
        if args.telegram_chat:
            chat_id = find_chat_id(client, config.directory, args.telegram_chat)
            print(f"TELEGRAM_CHAT_ID={chat_id}")
            return 0
        telegram = None if args.no_notify else Telegram(client, config.directory)
        if args.test_alert:
            telegram.send("TEST ALERT\nCinema watcher notifications are configured.")
            print("Telegram test alert sent.")
            return 0

        channel_id = telegram.channel_id if telegram is not None else None
        public = channel_id is not None
        if args.queue_current_showtimes and telegram is not None and not public:
            raise ValueError("Set TELEGRAM_CHANNEL_ID before queuing current showtimes")
        if public or args.queue_current_showtimes:
            config = replace(config, seats=replace(config.seats, notify_outside_schedule=True))

        state = State(":memory:" if args.no_notify else config.state_path)
        stack.callback(state.close)
        shaw_transport: ShawTransport = ShawHttp(client)
        if config.sources.shaw_access == "browser":
            shaw_transport = ShawBrowser(
                config.sources.chromium_executable,
                endpoint=os.environ.get("SHAW_BROWSER_WS_URL"),
            )
            stack.callback(shaw_transport.close)
        sources: list[PollingSource] = [Shaw(config, shaw_transport)]
        if config.sources.gv_enabled:
            if config.sources.gv_access == "listings":
                sources.append(GvListings(config, client))
            else:
                sources.append(GoldenVillage(config, GvHttp(client)))
        else:
            LOG.info("Golden Village polling is disabled in the configuration.")
        if args.queue_current_showtimes:
            return _queue_current_showtimes(config, state, sources, channel_id)
        if config.sources.gv_enabled and state.health(Cinema.GV)[1]:
            LOG.warning("Golden Village polling is paused pending verification.")
        monitor = Monitor(config, state, sources, public=public)
        disabled = set(Cinema) - {source.cinema for source in sources}

        audiences = (PRIVATE, CHANNEL) if public else (PRIVATE,)
        next_delivery = dict.fromkeys(audiences, 0.0)
        while True:
            healthy = monitor.poll()
            delivered = True
            if telegram is not None:
                with state.transaction():
                    state.heartbeat(time.time(), disabled)
                for audience in audiences:
                    if time.monotonic() >= next_delivery[audience] and not _deliver(
                        state, telegram, audience
                    ):
                        delivered = False
                        next_delivery[audience] = time.monotonic() + 60
            if args.once:
                _print_table(monitor.rows())
                return 0 if healthy and delivered else 1
            time.sleep(5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor cinema showtimes and seat availability.")
    parser.add_argument("--config", type=Path, help="Path to a TOML configuration file")
    parser.add_argument("--state-path", type=Path, help="Override the SQLite database location")
    parser.add_argument(
        "--bundled-chromium", action="store_true", help="Use the Playwright Chromium installation"
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--once", action="store_true", help="Poll once and print showtimes")
    parser.add_argument(
        "--no-notify", action="store_true", help="Disable Telegram and persistent state"
    )
    actions.add_argument("--test-alert", action="store_true", help="Send one Telegram test message")
    actions.add_argument(
        "--queue-current-showtimes",
        action="store_true",
        help="Queue current showtimes for the Telegram channel once, or preview with --no-notify",
    )
    actions.add_argument(
        "--telegram-chat", choices=("private", "group"), help="Look up a Telegram chat ID"
    )
    actions.add_argument(
        "--resume-gv", action="store_true", help="Clear a Golden Village polling pause"
    )
    args = parser.parse_args(argv)
    if args.no_notify and (args.test_alert or args.telegram_chat or args.resume_gv):
        parser.error(
            "--no-notify is incompatible with --test-alert, --telegram-chat and --resume-gv"
        )
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        config = load_config(args.config)
        if args.state_path is not None:
            config = replace(config, state_path=args.state_path.expanduser())
        if args.bundled_chromium:
            config = replace(config, sources=replace(config.sources, chromium_executable=None))
        if args.resume_gv and not config.sources.gv_enabled:
            parser.error("--resume-gv requires gv_enabled = true in the configuration")
        return _run(args, config)
    except KeyboardInterrupt:
        return 0
    except (
        DeliveryError,
        OSError,
        TypeError,
        ValueError,
        ZoneInfoNotFoundError,
        sqlite3.Error,
    ) as error:
        LOG.error("%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

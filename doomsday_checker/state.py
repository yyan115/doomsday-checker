"""Persistent observations and pending notifications."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .models import Cinema, Listing, SeatPair, Session

PRIVATE = "private"
CHANNEL = "channel"


class State:
    def __init__(self, path: Path | str):
        self._db = sqlite3.connect(path, timeout=15)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                cinema TEXT PRIMARY KEY,
                failures INTEGER NOT NULL DEFAULT 0,
                challenged INTEGER NOT NULL DEFAULT 0,
                catalog_seen INTEGER NOT NULL DEFAULT 0,
                session_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS listings (
                cinema TEXT NOT NULL,
                id TEXT NOT NULL,
                on_sale INTEGER NOT NULL,
                PRIMARY KEY (cinema, id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                cinema TEXT NOT NULL,
                id TEXT NOT NULL,
                sold_out INTEGER NOT NULL,
                PRIMARY KEY (cinema, id)
            );
            CREATE TABLE IF NOT EXISTS pairs (
                cinema TEXT NOT NULL,
                session_id TEXT NOT NULL,
                seat_a TEXT NOT NULL,
                seat_b TEXT NOT NULL,
                PRIMARY KEY (cinema, session_id, seat_a, seat_b)
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY,
                message TEXT NOT NULL,
                photo BLOB,
                parse_mode TEXT,
                audience TEXT NOT NULL DEFAULT 'private'
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value REAL NOT NULL
            );
        """)
        with self.transaction():
            columns = {column[1] for column in self._db.execute("PRAGMA table_info(outbox)")}
            if "photo" not in columns:
                self._db.execute("ALTER TABLE outbox ADD COLUMN photo BLOB")
            if "parse_mode" not in columns:
                self._db.execute("ALTER TABLE outbox ADD COLUMN parse_mode TEXT")
            if "audience" not in columns:
                self._db.execute(
                    "ALTER TABLE outbox ADD COLUMN audience TEXT NOT NULL DEFAULT 'private'"
                )

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            yield

    def health(self, cinema: Cinema) -> tuple[int, bool]:
        row = self._db.execute(
            "SELECT failures, challenged FROM sources WHERE cinema = ?", (cinema,)
        ).fetchone()
        return (row[0], bool(row[1])) if row else (0, False)

    def set_health(self, cinema: Cinema, failures: int, challenged: bool = False) -> None:
        self._db.execute(
            """
            INSERT INTO sources (cinema, failures, challenged) VALUES (?, ?, ?)
            ON CONFLICT (cinema) DO UPDATE SET
                failures = excluded.failures, challenged = excluded.challenged
            """,
            (cinema, failures, challenged),
        )

    def observe_listings(
        self, cinema: Cinema, listings: tuple[Listing, ...], *, announce: bool = True
    ) -> None:
        first_catalog = self._db.execute(
            "SELECT catalog_seen FROM sources WHERE cinema = ?", (cinema,)
        ).fetchone() != (1,)
        for listing in listings:
            previous = self._db.execute(
                "SELECT on_sale FROM listings WHERE cinema = ? AND id = ?", (cinema, listing.id)
            ).fetchone()
            self._db.execute(
                """
                INSERT INTO listings VALUES (?, ?, ?)
                ON CONFLICT (cinema, id) DO UPDATE SET on_sale = excluded.on_sale
                """,
                (cinema, listing.id, listing.on_sale),
            )
            if not announce:
                continue
            new_listing = previous is None and not first_catalog
            if (
                listing.on_sale
                and previous != (1,)
                and (listing.standard or new_listing or previous)
            ):
                self.enqueue(f"SALES OPENED\n{cinema} · {listing.title}")
            elif new_listing:
                self.enqueue(f"NEW LISTING\n{cinema} · {listing.title}")
        self._db.execute(
            """
            INSERT INTO sources (cinema, catalog_seen) VALUES (?, 1)
            ON CONFLICT (cinema) DO UPDATE SET catalog_seen = 1
            """,
            (cinema,),
        )

    def observe_session(self, session: Session, has_layout: bool = False) -> tuple[bool, bool]:
        key = session.cinema, session.id
        previous = self._db.execute(
            "SELECT sold_out FROM sessions WHERE cinema = ? AND id = ?", key
        ).fetchone()
        sold_out = session.sold_out
        if sold_out is None:
            sold_out = bool(previous and previous[0])
        self._db.execute(
            """
            INSERT INTO sessions VALUES (?, ?, ?)
            ON CONFLICT (cinema, id) DO UPDATE SET sold_out = excluded.sold_out
            """,
            (*key, sold_out),
        )
        if previous == (0,) and session.sold_out and not has_layout:
            self._db.execute("DELETE FROM pairs WHERE cinema = ? AND session_id = ?", key)
        return previous is None, previous == (1,) and session.sold_out is False

    def observe_pairs(self, session: Session, pairs: list[SeatPair]) -> list[SeatPair]:
        key = session.cinema, session.id
        previous = set(
            self._db.execute(
                "SELECT seat_a, seat_b FROM pairs WHERE cinema = ? AND session_id = ?", key
            )
        )
        current = {}
        for pair in pairs:
            first, second = sorted((pair.first, pair.second))
            current[first, second] = pair
        missing = previous - current.keys()
        new = current.keys() - previous
        self._db.executemany(
            "DELETE FROM pairs WHERE cinema = ? AND session_id = ? AND seat_a = ? AND seat_b = ?",
            ((*key, *pair) for pair in missing),
        )
        self._db.executemany(
            "INSERT INTO pairs VALUES (?, ?, ?, ?)",
            ((*key, *pair) for pair in new),
        )
        return [pair for labels, pair in current.items() if labels in new]

    def set_session_count(self, cinema: Cinema, count: int) -> None:
        self._db.execute(
            """
            INSERT INTO sources (cinema, session_count) VALUES (?, ?)
            ON CONFLICT (cinema) DO UPDATE SET session_count = excluded.session_count
            """,
            (cinema, count),
        )

    def heartbeat(self, now: float, disabled: set[Cinema]) -> None:
        row = self._db.execute("SELECT value FROM settings WHERE key = 'heartbeat'").fetchone()
        if row is not None and now < row[0] + 6 * 60 * 60:
            return
        self._db.execute(
            """
            INSERT INTO settings VALUES ('heartbeat', ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (now,),
        )
        if row is None:
            return
        sources = {
            cinema: (count, failures, challenged)
            for cinema, count, failures, challenged in self._db.execute(
                "SELECT cinema, session_count, failures, challenged FROM sources"
            )
        }
        lines = ["HEARTBEAT", "Watcher is running."]
        for cinema in Cinema:
            count, failures, challenged = sources.get(cinema, (0, 0, 0))
            status = ""
            if challenged or cinema in disabled:
                status = " · Paused"
            elif failures:
                status = f" · {failures} failed polls"
            lines.append(f"{cinema} · {count} sessions seen{status}")
        self.enqueue("\n".join(lines))

    def channel_history_queued(self, channel_id: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM settings WHERE key = ?", (f"channel_history:{channel_id}",)
            ).fetchone()
            is not None
        )

    def record_channel_history(self, channel_id: str, now: float) -> None:
        self._db.execute(
            "INSERT INTO settings VALUES (?, ?)", (f"channel_history:{channel_id}", now)
        )

    def enqueue(
        self,
        message: str,
        photo: bytes | None = None,
        parse_mode: str | None = None,
        *,
        audience: str = PRIVATE,
    ) -> None:
        self._db.execute(
            "INSERT INTO outbox (message, photo, parse_mode, audience) VALUES (?, ?, ?, ?)",
            (message, photo, parse_mode, audience),
        )

    def next_message(
        self, audience: str | None = None
    ) -> tuple[int, str, bytes | None, str | None] | None:
        if audience is not None:
            return self._db.execute(
                "SELECT id, message, photo, parse_mode FROM outbox WHERE audience = ? ORDER BY id LIMIT 1",
                (audience,),
            ).fetchone()
        return self._db.execute(
            "SELECT id, message, photo, parse_mode FROM outbox ORDER BY id LIMIT 1"
        ).fetchone()

    def confirm_message(self, message_id: int) -> None:
        self._db.execute("DELETE FROM outbox WHERE id = ?", (message_id,))

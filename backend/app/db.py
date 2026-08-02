from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

_pool: ConnectionPool | None = None


def init_pool() -> None:
    global _pool
    if _pool is not None:
        return
    settings = get_settings()
    _pool = ConnectionPool(
        conninfo=settings.dsn,
        min_size=1,
        max_size=10,
        kwargs={
            "row_factory": dict_row,
            # Every date boundary in this app is a local one, and the database
            # is not local by default.
            #
            # `snapshots.snapshot_date` is written by the desktop scraper as a
            # calendar date in America/New_York. The server's own timezone is
            # UTC, so `CURRENT_DATE` and `timestamptz::date` roll over at 20:00
            # Eastern — four hours before the data does. Between 20:00 and
            # midnight every comparison between the two was off by one: a
            # scrape that had just run reported `age_days = 1`, "days active"
            # gained a day, and the young-post filter excluded posts a day
            # early.
            #
            # Setting it on the connection fixes all of them at once, rather
            # than sprinkling AT TIME ZONE over the three call sites and
            # waiting for the fourth to be written without it. Instants are
            # unaffected — a timestamptz is the same moment whatever the
            # session renders it in, and every consumer here calls
            # .astimezone() rather than reading the tzinfo it arrives with.
            "options": f"-c timezone={settings.display_tz}",
        },
    )
    _pool.wait(timeout=10.0)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    """Borrow a connection from the pool. Autocommits at block exit."""
    if _pool is None:
        init_pool()
    assert _pool is not None
    with _pool.connection() as c:
        yield c


@contextmanager
def tx() -> Iterator[psycopg.Connection]:
    """Borrow a connection wrapped in a transaction. Rolls back on error."""
    if _pool is None:
        init_pool()
    assert _pool is not None
    with _pool.connection() as c:
        with c.transaction():
            yield c

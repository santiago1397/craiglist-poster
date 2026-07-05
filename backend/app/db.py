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
        kwargs={"row_factory": dict_row},
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

"""MySQL connection helpers. Importing this module never connects to the database."""

from __future__ import annotations

import sys
from collections.abc import Generator
from typing import Any

from .config import ConfigurationError, DatabaseSettings, load_settings


def connect(settings: DatabaseSettings | None = None) -> Any:
    """Open one PyMySQL connection using the supplied or environment settings."""
    import pymysql

    config = settings or load_settings()
    if config.query:
        raise ConfigurationError("DATABASE_URL query options are not supported by this connection helper")
    return pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.username,
        password=config.password,
        database=config.name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        init_command="SET time_zone = '+00:00'",
    )


def get_db() -> Generator[Any, None, None]:
    """Yield a request-scoped connection; always roll back and close it."""
    connection = connect()
    try:
        yield connection
    finally:
        active_error = sys.exception()
        cleanup_error: BaseException | None = None
        try:
            connection.rollback()
        except BaseException as exc:
            cleanup_error = exc
        try:
            connection.close()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if active_error is None and cleanup_error is not None:
            raise cleanup_error

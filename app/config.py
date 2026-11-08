"""Application and database configuration with legacy DB_* compatibility."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import URL, make_url


class ConfigurationError(ValueError):
    """A safe configuration error that never embeds setting values."""


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    name: str
    query: dict[str, str] = field(default_factory=dict, repr=False)


def _url_from_parts(values: Mapping[str, str]) -> URL:
    required = ("DB_HOST", "DB_USERNAME", "DB_PASSWORD", "DB_NAME")
    missing = [name for name in required if not values.get(name)]
    if missing:
        raise ConfigurationError("Incomplete database settings: " + ", ".join(missing))
    try:
        port = int(values.get("DB_PORT", "3306"))
    except ValueError as exc:
        raise ConfigurationError("DB_PORT must be an integer between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError("DB_PORT must be an integer between 1 and 65535")
    return URL.create(
        "mysql+pymysql",
        username=values["DB_USERNAME"],
        password=values["DB_PASSWORD"],
        host=values["DB_HOST"],
        port=port,
        database=values["DB_NAME"],
    )


def _database_url(values: Mapping[str, str]) -> str | None:
    configured_url = values.get("DATABASE_URL")
    db_names = {"DB_HOST", "DB_PORT", "DB_USERNAME", "DB_PASSWORD", "DB_NAME"}
    has_parts = any(name in values for name in db_names)
    if configured_url:
        return configured_url
    if has_parts:
        return _url_from_parts(values).render_as_string(hide_password=False)
    return None


@dataclass(frozen=True)
class Settings:
    database_url: str | None = field(default=None, repr=False)
    environment: str = "production"
    allowed_origins: tuple[str, ...] = ()
    cursor_signing_key: str | None = field(default=None, repr=False)
    local_http_cookie: bool = False
    rate_limit_enabled: bool = True

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Read current process-like values only; never loads a private .env implicitly."""
        values = os.environ if environ is None else environ
        origins = tuple(x.strip() for x in values.get("ALLOWED_ORIGINS", "").split(",") if x.strip())
        return cls(
            database_url=_database_url(values),
            environment=values.get("ENVIRONMENT", values.get("APP_ENV", "production")),
            allowed_origins=origins,
            cursor_signing_key=values.get("CURSOR_SIGNING_KEY"),
            local_http_cookie=values.get("LOCAL_HTTP_COOKIE", "false").lower() == "true",
            rate_limit_enabled=values.get("RATE_LIMIT_ENABLED", "true").lower() != "false",
        )


def load_settings(*, load_env_file: bool = True) -> DatabaseSettings:
    """Resolve settings in OS URL, OS DB_*, file URL, file DB_* precedence order."""
    process_values = dict(os.environ)
    has_os_url = bool(process_values.get("DATABASE_URL"))
    os_db_names = {"DB_HOST", "DB_PORT", "DB_USERNAME", "DB_PASSWORD", "DB_NAME"}
    has_os_db_parts = any(name in process_values for name in os_db_names)

    if has_os_url:
        database_url = process_values["DATABASE_URL"]
    elif has_os_db_parts:
        # Validate before loading .env so partial process configuration cannot silently
        # fall through to a different target from the project file.
        database_url = _url_from_parts(process_values).render_as_string(hide_password=False)
    else:
        if load_env_file:
            try:
                from dotenv import load_dotenv
            except ImportError as exc:
                raise ConfigurationError("python-dotenv is required to load the project .env file") from exc
            project_root = Path(__file__).resolve().parent.parent
            load_dotenv(project_root / ".env", override=False, interpolate=False)
        database_url = _database_url(os.environ)

    if not database_url:
        raise ConfigurationError("Set DATABASE_URL or DB_HOST, DB_USERNAME, DB_PASSWORD, and DB_NAME")
    try:
        parsed = make_url(database_url)
    except Exception as exc:
        raise ConfigurationError("DATABASE_URL is invalid") from exc
    if parsed.drivername != "mysql+pymysql" or not parsed.host or not parsed.database:
        raise ConfigurationError("DATABASE_URL must identify a mysql+pymysql database")
    return DatabaseSettings(
        host=parsed.host,
        port=parsed.port or 3306,
        username=parsed.username or "",
        password=parsed.password or "",
        name=parsed.database,
        query=dict(parsed.query),
    )

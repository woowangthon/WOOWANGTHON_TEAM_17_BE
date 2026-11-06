"""Environment-based database settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when required database configuration is invalid."""


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    name: str


def load_settings(*, load_env_file: bool = True) -> DatabaseSettings:
    """Read DB settings, preserving process environment values over the local .env."""
    if load_env_file:
        try:
            from dotenv import load_dotenv
        except ImportError as exc:
            raise ConfigurationError(
                "python-dotenv is required to load the project .env file"
            ) from exc
        project_root = Path(__file__).resolve().parent.parent
        load_dotenv(
            dotenv_path=project_root / ".env",
            override=False,
            interpolate=False,
        )

    names = ("DB_HOST", "DB_USERNAME", "DB_PASSWORD", "DB_NAME")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise ConfigurationError("Missing required environment variable(s): " + ", ".join(missing))

    raw_port = os.environ.get("DB_PORT", "3306")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ConfigurationError("DB_PORT must be an integer between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError("DB_PORT must be an integer between 1 and 65535")

    return DatabaseSettings(
        host=os.environ["DB_HOST"],
        port=port,
        username=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
        name=os.environ["DB_NAME"],
    )

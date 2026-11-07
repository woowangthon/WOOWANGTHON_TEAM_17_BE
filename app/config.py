from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str | None = None
    environment: str = "production"
    allowed_origins: tuple[str, ...] = ()
    cursor_signing_key: str | None = None
    local_http_cookie: bool = False
    rate_limit_enabled: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        origins = tuple(x.strip() for x in os.getenv("ALLOWED_ORIGINS", "").split(",") if x.strip())
        return cls(
            database_url=os.getenv("DATABASE_URL"),
            environment=os.getenv("ENVIRONMENT", os.getenv("APP_ENV", "production")),
            allowed_origins=origins,
            cursor_signing_key=os.getenv("CURSOR_SIGNING_KEY"),
            local_http_cookie=os.getenv("LOCAL_HTTP_COOKIE", "false").lower() == "true",
            rate_limit_enabled=os.getenv("RATE_LIMIT_ENABLED", "true").lower() != "false",
        )

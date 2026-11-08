"""Local PyCharm launcher for the backend and review HTML.

Importing this module is intentionally inert. Configuration and application
construction happen only when ``build_app`` or ``run_server`` is called.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from fastapi import FastAPI


ROOT = Path(__file__).resolve().parent
DEFAULT_FRONTEND_DIR = ROOT.parent / "WOOWANGTHON_TEAM_17_FE" / "dist"
LOCAL_ORIGIN = "http://127.0.0.1:8017"


class LauncherError(RuntimeError):
    """A safe, user-readable local setup error."""


def _is_loopback_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        if host == "localhost":
            return parsed.username is None and parsed.password is None and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
        return host in {"127.0.0.1", "::1"} and ipaddress.ip_address(host).is_loopback and parsed.username is None and parsed.password is None and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
    except ValueError:
        return False


def _resolve_frontend(value: str | Path | None) -> Path:
    if value is None:
        configured = os.environ.get("FRONTEND_DIR")
        path = Path(configured) if configured else DEFAULT_FRONTEND_DIR
    else:
        path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def _validate_settings(settings) -> None:
    missing = []
    if settings.environment.lower() not in {"development", "dev"}:
        missing.append("ENVIRONMENT=development")
    if not settings.database_url:
        missing.append("DATABASE_URL")
    else:
        try:
            from sqlalchemy.engine import make_url

            parsed_database_url = make_url(settings.database_url)
            if parsed_database_url.drivername != "mysql+pymysql":
                missing.append("DATABASE_URL must use mysql+pymysql")
        except Exception:
            missing.append("DATABASE_URL is invalid")
    if not settings.cursor_signing_key or len(settings.cursor_signing_key) < 32:
        missing.append("CURSOR_SIGNING_KEY (at least 32 characters)")
    if not settings.local_http_cookie:
        missing.append("LOCAL_HTTP_COOKIE=true")
    origins = settings.allowed_origins
    if LOCAL_ORIGIN not in origins:
        missing.append(f"ALLOWED_ORIGINS must include {LOCAL_ORIGIN}")
    if not origins or any(not _is_loopback_origin(origin) for origin in origins):
        missing.append("ALLOWED_ORIGINS must contain only loopback origins")
    if missing:
        raise LauncherError("Local launcher configuration is incomplete: " + ", ".join(missing) + ". Check the named settings in .env.")


def build_app(*, settings=None, env_file: str | Path | None = None, frontend_dir: str | Path | None = None) -> "FastAPI":
    """Build the local API app and attach the fixed review HTML routes.

    ``settings`` allows isolated tests to avoid reading any configuration.
    When omitted, the selected dotenv file is loaded at call time only.
    """
    if settings is None:
        from dotenv import load_dotenv
        from app.config import ConfigurationError, Settings

        process_values = dict(os.environ)
        has_process_url = bool(process_values.get("DATABASE_URL"))
        process_db_names = {"DB_HOST", "DB_PORT", "DB_USERNAME", "DB_PASSWORD", "DB_NAME"}
        has_process_db_parts = any(name in process_values for name in process_db_names)
        if has_process_db_parts and not has_process_url:
            # Force validation against the process environment before .env can fill
            # missing values or select another target.
            try:
                Settings.from_env(process_values)
            except ConfigurationError as exc:
                raise LauncherError(str(exc)) from None

        selected_env = Path(env_file) if env_file is not None else ROOT / ".env"
        if not selected_env.is_absolute():
            selected_env = ROOT / selected_env
        load_dotenv(selected_env, override=False, interpolate=False)

        resolved_values = dict(os.environ)
        if has_process_url:
            resolved_values["DATABASE_URL"] = process_values["DATABASE_URL"]
        elif has_process_db_parts:
            resolved_values["DATABASE_URL"] = Settings.from_env(process_values).database_url or ""
        try:
            settings = Settings.from_env(resolved_values)
        except ConfigurationError as exc:
            raise LauncherError(str(exc)) from None

    _validate_settings(settings)
    assets = _resolve_frontend(frontend_dir)
    index_file = assets / "index.html"
    room_file = assets / "room.html"
    absent = [name for name, path in (("index.html", index_file), ("room.html", room_file)) if not path.is_file()]
    if absent:
        raise LauncherError(
            f"Local review files are missing from {assets}: {', '.join(absent)}. "
            "Build WOOWANGTHON_TEAM_17_FE first with npm ci and npm run build, "
            "or set FRONTEND_DIR to an existing frontend build."
        )

    # Import only after local settings and review files are validated.
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from app.main import create_app

    application = create_app(settings)

    @application.get("/", include_in_schema=False)
    async def review_home():
        return FileResponse(index_file, media_type="text/html; charset=utf-8")

    @application.get("/index.html", include_in_schema=False)
    async def review_index():
        return FileResponse(index_file, media_type="text/html; charset=utf-8")

    @application.get("/room.html", include_in_schema=False)
    async def review_room():
        return FileResponse(room_file, media_type="text/html; charset=utf-8")

    preview_file = assets / "ui-preview.html"
    if preview_file.is_file():
        @application.get("/ui-preview.html", include_in_schema=False)
        async def review_common_ui():
            return FileResponse(preview_file, media_type="text/html; charset=utf-8")

    asset_directory = assets / "assets"
    if asset_directory.is_dir():
        application.mount("/assets", StaticFiles(directory=asset_directory), name="frontend-assets")

    return application


def run_server() -> None:
    """Start the local review server with the supported single-worker setup."""
    application = build_app()
    import uvicorn

    uvicorn.run(application, host="127.0.0.1", port=8017, workers=1, reload=False, access_log=False)


if __name__ == "__main__":
    try:
        run_server()
    except LauncherError as exc:
        print(str(exc))
        raise SystemExit(2) from None

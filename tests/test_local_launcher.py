from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import run
from app.config import Settings


def local_settings(**overrides) -> Settings:
    values = {
        "database_url": "mysql+pymysql://local:placeholder@127.0.0.1:3306/example",
        "environment": "development",
        "allowed_origins": ("http://127.0.0.1:8017", "http://localhost:8017"),
        "cursor_signing_key": "synthetic-local-cursor-key-that-is-long-enough",
        "local_http_cookie": True,
        "rate_limit_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def write_review_files(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "index.html").write_text("<h1>Home review</h1>", encoding="utf-8")
    (folder / "room.html").write_text("<h1>Room review</h1>", encoding="utf-8")
    return folder


def test_launcher_serves_only_fixed_review_pages_and_keeps_api_docs(tmp_path: Path):
    assets = write_review_files(tmp_path / "review")
    app = run.build_app(settings=local_settings(), frontend_dir=assets)

    with TestClient(app, base_url="http://127.0.0.1:8017") as client:
        assert "Home review" in client.get("/").text
        assert "Home review" in client.get("/index.html").text
        assert "Room review" in client.get("/room.html").text
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/.env").status_code == 404
        assert client.get("/room.html/../../.env").status_code == 404


def test_relative_frontend_directory_is_resolved_from_launcher_root_not_cwd(tmp_path: Path, monkeypatch):
    write_review_files(tmp_path / "project" / "review")
    monkeypatch.setattr(run, "ROOT", tmp_path / "project")
    monkeypatch.chdir(tmp_path)

    app = run.build_app(settings=local_settings(), frontend_dir="review")
    with TestClient(app, base_url="http://127.0.0.1:8017") as client:
        assert "Room review" in client.get("/room.html").text


def test_frontend_build_assets_and_preview_keep_private_files_inaccessible(tmp_path: Path):
    frontend = write_review_files(tmp_path / "dist")
    (frontend / "ui-preview.html").write_text("<h1>Common UI</h1>", encoding="utf-8")
    (frontend / ".env").write_text("NEVER_SERVE=private", encoding="utf-8")
    assets = frontend / "assets"
    assets.mkdir()
    (assets / "entry.js").write_text("console.log('entry')", encoding="utf-8")
    app = run.build_app(settings=local_settings(), frontend_dir=frontend)
    with TestClient(app, base_url="http://127.0.0.1:8017") as client:
        assert client.get("/assets/entry.js").status_code == 200
        assert "Common UI" in client.get("/ui-preview.html?preview=ui").text
        assert client.get("/.env").status_code == 404
        assert client.get("/assets/../.env").status_code == 404
        assert client.get("/assets/%2e%2e/.env").status_code == 404


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"environment": "production"}, "ENVIRONMENT=development"),
        ({"database_url": None}, "DATABASE_URL"),
        ({"database_url": "not-a-valid-secret-url"}, "DATABASE_URL"),
        ({"database_url": "sqlite:///secret.db"}, "DATABASE_URL"),
        ({"cursor_signing_key": "short"}, "CURSOR_SIGNING_KEY"),
        ({"local_http_cookie": False}, "LOCAL_HTTP_COOKIE=true"),
        ({"allowed_origins": ("https://example.com",)}, "ALLOWED_ORIGINS"),
        ({"allowed_origins": ("http://127.0.0.2:8017",)}, "ALLOWED_ORIGINS"),
    ],
)
def test_invalid_settings_fail_with_names_only(tmp_path: Path, override, expected):
    with pytest.raises(run.LauncherError) as caught:
        run.build_app(settings=local_settings(**override), frontend_dir=tmp_path)
    message = str(caught.value)
    assert expected in message
    assert "placeholder" not in message
    assert "secret" not in message
    assert "synthetic-local-cursor-key" not in message


def test_missing_review_files_are_reported_without_loading_factory(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(run, "DEFAULT_FRONTEND_DIR", tmp_path / "review")
    with pytest.raises(run.LauncherError, match="index.html, room.html"):
        run.build_app(settings=local_settings())


def test_dotenv_and_frontend_override_are_loaded_only_when_building_app(tmp_path: Path, monkeypatch):
    assets = write_review_files(tmp_path / "front")
    env_file = tmp_path / "synthetic.env"
    env_file.write_text(
        "\n".join(
            [
                "DATABASE_URL=mysql+pymysql://synthetic:placeholder@127.0.0.1:3306/example",
                "ENVIRONMENT=development",
                "ALLOWED_ORIGINS=http://127.0.0.1:8017,http://localhost:8017",
                "CURSOR_SIGNING_KEY=synthetic-config-key-that-is-at-least-32-characters",
                "LOCAL_HTTP_COOKIE=true",
                f"FRONTEND_DIR={assets}",
            ]
        ),
        encoding="utf-8",
    )
    for name in ("DATABASE_URL", "ENVIRONMENT", "ALLOWED_ORIGINS", "CURSOR_SIGNING_KEY", "LOCAL_HTTP_COOKIE", "FRONTEND_DIR"):
        monkeypatch.delenv(name, raising=False)

    app = run.build_app(env_file=env_file)
    assert app.state.settings.environment == "development"
    assert app.state.settings.database_url.endswith("/example")
    assert app.state.settings.local_http_cookie is True


def test_import_is_inert_and_does_not_load_dotenv_or_application(tmp_path: Path):
    repo = Path(run.__file__).resolve().parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, run; assert 'dotenv' not in sys.modules; assert 'app.main' not in sys.modules",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "run.py import should not load configuration or app modules"


def test_run_server_uses_one_loopback_worker_without_access_log(monkeypatch):
    observed = {}
    fake_app = object()
    fake_uvicorn = types.SimpleNamespace(run=lambda app, **kwargs: observed.update(app=app, **kwargs))
    monkeypatch.setattr(run, "build_app", lambda: fake_app)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    run.run_server()

    assert observed == {
        "app": fake_app,
        "host": "127.0.0.1",
        "port": 8017,
        "workers": 1,
        "reload": False,
        "access_log": False,
    }

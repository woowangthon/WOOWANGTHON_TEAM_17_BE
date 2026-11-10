from fastapi import FastAPI
import pytest

from app.generation_api import register_generation_routes
from preparation_helpers import base, create_room
from preparation_helpers import headers as prep_headers


class ApiError(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args)


def test_generation_router_registers_exact_feature_paths_without_touching_database():
    app = FastAPI()
    register_generation_routes(app, object(), object(), find_session=lambda *_: None,
                               cookie_name=lambda: "device", origin_check=lambda *_: None,
                               body_json=lambda *_: {}, ApiError=ApiError,
                               set_session_cookie=lambda *_: None)
    routes = {(method, route.path) for route in app.routes if hasattr(route, "methods")
              for method in route.methods if method not in {"HEAD", "OPTIONS"}}
    expected = {
        ("GET", "/api/v1/trips/{trip_id}/generation"),
        ("GET", "/api/v1/trips/{trip_id}/generation/preferences/me"),
        ("POST", "/api/v1/trips/{trip_id}/generation/preferences/me"),
        ("PUT", "/api/v1/trips/{trip_id}/generation/preferences/me/review"),
        ("PUT", "/api/v1/trips/{trip_id}/generation/settings"),
        ("GET", "/api/v1/trips/{trip_id}/generation/places"),
        ("POST", "/api/v1/trips/{trip_id}/generation/jobs"),
        ("GET", "/api/v1/trips/{trip_id}/generation/jobs/{job_id}"),
        ("GET", "/api/v1/trips/{trip_id}/plans/{plan_id}"),
        ("POST", "/api/v1/trips/{trip_id}/plans/{plan_id}/save"),
    }
    assert expected <= routes


@pytest.mark.mysql
def test_generation_readiness_is_publicly_safe_before_personal_reviews(client):
    room = create_room(client)
    response = client.get(base(room) + "/generation")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["conditionsVersion"] == 0
    assert data["settingsVersion"] == 0
    assert data["settings"]["activityWindow"] == {"start": "09:00", "end": "18:00"}
    assert data["readyForGeneration"] is False
    assert {member["status"] for member in data["reviewMembers"]} == {"stale"}
    public = str(data)
    assert "originalText" not in public and "evidence" not in public and "memberScores" not in public


@pytest.mark.mysql
def test_generation_settings_rejects_owner_supplied_unvalidated_anchor_metadata(client):
    room = create_room(client)
    response = client.put(base(room) + "/generation/settings", headers=prep_headers(), json={
        "expectedConditionsVersion": 0, "expectedSettingsVersion": 0,
        "settings": {"activityWindow": {"start": "09:00", "end": "18:00"},
                     "departureAnchor": None, "returnAnchor": None,
                     "reviewedCommonFields": [{"field": "lodging", "reviewed": True,
                                               "anchors": [{"date": "2026-12-12", "contentid": 23,
                                                            "position": 0, "title": "forged"}]}]}})
    assert response.status_code == 400

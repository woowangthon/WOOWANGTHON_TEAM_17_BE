"""Generation foundations against the strictly isolated MySQL fixture only."""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, event, text
from starlette.requests import Request

from app import preparation
from app.generation_foundation import (
    GenerationFoundationError,
    capture_confirmed_preparation,
    normalize_snapshot,
)
from app.main import COOKIE_DEV
from app.place_catalog import PlaceCatalogError, load_place_catalog
from preparation_helpers import (
    base, confirm, create_room, member_client, save_conditions, save_personal,
)

pytestmark = pytest.mark.mysql


def authorizer(client):
    raw = client.cookies.get(COOKIE_DEV)
    request = Request({"type": "http", "headers": [(b"cookie", f"{COOKIE_DEV}={raw}".encode())]})

    def find_session(conn, request):
        credential = request.cookies.get(COOKIE_DEV)
        row = conn.execute(text(
            "SELECT id,expires_at,revoked_at FROM device_sessions "
            "WHERE credential_hash=:hash FOR UPDATE"
        ), {"hash": hashlib.sha256(credential.encode()).hexdigest()}).mappings().first()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if not row or row["revoked_at"] is not None or row["expires_at"] <= now:
            return None
        return dict(row)

    def authorize(conn, trip_id):
        return preparation._access(conn, request, trip_id, find_session, lambda: COOKIE_DEV, owner=True)

    return authorize


def ready_owner(client):
    room = create_room(client)
    assert save_conditions(client, room).status_code == 200
    assert save_personal(client, room, cv=1).status_code == 200
    assert confirm(client, room, cv=1).status_code == 200
    return room, authorizer(client)


def run_normalization(snapshot, engine, trip_id, authorize, provider):
    return asyncio.run(normalize_snapshot(
        snapshot, SimpleNamespace(supported_tags=("자연",), digest="fixture-catalog"),
        lambda: capture_confirmed_preparation(engine, trip_id, authorize), provider,
    ))


def test_mysql_capture_is_private_stable_and_does_not_mutate_documents(client, app):
    room, authorize = ready_owner(client)
    engine = app.state.engine
    before = client.get(base(room) + "/preferences/me").json()
    snapshot = capture_confirmed_preparation(engine, room["trip"]["id"], authorize)
    second = capture_confirmed_preparation(engine, room["trip"]["id"], authorize)
    assert snapshot.fingerprint == second.fingerprint
    assert "originalText" not in snapshot._document
    assert "테스트용 개인 원문" not in snapshot._document
    assert "nickname" not in snapshot._document
    assert "session" not in repr(snapshot).lower()
    observed = []

    def provider(preferences, tags):
        observed.append(preferences)
        assert tags == ["자연"]
        return {"terms": [], "unmapped": [], "walking": [], "conflictCandidates": [], "needsReview": True}

    result = run_normalization(snapshot, engine, room["trip"]["id"], authorize, provider)
    assert result["sourceFingerprint"] == snapshot.fingerprint
    assert observed[0][0]["memberRef"] == "p1"
    assert "memberId" not in observed[0][0]
    assert "originalText" not in observed[0][0]
    assert client.get(base(room) + "/preferences/me").json() == before


def test_mysql_capture_rejects_unconfirmed_and_nonowner(client, app):
    room = create_room(client)
    assert save_conditions(client, room).status_code == 200
    assert save_personal(client, room, cv=1).status_code == 200
    with pytest.raises(GenerationFoundationError):
        capture_confirmed_preparation(app.state.engine, room["trip"]["id"], authorizer(client))
    with member_client(app, room) as (other, _):
        with pytest.raises(GenerationFoundationError):
            capture_confirmed_preparation(app.state.engine, room["trip"]["id"], authorizer(other))


def test_mysql_real_ai_adapter_preserves_confirmed_walking_and_review_boundary(client, app, monkeypatch):
    from chungbuk_trip_ai import normalization
    from chungbuk_trip_ai.gemini import GeminiSettings

    room, authorize = ready_owner(client)
    engine = app.state.engine
    snapshot = capture_confirmed_preparation(engine, room["trip"]["id"], authorize)
    provider_payload = []
    proposal = {"items": [{"memberRef": "p1", "field": "likes", "tag": "자연",
                           "evidence": "자연", "polarity": "prefer"}]}
    response_body = json.dumps({"candidates": [{"finishReason": "STOP", "content": {
        "parts": [{"text": json.dumps(proposal, ensure_ascii=False)}]}}]}, ensure_ascii=False).encode()

    class FakeHTTP:
        def __init__(self, *_, **__):
            pass

        def request(self, *_, **kwargs):
            outer = json.loads(kwargs["body"])
            provider_payload.append(json.loads(outer["contents"][0]["parts"][0]["text"]))

        def getresponse(self):
            return SimpleNamespace(status=200, read=lambda _: response_body)

        def close(self):
            pass

    monkeypatch.setattr(normalization, "HTTPSConnection", FakeHTTP)
    monkeypatch.setattr(normalization, "load_gemini_settings", lambda: GeminiSettings("synthetic-not-real"))
    result = run_normalization(snapshot, engine, room["trip"]["id"], authorize, None)["normalization"]
    assert result["walking"] == [{"memberRef": "p1", "scope": "between_places", "maxMinutes": 20, "unlimited": False}]
    assert result["terms"][0]["importance"] == "preferred"
    assert result["terms"][0]["requiresReview"] is True
    assert result["needsReview"] is True
    assert set(provider_payload[0]["members"][0]) == {"memberRef", "likes", "avoid", "food"}


@pytest.mark.parametrize("edit", ["personal", "common", "unversioned_personal"])
def test_mysql_changes_while_ai_runs_discard_result_without_holding_trip_lock(client, app, edit):
    room, authorize = ready_owner(client)
    engine = app.state.engine
    trip_id = room["trip"]["id"]
    snapshot = capture_confirmed_preparation(engine, trip_id, authorize)

    def provider(*_):
        # These writes would block if capture kept its trip lock during AI work.
        if edit == "personal":
            assert save_personal(client, room, cv=1, pv=1, likes="새로운 확정 전 취향").status_code == 200
        elif edit == "common":
            assert save_conditions(client, room, version=1, region="청주").status_code == 200
        else:
            with engine.begin() as conn:
                conn.execute(text("UPDATE member_preparation_documents SET draft_json="
                                  "JSON_SET(draft_json,'$.likes','직접 변경된 취향') WHERE trip_id=:trip"), {"trip": trip_id})
        return {"terms": []}

    with pytest.raises(GenerationFoundationError):
        run_normalization(snapshot, engine, trip_id, authorize, provider)


@pytest.mark.parametrize("change", ["session", "closed", "left", "role"])
def test_mysql_authority_is_rechecked_after_provider(client, app, change):
    room, authorize = ready_owner(client)
    engine = app.state.engine
    trip_id = room["trip"]["id"]
    snapshot = capture_confirmed_preparation(engine, trip_id, authorize)

    def provider(*_):
        with engine.begin() as conn:
            if change == "session":
                conn.execute(text("UPDATE device_sessions SET revoked_at=UTC_TIMESTAMP(3)"))
            elif change == "closed":
                conn.execute(text("UPDATE trips SET status='CLOSED' WHERE id=:trip"), {"trip": trip_id})
            elif change == "left":
                conn.execute(text("UPDATE trip_members SET status='LEFT' WHERE trip_id=:trip"), {"trip": trip_id})
            else:
                conn.execute(text("UPDATE trip_members SET role='MEMBER' WHERE trip_id=:trip"), {"trip": trip_id})
        return {"terms": []}

    with pytest.raises(GenerationFoundationError):
        run_normalization(snapshot, engine, trip_id, authorize, provider)


def test_mysql_same_count_member_replacement_is_stale(client, app):
    room = create_room(client)
    with member_client(app, room) as (second, second_member):
        assert save_conditions(client, room, expectedPeople=2).status_code == 200
        for actor in (client, second):
            assert save_personal(actor, room, cv=1).status_code == 200
            assert confirm(actor, room, cv=1).status_code == 200
        engine = app.state.engine
        trip_id = room["trip"]["id"]
        authorize = authorizer(client)
        snapshot = capture_confirmed_preparation(engine, trip_id, authorize)

        def provider(*_):
            with engine.begin() as conn:
                conn.execute(text("UPDATE trip_members SET status='LEFT' WHERE id=:member"), {"member": second_member["id"]})
            with member_client(app, room, nickname="새 참여자") as (third, _):
                assert save_personal(third, room, cv=1).status_code == 200
                assert confirm(third, room, cv=1).status_code == 200
            return {"terms": []}

        with pytest.raises(GenerationFoundationError, match="STALE_GENERATION_INPUT"):
            run_normalization(snapshot, engine, trip_id, authorize, provider)


@pytest.fixture
def synthetic_catalog(app):
    """Only synthetic tables in conftest's guarded local test schema; no live DDL claim."""
    engine = app.state.engine
    metadata = MetaData()
    places = Table("places", metadata, Column("contentid", Integer), Column("title", String(100)),
                   Column("mapx", String(40)), Column("mapy", String(40)), Column("geoValidated", String(10)),
                   Column("privateUnknownColumn", String(100)))
    details = Table("place_details", metadata, Column("contentid", Integer),
                    Column("openingHoursText", String(100)), Column("feeText", String(100)))
    preferences = Table("place_preferences", metadata, Column("contentid", Integer),
                        Column("preferenceTags", String(100)), Column("natureScore", Integer))
    metadata.create_all(engine)
    try:
        with engine.begin() as conn:
            conn.execute(places.insert(), [
                {"contentid": 101, "title": "합성 장소A", "mapx": "127.4341", "mapy": "36.6286", "geoValidated": "yes", "privateUnknownColumn": "must omit"},
                {"contentid": 102, "title": "합성 장소B", "mapx": "36.6286", "mapy": "127.4341", "geoValidated": "yes", "privateUnknownColumn": "must omit"},
                {"contentid": 103, "title": "합성 장소C", "mapx": None, "mapy": None, "geoValidated": None, "privateUnknownColumn": None},
            ])
            conn.execute(details.insert(), [{"contentid": 101, "openingHoursText": None, "feeText": None},
                                            {"contentid": 102, "openingHoursText": "09:00-18:00", "feeText": "입장료 1000원"}])
            conn.execute(preferences.insert(), [{"contentid": 101, "preferenceTags": "원문|태그", "natureScore": 0}])
        yield engine
    finally:
        metadata.drop_all(engine)


def test_mysql_reflection_selects_known_fields_and_preserves_marker_identity(synthetic_catalog):
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(synthetic_catalog, "before_cursor_execute", observe)
    try:
        with synthetic_catalog.connect() as conn:
            catalog = load_place_catalog(conn, supported_tags=["자연"])
    finally:
        event.remove(synthetic_catalog, "before_cursor_execute", observe)
    assert statements and set(statements) <= {"SELECT", "SHOW", "DESCRIBE"}
    a = catalog.presentation_place(101)
    assert a["contentid"] == 101 and a["title"] == "합성 장소A"
    assert a["coordinate"] == {"latitude": 36.6286, "longitude": 127.4341, "crs": "WGS84"}
    assert a["feeLabel"] == "요금 확인 필요"
    assert "openingHoursText" not in a
    assert "privateUnknownColumn" not in a
    assert "coordinate" not in catalog.presentation_place(102)
    assert catalog.presentation_place(102)["feeLabel"] == "입장료 1000원"
    assert catalog.presentation_place(102)["openingHoursText"] == "09:00-18:00"
    assert catalog.presentation_place(103)["feeLabel"] == "요금 확인 필요"
    assert catalog.preferences[0]["natureScore"] == 0
    assert catalog.preferences[0]["preferenceTags"] == "원문|태그"


@pytest.mark.parametrize("mysql_type", ["BIGINT", "SMALLINT", "INT UNSIGNED", "VARCHAR(30)"])
def test_mysql_catalog_rejects_incompatible_contentid_types(synthetic_catalog, mysql_type):
    with synthetic_catalog.begin() as conn:
        conn.execute(text(f"ALTER TABLE places MODIFY contentid {mysql_type}"))
    with synthetic_catalog.connect() as conn, pytest.raises(PlaceCatalogError):
        load_place_catalog(conn)

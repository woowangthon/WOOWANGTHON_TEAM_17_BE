"""Plan persistence, FK boundaries and rollback on the isolated MySQL only."""
import copy
import json

import pytest
from sqlalchemy import text

from app.generation_foundation import capture_confirmed_preparation
from app.plan_store import PlanStoreError, create_generated_plan, get_plan_record, save_generated_plan
from app.plan_validation import validate_generated_plan
from app.routing import KakaoRouter
from preparation_helpers import create_room, save_conditions, save_personal, confirm
from test_foundation_mysql import authorizer
from test_routing_validation import COORD, Catalog, Router

pytestmark = pytest.mark.mysql


@pytest.fixture
def stored(client, app):
    room = create_room(client)
    assert save_conditions(client, room, startDate="2026-12-12", endDate="2026-12-12",
                           lodging=None, fixedSchedule=None, transport="walk").status_code == 200
    assert save_personal(client, room, cv=1, likes="자연", avoid=None, food=None).status_code == 200
    assert confirm(client, room, cv=1).status_code == 200
    engine = app.state.engine
    trip_id, member_id = room["trip"]["id"], room["member"]["id"]
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO places(contentid,title) VALUES(11,'합성 장소')"))
    snapshot = capture_confirmed_preparation(engine, trip_id, authorizer(client))
    settings = {"activityWindow": {"start": "09:00", "end": "18:00"},
                "departureAnchor": {"label": "출발", "coordinate": COORD},
                "returnAnchor": {"label": "복귀", "coordinate": COORD}, "reviewedCommonFields": []}
    document = validate_generated_plan({"days": [{"date": "2026-12-12", "places": [{"contentid": 11}]}]},
                Catalog(), {"expectedPeople": 1, "budget": 10000, "transport": "walk"},
                [{"walking": "20"}], settings, Router(seconds=60))
    stamp = {"inputToken": "source-token", "conditionsVersion": 1, "catalogDigest": "synthetic"}
    return engine, trip_id, member_id, snapshot, document, stamp


def test_existing_plan_schema_preserves_generated_order_utc_and_private_boundary(stored):
    engine, trip_id, member_id, snapshot, document, stamp = stored
    with engine.begin() as conn:
        record = create_generated_plan(conn, trip_id, member_id, snapshot, document, stamp)
        public = record["public"]
        assert public["status"] == "DRAFT" and public["lockVersion"] == 1
        assert public["versionNo"] is None and public["savedAt"] is None
        nodes = conn.execute(text("SELECT order_index,active_order_index,start_at,place_id FROM itinerary_nodes "
                                  "WHERE version_id=:v ORDER BY order_index"), {"v": public["versionId"]}).mappings().all()
        assert [(n["order_index"], n["active_order_index"]) for n in nodes] == [(1, 1), (2, 2), (3, 3)]
        assert nodes[1]["place_id"] == 11 and nodes[1]["start_at"].hour == 0
        assert nodes[1]["start_at"].minute == 1
        assert conn.execute(text("SELECT COUNT(*) FROM itinerary_edges WHERE version_id=:v"), {"v": public["versionId"]}).scalar() == 2
        raw = conn.execute(text("SELECT input_snapshot FROM group_snapshots WHERE trip_id=:t"), {"t": trip_id}).scalar_one()
        assert "originalText" not in str(raw) and "confirmedPreferences" not in str(raw)
        assert get_plan_record(conn, "00000000-0000-4000-8000-000000000000", public["planId"]) is None


def test_unknown_plan_requires_acknowledgment_then_cas_saves_once(stored):
    engine, trip_id, member_id, snapshot, document, stamp = stored
    with engine.begin() as conn:
        record = create_generated_plan(conn, trip_id, member_id, snapshot, document, stamp)
    p = record["public"]
    with pytest.raises(PlanStoreError, match="UNVERIFIED_ACK_REQUIRED"), engine.begin() as conn:
        save_generated_plan(conn, trip_id, p["planId"], p["versionId"], member_id, 1, False, "source-token")
    with engine.begin() as conn:
        saved = save_generated_plan(conn, trip_id, p["planId"], p["versionId"], member_id, 1, True, "source-token")["public"]
        assert saved["status"] == "SAVED" and saved["lockVersion"] == 2 and saved["versionNo"] == 1
        assert saved["savedAt"].endswith("Z")
        pointers = conn.execute(text("SELECT current_version_id,active_draft_id FROM plans WHERE id=:p"), {"p": p["planId"]}).first()
        assert pointers == (p["versionId"], None)
    with pytest.raises(PlanStoreError, match="VERSION_CONFLICT"), engine.begin() as conn:
        save_generated_plan(conn, trip_id, p["planId"], p["versionId"], member_id, 1, True, "source-token")


def test_stale_source_and_known_violations_cannot_be_saved(stored):
    engine, trip_id, member_id, snapshot, document, stamp = stored
    document = copy.deepcopy(document)
    document["validation"]["status"] = "violated"
    document["validation"]["violations"] = [{"code": "WALKING_LIMIT_EXCEEDED", "date": "2026-12-12"}]
    with engine.begin() as conn:
        record = create_generated_plan(conn, trip_id, member_id, snapshot, document, stamp)
    p = record["public"]
    for token, code in [("changed", "STALE_GENERATION_INPUT"), ("source-token", "VALIDATION_FAILED")]:
        with pytest.raises(PlanStoreError, match=code), engine.begin() as conn:
            save_generated_plan(conn, trip_id, p["planId"], p["versionId"], member_id, 1, True, token)


def test_plan_and_all_children_rollback_in_one_transaction(stored):
    engine, trip_id, member_id, snapshot, document, stamp = stored
    with pytest.raises(RuntimeError), engine.begin() as conn:
        create_generated_plan(conn, trip_id, member_id, snapshot, document, stamp)
        raise RuntimeError("synthetic failure before job completion")
    with engine.connect() as conn:
        for table in ("plans", "plan_versions", "group_snapshots", "itinerary_nodes", "itinerary_edges"):
            assert conn.exec_driver_sql(f"SELECT COUNT(*) FROM `{table}`").scalar() == 0


def test_charter_logical_edges_are_preserved_without_fabricated_legacy_mode(stored):
    engine, trip_id, member_id, snapshot, document, stamp = stored
    settings = {"activityWindow": {"start": "09:00", "end": "18:00"},
                "departureAnchor": {"label": "출발", "coordinate": COORD},
                "returnAnchor": {"label": "복귀", "coordinate": COORD}, "reviewedCommonFields": []}
    document = validate_generated_plan({"days": [{"date": "2026-12-12", "places": [{"contentid": 11}]}]},
                Catalog(), {"expectedPeople": 1, "budget": 10000, "transport": "charter"},
                [{"walking": "20"}], settings, KakaoRouter())
    with engine.begin() as conn:
        record = create_generated_plan(conn, trip_id, member_id, snapshot, document, stamp)
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM itinerary_edges").scalar() == 0
        assert record["public"]["document"]["days"][0]["edges"][0]["mode"] == "charter"
        assert record["public"]["document"]["validation"]["route"]["status"] == "unknown"

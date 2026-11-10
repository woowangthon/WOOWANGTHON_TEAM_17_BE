import json
from contextlib import contextmanager
from datetime import datetime

import pytest

from app.generation_foundation import (
    GenerationFoundationError,
    capture_confirmed_preparation,
)


CONDITIONS = {"region": "충북", "startDate": "2026-12-12", "endDate": "2026-12-13",
              "expectedPeople": 1, "budget": 100000, "transport": "charter", "departure": None,
              "returnRoute": None, "lodging": None, "fixedSchedule": None}
DRAFT = {"originalText": "비밀 원문", "likes": "산책", "avoid": None, "avoidImportance": "required",
         "food": None, "foodImportance": "preferred", "walking": "20"}


class Mappings:
    def __init__(self, rows): self.rows = rows
    def first(self): return self.rows[0] if self.rows else None
    def all(self): return self.rows


class Result:
    def __init__(self, rows): self.rows = rows
    def mappings(self): return Mappings(self.rows)


class FakeConnection:
    def __init__(self): self.closed = False
    @contextmanager
    def begin(self): yield
    def execute(self, query, params):
        sql = str(query)
        if "trip_preparation_documents" in sql:
            return Result([{"conditions_json": json.dumps(CONDITIONS), "conditions_version": 4}])
        if "FROM trip_members" in sql:
            return Result([{"id": "member-1", "role": "OWNER", "status": "ACTIVE"}])
        if "member_preparation_documents" in sql:
            return Result([{"member_id": "member-1", "draft_json": json.dumps(DRAFT), "preference_version": 2,
                            "confirmed_conditions_version": 4, "confirmed_preference_version": 2,
                            "confirmed_at": datetime(2026, 1, 1)}])
        raise AssertionError("Unexpected query")
    def close(self): self.closed = True


class FakeEngine:
    def __init__(self): self.conn = FakeConnection()
    def connect(self): return self.conn


def authorize(conn, trip_id):
    return ({"id": "session-1"}, {"id": trip_id, "status": "OPEN"},
            {"id": "member-1", "role": "OWNER", "status": "ACTIVE"})


def test_capture_omits_original_text_and_detaches_preferences():
    engine = FakeEngine()
    snapshot = capture_confirmed_preparation(engine, "trip-1", authorize)
    prefs = snapshot.confirmed_preferences()
    assert prefs == [{"memberRef": "p1", "likes": "산책", "avoid": None, "avoidImportance": "required",
                      "food": None, "foodImportance": "preferred", "walking": "20", "scheduleItems": []}]
    assert "비밀 원문" not in repr(snapshot)
    prefs[0]["likes"] = "mutated"
    assert snapshot.confirmed_preferences()[0]["likes"] == "산책"
    assert engine.conn.closed
    class EditedConnection(FakeConnection):
        def execute(self, query, params):
            result = super().execute(query, params)
            if "member_preparation_documents" in str(query):
                changed = DRAFT | {"likes": "수정"}
                return Result([{**result.rows[0], "draft_json": json.dumps(changed)}])
            return result
    class EditedEngine:
        def connect(self): return EditedConnection()
    # Detect out-of-band edits even if a buggy writer leaves versions unchanged.
    assert capture_confirmed_preparation(EditedEngine(), "trip-1", authorize).fingerprint != snapshot.fingerprint


def test_capture_rejects_non_owner_and_unconfirmed_members_safely():
    with pytest.raises(GenerationFoundationError, match="NOT_AUTHORIZED"):
        capture_confirmed_preparation(FakeEngine(), "trip-1", lambda *_: (
            {"id": "s"}, {"id": "t", "status": "OPEN"}, {"id": "m", "role": "MEMBER", "status": "ACTIVE"}))

    class Unconfirmed(FakeConnection):
        def execute(self, query, params):
            result = super().execute(query, params)
            if "member_preparation_documents" in str(query):
                return Result([{**result.rows[0], "confirmed_at": None}])
            return result
    class Engine:
        def connect(self): return Unconfirmed()
    with pytest.raises(GenerationFoundationError, match="PREPARATION_NOT_READY"):
        capture_confirmed_preparation(Engine(), "trip-1", authorize)

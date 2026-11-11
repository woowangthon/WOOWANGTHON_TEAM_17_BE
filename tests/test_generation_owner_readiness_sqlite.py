"""Portable SQL-backed regression for owner readiness and private drafts."""
import json
from types import SimpleNamespace

from sqlalchemy import create_engine, text

from app.generation_service import GenerationService
from preparation_helpers import CONDITIONS, DRAFT


class SQLiteConnectionAdapter:
    """Execute production SQL against SQLite, dropping only MySQL lock syntax."""

    def __init__(self, conn):
        self.conn = conn
        self.draft_reads = []

    def execute(self, statement, params=None):
        sql = str(statement)
        if "SELECT draft_json" in sql:
            self.draft_reads.append((params or {}).get("m"))
        return self.conn.execute(text(sql.replace(" FOR UPDATE", "")), params or {})

    def __getattr__(self, name):
        return getattr(self.conn, name)


def test_generation_readiness_uses_owner_and_never_reads_unconfirmed_drafts():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE trip_members (id TEXT, trip_id TEXT, nickname TEXT, role TEXT, status TEXT, created_at TEXT)"))
        conn.execute(text("CREATE TABLE member_preparation_documents (trip_id TEXT, member_id TEXT, draft_json TEXT, preference_version INTEGER, confirmed_conditions_version INTEGER, confirmed_preference_version INTEGER, confirmed_at TEXT)"))
        conn.execute(text("CREATE TABLE trip_preparation_documents (trip_id TEXT, conditions_json TEXT, conditions_version INTEGER)"))
        conn.execute(text("CREATE TABLE generation_settings (trip_id TEXT, conditions_version INTEGER, settings_version INTEGER, settings_json TEXT)"))
        conn.execute(text("CREATE TABLE generation_jobs (id TEXT, trip_id TEXT, kind TEXT, status TEXT, phase TEXT, plan_id TEXT, version_id TEXT, error_code TEXT, created_at TEXT, updated_at TEXT, lease_expires_at TEXT)"))
        conn.execute(text("INSERT INTO trip_members VALUES ('owner','trip','owner','OWNER','ACTIVE','1'), ('guest','trip','guest','MEMBER','ACTIVE','2')"))
        conn.execute(text("INSERT INTO trip_preparation_documents VALUES (:t,:c,1)"), {
            "t": "trip", "c": json.dumps(CONDITIONS | {"lodging": None, "fixedSchedule": None}, ensure_ascii=False)})
        owner_draft = json.dumps(DRAFT | {"avoid": None, "food": None}, ensure_ascii=False)
        conn.execute(text("INSERT INTO member_preparation_documents VALUES ('trip','owner',:draft,1,1,1,'2026-11-11T00:00:00Z')"), {"draft": owner_draft})
        # Invalid JSON ensures an unchecked document must not be parsed.
        conn.execute(text("INSERT INTO member_preparation_documents VALUES ('trip','guest','not-json',8,NULL,NULL,NULL)"))

    adapters = []
    def connect():
        adapter = SQLiteConnectionAdapter(engine.connect())
        adapters.append(adapter)
        return adapter

    service = GenerationService(SimpleNamespace(state=SimpleNamespace()), engine, SimpleNamespace(), None, lambda: "device", None)
    service._conn = connect
    service._catalog = lambda: SimpleNamespace(digest="catalog-v1", preferences=[])
    service._auth = lambda conn, request, trip_id, owner=False: (
        {"id": "session"}, {"id": trip_id, "status": "OPEN"}, {"id": "owner", "role": "OWNER"})
    try:
        result = service.get_generation(object(), "trip")
        assert result["readyForGeneration"] is True, result["reasons"]
        assert result["settings"]["activityWindow"] == {"start": "09:00", "end": "20:00"}
        assert result["reasons"] == []
        assert result["preparationMembers"] == [
            {"id": "owner", "nickname": "owner", "status": "confirmed"},
            {"id": "guest", "nickname": "guest", "status": "writing"},
        ]
        assert adapters[0].draft_reads == ["owner", "owner"]
    finally:
        engine.dispose()

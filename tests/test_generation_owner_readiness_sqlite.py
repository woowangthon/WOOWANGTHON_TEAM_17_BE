"""Portable SQL-backed regression for owner readiness and private drafts."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace

from sqlalchemy import create_engine, event, text
import pytest

from app.generation_service import GenerationService, ServiceError, _canonical
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
        sql = sql.replace("JSON_UNQUOTE(JSON_EXTRACT(input_stamp_json,'$.inputToken'))", "json_extract(input_stamp_json,'$.inputToken')")
        return SQLiteResultAdapter(self.conn.execute(text(sql.replace(" FOR UPDATE", "")), params or {}))

    def __getattr__(self, name):
        return getattr(self.conn, name)


class SQLiteResultAdapter:
    def __init__(self, result):
        self.result = result

    @staticmethod
    def _row(row):
        if row is None:
            return None
        values = dict(row)
        for field in ("expires_at", "lease_expires_at", "created_at", "updated_at"):
            value = values.get(field)
            if isinstance(value, str):
                try:
                    values[field] = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    pass
        return values

    def mappings(self):
        return self

    def first(self):
        return self._row(self.result.mappings().first())

    def all(self):
        return [self._row(row) for row in self.result.mappings().all()]

    def scalar(self):
        return self.result.scalar()

    def scalar_one(self):
        return self.result.scalar_one()

    def __getattr__(self, name):
        return getattr(self.result, name)


@pytest.mark.parametrize("atomic_food_only", [False, True])
def test_generation_readiness_uses_owner_and_never_reads_unconfirmed_drafts(atomic_food_only):
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
        owner_data = DRAFT | {"avoid": None, "food": None}
        if atomic_food_only:
            from chungbuk_trip_ai import build_atomic_preferences
            original = "여행 중 초밥을 한 번 먹고 싶어요"
            profile = build_atomic_preferences([{"dimension": "food", "target": "초밥", "polarity": "prefer", "priority": "preferred",
                "priorityBasis": "unspecified", "scope": "trip", "audience": "unspecified", "frequency": {"count": 1, "operator": "exact", "period": "trip"},
                "value": None, "unit": None, "approximate": False, "evidence": original}], [], original,
                CONDITIONS | {"lodging": None, "fixedSchedule": None})
            owner_data = {**owner_data, "originalText": original, "likes": None, "walking": None, "food": "초밥", "atomicPreferences": profile}
            owner_data["_unresolvedAcknowledgements"] = {"sourceDigest": profile["sourceDigest"], "items": []}
        owner_draft = json.dumps(owner_data, ensure_ascii=False)
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
        if atomic_food_only:
            conn = service._conn()
            try:
                snapshot, self_draft, *_ = service._self_capture(conn, object(), "trip")
            finally:
                conn.close()
            assert self_draft["food"] == "초밥"
            assert "_unresolvedAcknowledgements" not in self_draft
            assert "_unresolvedAcknowledgements" not in json.dumps(snapshot.as_dict(), ensure_ascii=False)
    finally:
        engine.dispose()


def test_generation_jobs_coalesce_keys_without_adding_jobs_and_reopen_current_result(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    trip_id, owner_id, session_id = (str(uuid.uuid4()) for _ in range(3))
    event.listen(engine, "connect", lambda dbapi_conn, _: dbapi_conn.create_function("JSON_UNQUOTE", 1, lambda value: value))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE trip_members (id TEXT, trip_id TEXT, nickname TEXT, role TEXT, status TEXT, created_at TEXT)"))
        conn.execute(text("CREATE TABLE member_preparation_documents (trip_id TEXT, member_id TEXT, draft_json TEXT, preference_version INTEGER, confirmed_conditions_version INTEGER, confirmed_preference_version INTEGER, confirmed_at TEXT)"))
        conn.execute(text("CREATE TABLE trip_preparation_documents (trip_id TEXT, conditions_json TEXT, conditions_version INTEGER)"))
        conn.execute(text("CREATE TABLE generation_settings (trip_id TEXT, conditions_version INTEGER, settings_version INTEGER, settings_json TEXT)"))
        conn.execute(text("CREATE TABLE api_idempotency (session_id TEXT, method TEXT, path TEXT, idempotency_key TEXT, fingerprint TEXT, state TEXT, trip_id TEXT, member_id TEXT, http_status INTEGER, response_json TEXT, created_at DATETIME, expires_at DATETIME, UNIQUE(session_id,method,path,idempotency_key))"))
        conn.execute(text("CREATE TABLE generation_jobs (id TEXT PRIMARY KEY, trip_id TEXT, member_id TEXT, session_id TEXT, kind TEXT, method TEXT, path TEXT, idempotency_key TEXT, fingerprint TEXT, lease_token TEXT, lease_expires_at DATETIME, status TEXT, phase TEXT, input_stamp_json TEXT, response_json TEXT, plan_id TEXT, version_id TEXT, error_code TEXT, created_at DATETIME, updated_at DATETIME, expires_at DATETIME, UNIQUE(session_id,method,path,idempotency_key))"))
        conn.execute(text("CREATE TABLE plans (id TEXT PRIMARY KEY, trip_id TEXT, lock_version INTEGER, active_draft_id TEXT, current_version_id TEXT)"))
        conn.execute(text("CREATE TABLE plan_versions (id TEXT PRIMARY KEY, plan_id TEXT, trip_id TEXT, status TEXT, version_no INTEGER, saved_at DATETIME, validation_result TEXT)"))
        conn.execute(text("INSERT INTO trip_members VALUES (:m,:t,'owner','OWNER','ACTIVE','1')"), {"m": owner_id, "t": trip_id})
        conn.execute(text("INSERT INTO trip_preparation_documents VALUES (:t,:c,1)"), {
            "t": trip_id, "c": json.dumps(CONDITIONS | {"lodging": None, "fixedSchedule": None}, ensure_ascii=False)})
        conn.execute(text("INSERT INTO member_preparation_documents VALUES (:t,:m,:draft,1,1,1,'2026-11-11T00:00:00Z')"), {
            "t": trip_id, "m": owner_id, "draft": json.dumps(DRAFT | {"avoid": None, "food": None}, ensure_ascii=False)})

    rate_calls = []
    service = GenerationService(SimpleNamespace(state=SimpleNamespace()), engine,
                                SimpleNamespace(rate_limit_enabled=True), None, lambda: "device", None)
    def connect():
        conn = engine.connect()
        # Model MySQL's trip FOR UPDATE with SQLite's writer lock. Each tab
        # has its own connection; both execute the real creation transaction.
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        return SQLiteConnectionAdapter(conn)
    service._conn = connect
    service._catalog = lambda: SimpleNamespace(digest="catalog-v1", preferences=[])
    service._auth = lambda conn, request, trip_id, owner=False: (
        {"id": session_id}, {"id": trip_id, "status": "OPEN"}, {"id": owner_id, "role": "OWNER"})
    service._confirmed_issues = lambda *args: []
    service._rate = lambda *args: rate_calls.append(args)
    service._job_public = lambda row: {"id": row["id"], "status": row["status"],
                                        "planId": row["plan_id"], "versionId": row["version_id"]}
    request = object()
    try:
        state_conn = service._conn()
        try:
            token, _, _ = service._source_token(state_conn, trip_id, 1, "catalog-v1", 0)
        finally:
            state_conn.close()
        body = {"expectedInputToken": token, "expectedSettingsVersion": 0}
        barrier = Barrier(2)
        def submit(key):
            barrier.wait(timeout=5)
            return key, service.create_job(request, trip_id, body, key)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(submit, [str(uuid.uuid4()), str(uuid.uuid4())]),
                              key=lambda outcome: outcome[1][1], reverse=True)
        (first_key, (first, first_status)), (second_key, (second, second_status)) = outcomes
        assert first_status == 202
        assert second_status == 200 and second["id"] == first["id"]
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT COUNT(*) FROM generation_jobs WHERE kind='GENERATE'")) == 1
            assert conn.scalar(text("SELECT COUNT(*) FROM api_idempotency")) == 1
        assert len(rate_calls) == 1

        receipt_retry, receipt_status = service.create_job(request, trip_id, body, second_key)
        assert receipt_status == 200 and receipt_retry["id"] == first["id"]
        with pytest.raises(ServiceError) as conflict:
            service.create_job(request, trip_id, {**body, "expectedSettingsVersion": 7}, second_key)
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"

        # Complete the canonical job with a current plan, then expire its job
        # retention window. Current plan state still makes it reusable.
        plan_id, version_id = str(uuid.uuid4()), str(uuid.uuid4())
        stamp = {"inputToken": token, "catalogDigest": "catalog-v1"}
        payload = {"schema": "generation.v1", "sourceStamp": stamp, "document": {}}
        with engine.begin() as conn:
            conn.execute(text("UPDATE generation_jobs SET status='SUCCEEDED',phase='complete',plan_id=:p,version_id=:v,expires_at=:expired WHERE id=:id"), {
                "p": plan_id, "v": version_id,
                "expired": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1), "id": first["id"]})
            conn.execute(text("INSERT INTO plans VALUES (:p,:t,1,:v,:v)"), {"p": plan_id, "v": version_id, "t": trip_id})
            conn.execute(text("INSERT INTO plan_versions VALUES (:v,:p,:t,'DRAFT',1,NULL,:result)"), {
                "v": version_id, "p": plan_id, "t": trip_id, "result": json.dumps(payload)})
        original, original_status = service.create_job(request, trip_id, body, first_key)
        assert original_status == 202 and original["id"] == first["id"]
        another, another_status = service.create_job(request, trip_id, body, str(uuid.uuid4()))
        assert another_status == 200 and another["id"] == first["id"]
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT COUNT(*) FROM generation_jobs WHERE kind='GENERATE'")) == 1
        assert len(rate_calls) == 1

        with engine.begin() as conn:
            conn.execute(text("INSERT INTO generation_settings VALUES (:t,1,1,:settings)"), {
                "t": trip_id, "settings": json.dumps({"activityWindow": {"start": "09:00", "end": "17:00"},
                                         "departureAnchor": None, "returnAnchor": None,
                                         "reviewedCommonFields": []})})
        state_conn = service._conn()
        try:
            new_token, _, _ = service._source_token(state_conn, trip_id, 1, "catalog-v1", 1)
        finally:
            state_conn.close()
        changed, changed_status = service.create_job(request, trip_id, {
            "expectedInputToken": new_token, "expectedSettingsVersion": 1}, str(uuid.uuid4()))
        assert changed_status == 202 and changed["id"] != first["id"]
        assert len(rate_calls) == 2
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT COUNT(*) FROM generation_jobs WHERE kind='GENERATE'")) == 2
    finally:
        engine.dispose()

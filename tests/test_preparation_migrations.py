from pathlib import Path

import pytest
from sqlalchemy import text

from preparation_helpers import CONDITIONS, base, create_room, save_conditions, save_personal
from test_security_migrations import _run_sql_script

pytestmark = pytest.mark.mysql


def test_preparation_migration_round_trip_preserves_existing_schema_and_rows(client, db_engine):
    room = create_room(client)
    trip_id = room["trip"]["id"]
    assert save_conditions(client, room).status_code == 200
    assert save_personal(client, room, cv=1).status_code == 200
    existing = ("trips", "trip_members", "trip_conditions", "device_sessions", "device_session_memberships", "api_idempotency")

    def snapshot():
        with db_engine.connect() as conn:
            return {name: (conn.exec_driver_sql(f"SHOW CREATE TABLE `{name}`").first()[1],
                           conn.exec_driver_sql(f"SELECT COUNT(*) FROM `{name}`").scalar()) for name in existing}

    before = snapshot()
    migration_dir = Path(__file__).resolve().parents[1] / "migrations"
    with db_engine.connect() as conn:
        _run_sql_script(conn, migration_dir / "002_preparation.down.sql")
        remaining = conn.execute(text("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()" )).scalars().all()
        assert all(name not in remaining for name in ("trip_preparation_documents", "member_preparation_documents", "preparation_interpret_requests"))
        _run_sql_script(conn, migration_dir / "002_preparation.up.sql")
    assert snapshot() == before
    prep = client.get(base(room) + "/preparation").json()
    assert prep["conditionsVersion"] == 0
    assert prep["conditions"] == {**{key: None for key in CONDITIONS}, "departureTime": "09:00"}
    personal = client.get(base(room) + "/preferences/me").json()
    assert personal["clarifications"] == []
    assert personal["draft"] == {"originalText": None, "likes": None, "avoid": None,
        "avoidImportance": "required", "food": None, "foodImportance": "preferred",
        "walking": None, "scheduleItems": []}
    assert prep["trip"]["id"] == trip_id

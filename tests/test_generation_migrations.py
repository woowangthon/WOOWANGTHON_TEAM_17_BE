from pathlib import Path
import re

import pytest
from sqlalchemy import text


ROOT = Path(__file__).resolve().parents[1]


def test_generation_migration_adds_only_three_trip_scoped_support_tables():
    up = (ROOT / "migrations" / "003_generation.up.sql").read_text(encoding="utf-8")
    down = (ROOT / "migrations" / "003_generation.down.sql").read_text(encoding="utf-8")
    created = re.findall(r"CREATE TABLE\s+`([^`]+)`", up, re.I)
    dropped = re.findall(r"DROP TABLE\s+`([^`]+)`", down, re.I)
    expected = ["generation_member_reviews", "generation_settings", "generation_jobs"]
    assert created == expected
    assert dropped == list(reversed(expected))
    assert "ON DELETE CASCADE" in up
    assert "JSON_TYPE(`normalization_json`) = 'OBJECT'" in up
    assert "JSON_TYPE(`settings_json`) = 'OBJECT'" in up
    assert "JSON_TYPE(`input_stamp_json`) = 'OBJECT'" in up
    assert "UNIQUE KEY `uq_generation_job_key` (`session_id`,`method`,`path`,`idempotency_key`)" in up
    assert "'NORMALIZE_SELF','GENERATE','SAVE_PLAN'" in up
    assert "'QUEUED','RUNNING','SUCCEEDED','FAILED','STALE'" in up


@pytest.mark.mysql
def test_generation_migration_round_trip_on_dedicated_empty_schema(db_engine):
    up = (ROOT / "migrations" / "003_generation.up.sql").read_text(encoding="utf-8")
    down = (ROOT / "migrations" / "003_generation.down.sql").read_text(encoding="utf-8")

    try:
        with db_engine.connect() as conn:
            for table in ("generation_jobs", "generation_settings", "generation_member_reviews"):
                count = conn.execute(text("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t"), {"t": table}).scalar()
                assert count == 1
        with db_engine.connect() as conn:
            for statement in down.split(";"):
                if statement.strip(): conn.exec_driver_sql(statement)
        with db_engine.connect() as conn:
            for table in ("generation_jobs", "generation_settings", "generation_member_reviews"):
                count = conn.execute(text("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t"), {"t": table}).scalar()
                assert count == 0
        with db_engine.connect() as conn:
            for statement in up.split(";"):
                if statement.strip(): conn.exec_driver_sql(statement)
    finally:
        with db_engine.connect() as conn:
            existing = {name for name in ("generation_jobs", "generation_settings", "generation_member_reviews")
                        if conn.execute(text("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t"), {"t": name}).scalar()}
        if existing != {"generation_jobs", "generation_settings", "generation_member_reviews"}:
            with db_engine.connect() as conn:
                for table in ("generation_jobs", "generation_settings", "generation_member_reviews"):
                    if table in existing:
                        conn.exec_driver_sql(f"DROP TABLE `{table}`")
                for statement in up.split(";"):
                    if statement.strip(): conn.exec_driver_sql(statement)

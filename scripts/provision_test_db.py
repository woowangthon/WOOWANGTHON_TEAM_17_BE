"""Provision only the explicitly named, isolated loopback MySQL test schema.

Never reads .env or DATABASE_URL. Existing application databases are not targets.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
TEST_SCHEMA = "osami_entry_test"
TABLES = (
    "generation_jobs", "generation_member_reviews", "generation_settings", "ai_requests",
    "itinerary_edges", "itinerary_nodes", "plan_versions", "plans", "group_snapshots",
    "preparation_interpret_requests", "member_preparation_documents", "trip_preparation_documents",
    "api_idempotency", "device_session_memberships", "device_sessions",
    "trip_conditions", "trip_members", "trips",
    "place_preferences", "place_details", "places",
)


def checked_url():
    value = os.environ.get("TEST_DATABASE_URL", "")
    if not value:
        raise SystemExit("Set TEST_DATABASE_URL for the isolated test instance first.")
    url = make_url(value)
    if (url.drivername != "mysql+pymysql" or url.host != "127.0.0.1"
            or url.port != 3317 or url.database != TEST_SCHEMA or url.query):
        raise SystemExit("Refusing target: require MySQL 127.0.0.1:3317/osami_entry_test.")
    return url


def run_sql(connection, sql: str):
    # These repository SQL files use ordinary statements, not routines or delimiters.
    sql = re.sub(r"^\s*--[^\n]*", "", sql, flags=re.MULTILINE)
    for statement in sql.split(";"):
        if statement.strip():
            connection.execute(text(statement.strip()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="Reset ONLY the explicitly listed disposable test tables.")
    args = parser.parse_args()
    url = checked_url()
    admin = create_engine(url.set(database=""), hide_parameters=True)
    with admin.begin() as conn:
        conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {TEST_SCHEMA} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
    admin.dispose()
    engine = create_engine(url, hide_parameters=True)
    with engine.begin() as conn:
        present = set(conn.execute(text("SHOW TABLES")).scalars())
        if present - set(TABLES):
            raise SystemExit("Refusing schema containing unrelated tables.")
        if present and not args.reset:
            raise SystemExit("Test schema already populated; use explicit --reset for disposable test data.")
        if args.reset:
            if "plans" in present:
                # Break only the two known pointer FKs in this disposable schema;
                # ordinary application writes never disable foreign-key checks.
                keys = conn.exec_driver_sql("SHOW CREATE TABLE plans").first()[1]
                for key in ("fk_plan_current", "fk_plan_draft"):
                    if f"CONSTRAINT `{key}`" in keys:
                        conn.exec_driver_sql(f"ALTER TABLE plans DROP FOREIGN KEY `{key}`")
            for table in TABLES:
                conn.execute(text(f"DROP TABLE IF EXISTS `{table}`"))
        reference = (ROOT / "docs/database-schema-reference.md").read_text(encoding="utf-8")
        for table in ("trips", "trip_members", "trip_conditions"):
            match = re.search(rf"### {table}\s+```sql\s+([\s\S]*?)```", reference)
            if not match:
                raise SystemExit(f"Missing exact baseline DDL: {table}")
            run_sql(conn, match.group(1))
        run_sql(conn, (ROOT / "migrations/001_device_entry.up.sql").read_text(encoding="utf-8"))
        run_sql(conn, (ROOT / "migrations/002_preparation.up.sql").read_text(encoding="utf-8"))
        run_sql(conn, (ROOT / "tests/fixtures/generation_schema.sql").read_text(encoding="utf-8"))
        run_sql(conn, (ROOT / "migrations/003_generation.up.sql").read_text(encoding="utf-8"))
    engine.dispose()
    print("Provisioned entry, preparation and generation fixtures on isolated loopback MySQL.")


if __name__ == "__main__":
    main()

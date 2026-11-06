"""Explicitly check database connectivity with a read-only SELECT 1."""

from __future__ import annotations

import sys

from .config import ConfigurationError, load_settings
from .database import connect


def main() -> int:
    connection = None
    try:
        settings = load_settings()
        connection = connect(settings)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
        if not row or row.get("1") != 1:
            print("Database check failed: unexpected response")
            return 1
        print("Database connection successful")
        return 0
    except ConfigurationError as exc:
        print(f"Database check failed: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("Database check failed: could not connect or run the check", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())

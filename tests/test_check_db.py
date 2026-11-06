from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import patch

from app import check_db
from app.config import DatabaseSettings


class CheckDatabaseTests(unittest.TestCase):
    def test_runs_select_one_and_closes_connection(self) -> None:
        events: list[str] = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, query):
                events.append(query)

            def fetchone(self):
                return {"1": 1}

        class Connection:
            def cursor(self):
                return Cursor()

            def close(self):
                events.append("close")

        with patch.object(check_db, "load_settings", return_value=DatabaseSettings(
            "localhost", 3306, "reader", "secret", "travel"
        )), patch.object(check_db, "connect", return_value=Connection()):
            self.assertEqual(check_db.main(), 0)
        self.assertEqual(events, ["SELECT 1", "close"])

    def test_driver_error_does_not_expose_exception_text(self) -> None:
        with patch.object(check_db, "load_settings", return_value=DatabaseSettings(
            "localhost", 3306, "reader", "never-print-this", "travel"
        )), patch.object(check_db, "connect", side_effect=RuntimeError("never-print-this")):
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                self.assertEqual(check_db.main(), 1)
        self.assertNotIn("never-print-this", output.getvalue())


if __name__ == "__main__":
    unittest.main()

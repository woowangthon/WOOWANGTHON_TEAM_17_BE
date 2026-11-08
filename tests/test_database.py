from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.config import DatabaseSettings
from app.config import ConfigurationError
from app.database import get_db


class ConnectionCleanupTests(unittest.TestCase):
    def test_legacy_pymysql_helper_refuses_database_url_options(self):
        config = DatabaseSettings("localhost", 3306, "reader", "secret", "travel", {"ssl": "true"})
        with self.assertRaisesRegex(ConfigurationError, "query options"):
            from app.database import connect

            connect(config)

    def test_import_and_dependency_creation_do_not_connect_until_iterated(self) -> None:
        driver = SimpleNamespace(cursors=SimpleNamespace(DictCursor=object()))
        fake_connection = SimpleNamespace(rollback=lambda: None, close=lambda: None)
        driver.connect = Mock(return_value=fake_connection)
        settings = DatabaseSettings("localhost", 3306, "reader", "secret", "travel")
        with patch("app.database.load_settings", return_value=settings), patch.dict(
            sys.modules, {"pymysql": driver}
        ):
            dependency = get_db()
            self.assertIsNotNone(dependency)
            driver.connect.assert_not_called()
            connection = next(dependency)
            self.assertIs(connection, fake_connection)
            driver.connect.assert_called_once()
            kwargs = driver.connect.call_args.kwargs
            self.assertEqual(kwargs["charset"], "utf8mb4")
            self.assertFalse(kwargs["autocommit"])
            self.assertGreater(kwargs["connect_timeout"], 0)
            self.assertGreater(kwargs["read_timeout"], 0)
            self.assertGreater(kwargs["write_timeout"], 0)
            self.assertEqual(kwargs["password"], settings.password)
            with self.assertRaises(StopIteration):
                next(dependency)

    def test_rolls_back_and_closes_even_when_rollback_fails(self) -> None:
        events: list[str] = []

        def rollback() -> None:
            events.append("rollback")
            raise RuntimeError("rollback failed")

        fake_connection = SimpleNamespace(
            rollback=rollback,
            close=lambda: events.append("close"),
        )
        driver = SimpleNamespace(cursors=SimpleNamespace(DictCursor=object()))
        driver.connect = lambda **kwargs: fake_connection
        with patch("app.database.load_settings", return_value=DatabaseSettings(
            "localhost", 3306, "reader", "secret", "travel"
        )), patch.dict(sys.modules, {"pymysql": driver}):
            dependency = get_db()
            next(dependency)
            with self.assertRaisesRegex(RuntimeError, "rollback failed"):
                next(dependency)
        self.assertEqual(events, ["rollback", "close"])

    def test_close_failure_does_not_replace_handler_exception(self) -> None:
        original_error = ValueError("handler failure")
        events: list[str] = []
        fake_connection = SimpleNamespace(
            rollback=lambda: events.append("rollback"),
            close=lambda: (_ for _ in ()).throw(RuntimeError("close failure")),
        )
        driver = SimpleNamespace(cursors=SimpleNamespace(DictCursor=object()))
        driver.connect = lambda **kwargs: fake_connection
        with patch("app.database.load_settings", return_value=DatabaseSettings(
            "localhost", 3306, "reader", "secret", "travel"
        )), patch.dict(sys.modules, {"pymysql": driver}):
            dependency = get_db()
            next(dependency)
            with self.assertRaises(ValueError) as caught:
                dependency.throw(original_error)
        self.assertIs(caught.exception, original_error)

    def test_cleanup_failure_is_visible_without_handler_exception(self) -> None:
        fake_connection = SimpleNamespace(
            rollback=lambda: None,
            close=lambda: (_ for _ in ()).throw(RuntimeError("close failure")),
        )
        driver = SimpleNamespace(cursors=SimpleNamespace(DictCursor=object()))
        driver.connect = lambda **kwargs: fake_connection
        with patch("app.database.load_settings", return_value=DatabaseSettings(
            "localhost", 3306, "reader", "secret", "travel"
        )), patch.dict(sys.modules, {"pymysql": driver}):
            dependency = get_db()
            next(dependency)
            with self.assertRaisesRegex(RuntimeError, "close failure"):
                next(dependency)


if __name__ == "__main__":
    unittest.main()

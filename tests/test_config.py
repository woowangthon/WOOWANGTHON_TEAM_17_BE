from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy.engine import make_url

from app.config import ConfigurationError, Settings, load_settings


class LoadSettingsTests(unittest.TestCase):
    def test_reads_values_and_keeps_password_out_of_repr(self) -> None:
        env = {
            "DB_HOST": "localhost",
            "DB_PORT": "3307",
            "DB_USERNAME": "reader",
            "DB_PASSWORD": "value with $ and spaces",
            "DB_NAME": "travel",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings(load_env_file=False)

        self.assertEqual(settings.port, 3307)
        self.assertEqual(settings.password, env["DB_PASSWORD"])
        self.assertNotIn(env["DB_PASSWORD"], repr(settings))

    def test_rejects_missing_values_without_echoing_them(self) -> None:
        with patch.dict(os.environ, {"DB_PASSWORD": "never-print-this"}, clear=True):
            with self.assertRaises(ConfigurationError) as caught:
                load_settings(load_env_file=False)
        self.assertIn("DB_HOST", str(caught.exception))
        self.assertNotIn("never-print-this", str(caught.exception))

    def test_rejects_invalid_port_without_echoing_value(self) -> None:
        env = {
            "DB_HOST": "localhost",
            "DB_PORT": "not-a-port-secret",
            "DB_USERNAME": "reader",
            "DB_PASSWORD": "secret",
            "DB_NAME": "travel",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigurationError) as caught:
                load_settings(load_env_file=False)
        self.assertNotIn(env["DB_PORT"], str(caught.exception))
        self.assertNotIn(env["DB_PASSWORD"], str(caught.exception))

    def test_settings_url_is_preferred_and_secret_is_hidden_from_repr(self):
        values = {
            "DATABASE_URL": "mysql+pymysql://url-user:url-pass@db.example:3306/url_db?charset=utf8mb4",
            "DB_HOST": "other.example",
            "DB_USERNAME": "other-user",
            "DB_PASSWORD": "other-pass",
            "DB_NAME": "other_db",
        }
        settings = Settings.from_env(values)
        self.assertEqual(settings.database_url, values["DATABASE_URL"])
        self.assertNotIn("url-pass", repr(settings))
        self.assertNotIn("other-pass", repr(settings))

    def test_db_parts_create_url_with_reserved_password_characters(self):
        password = "p@:/%[]?#"
        settings = Settings.from_env({
            "DB_HOST": "localhost",
            "DB_USERNAME": "user",
            "DB_PASSWORD": password,
            "DB_NAME": "sample",
        })
        parsed = make_url(settings.database_url)
        self.assertEqual(parsed.password, password)
        self.assertEqual(parsed.port, 3306)
        self.assertNotIn(password, settings.database_url)

    def test_partial_process_db_settings_fail_without_loading_dotenv(self):
        load_dotenv = Mock()
        with patch.dict(os.environ, {"DB_HOST": "process-host"}, clear=True), \
             patch.dict(sys.modules, {"dotenv": SimpleNamespace(load_dotenv=load_dotenv)}):
            with self.assertRaisesRegex(ConfigurationError, "DB_USERNAME"):
                load_settings(load_env_file=True)
        load_dotenv.assert_not_called()

    def test_complete_process_db_settings_take_precedence_over_file_url(self):
        with patch.dict(os.environ, {
            "DB_HOST": "process-host",
            "DB_USERNAME": "process-user",
            "DB_PASSWORD": "process-password",
            "DB_NAME": "process-db",
        }, clear=True):
            settings = load_settings(load_env_file=False)
        self.assertEqual(settings.host, "process-host")
        self.assertEqual(settings.name, "process-db")
        self.assertEqual(settings.password, "process-password")

    def test_file_database_url_precedes_file_db_parts(self):
        def load_file(*args, **kwargs):
            os.environ.update({
                "DATABASE_URL": "mysql+pymysql://file-user:file-pass@file.example:3306/file_db",
                "DB_HOST": "other-file.example",
                "DB_USERNAME": "other-user",
                "DB_PASSWORD": "other-pass",
                "DB_NAME": "other-db",
            })

        fake_dotenv = SimpleNamespace(load_dotenv=Mock(side_effect=load_file))
        with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {"dotenv": fake_dotenv}):
            settings = load_settings(load_env_file=True)
        self.assertEqual(settings.host, "file.example")
        self.assertEqual(settings.name, "file_db")
        self.assertNotIn("file-pass", repr(settings))

    def test_url_query_options_are_preserved_in_app_settings(self):
        url = "mysql+pymysql://user:pass@db.example:3306/travel?charset=utf8mb4&ssl=true"
        settings = Settings.from_env({"DATABASE_URL": url})
        self.assertEqual(settings.database_url, url)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import ConfigurationError, load_settings


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


if __name__ == "__main__":
    unittest.main()

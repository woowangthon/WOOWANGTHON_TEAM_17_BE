from __future__ import annotations

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import gemini, trip_preview
from app.gemini import GeminiError, GeminiSettings
from app.trip_preview import TripInputError, validate_trip_input


SAMPLE = {
    "dates": {"start": "2026-12-12", "end": "2026-12-13"},
    "transport": "자가용",
    "total_budget_won": 600000,
    "departure": {"time": "09:00", "region": "충북 청주시"},
    "headcount": 4,
    "preferences_text": "온천과 조용한 자연을 원해요. 많이 걷는 곳은 빼주세요.",
}

EXTRACTED = {
    "travel_regions": ["단양"],
    "required_conditions": ["온천"],
    "exclusions": ["많이 걷는 장소"],
    "preferences": ["조용한 자연"],
    "fixed_conflicts": [],
    "questions": [],
}


class FakeResponse:
    def __init__(self, status: int, payload: bytes):
        self.status = status
        self.payload = payload
        self.read_limit = None

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return self.payload[:limit]


class FakeConnection:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.request_args = None
        self.closed = False

    def request(self, *args, **kwargs):
        self.request_args = (args, kwargs)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def response_for(data: dict, *, finish_reason: str = "STOP") -> bytes:
    return json.dumps({
        "candidates": [{
            "finishReason": finish_reason,
            "content": {"parts": [{"text": json.dumps(data, ensure_ascii=False)}]},
        }]
    }, ensure_ascii=False).encode("utf-8")


class TripPreviewTests(unittest.TestCase):
    def test_configurable_model_default_without_loading_env_file(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-only-key"}, clear=True), \
             patch.dict(sys.modules, {"dotenv": SimpleNamespace(load_dotenv=Mock())}):
            self.assertEqual(gemini.load_gemini_settings().model, "gemini-3.5-flash-lite")
            os.environ["GEMINI_MODEL"] = "gemini-custom"
            self.assertEqual(gemini.load_gemini_settings().model, "gemini-custom")

    def test_loads_env_model_when_api_key_comes_from_process_environment(self):
        load_dotenv = Mock(side_effect=lambda *args, **kwargs: os.environ.setdefault(
            "GEMINI_MODEL", "gemini-from-env-file"
        ))
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-only-key"}, clear=True), \
             patch.dict(sys.modules, {"dotenv": SimpleNamespace(load_dotenv=load_dotenv)}):
            settings = gemini.load_gemini_settings()
        self.assertEqual(settings.api_key, "test-only-key")
        self.assertEqual(settings.model, "gemini-from-env-file")
        kwargs = load_dotenv.call_args.kwargs
        self.assertFalse(kwargs["override"])
        self.assertFalse(kwargs["interpolate"])

    def test_validates_and_preserves_fixed_context(self):
        context, note = validate_trip_input(SAMPLE)
        self.assertEqual(context["dates"], SAMPLE["dates"])
        self.assertEqual(context["transport"], SAMPLE["transport"])
        self.assertEqual(context["total_budget_won"], SAMPLE["total_budget_won"])
        self.assertEqual(context["departure"], SAMPLE["departure"])
        self.assertEqual(context["headcount"], SAMPLE["headcount"])
        self.assertEqual(note, SAMPLE["preferences_text"])

    def test_rejects_invalid_fixed_values(self):
        cases = [
            ({**SAMPLE, "headcount": True}, "headcount"),
            ({**SAMPLE, "total_budget_won": -1}, "total_budget_won"),
            ({**SAMPLE, "dates": {"start": "2026-12-14", "end": "2026-12-13"}}, "before"),
            ({**SAMPLE, "departure": {"time": "25:00", "region": "청주"}}, "valid 24-hour"),
            ({**SAMPLE, "transport": " "}, "transport"),
            ({**SAMPLE, "preferences_text": " "}, "preferences_text"),
        ]
        for data, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(TripInputError, message):
                validate_trip_input(data)

    def test_uses_header_key_model_schema_and_returns_pending_conflict(self):
        payload = EXTRACTED.copy()
        payload["fixed_conflicts"] = [{
            "field": "dates",
            "description": "메모에 다른 날짜가 있습니다",
            "needs_confirmation": True,
        }]
        response = FakeResponse(200, response_for(payload))
        connection = FakeConnection(response)
        settings = GeminiSettings("key-secret", "gemini-custom")
        with patch.object(gemini, "load_gemini_settings", return_value=settings), \
             patch.object(gemini, "HTTPSConnection", return_value=connection) as connect:
            result = trip_preview.create_trip_preview(SAMPLE)

        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(result["fixed_context"]["dates"], SAMPLE["dates"])
        self.assertTrue(result["extracted"]["fixed_conflicts"][0]["needs_confirmation"])
        connect.assert_called_once_with("generativelanguage.googleapis.com", timeout=60)
        args, kwargs = connection.request_args
        self.assertEqual(args[:2], ("POST", "/v1beta/models/gemini-custom:generateContent"))
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "key-secret")
        self.assertNotIn("key-secret", args[1])
        request = json.loads(kwargs["body"])
        self.assertEqual(request["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(request["generationConfig"]["responseJsonSchema"], gemini.OUTPUT_SCHEMA)
        self.assertIn("한국어", request["systemInstruction"]["parts"][0]["text"])
        user_data = json.loads(request["contents"][0]["parts"][0]["text"])
        self.assertEqual(user_data["fixed_trip_context"], result["fixed_context"])
        self.assertEqual(user_data["trip_notes"], SAMPLE["preferences_text"])
        self.assertTrue(connection.closed)
        self.assertEqual(response.read_limit, gemini.MAX_RESPONSE_BYTES + 1)

    def test_non_200_does_not_leak_response_or_key_and_closes(self):
        response = FakeResponse(429, b"private server response")
        connection = FakeConnection(response)
        with patch.object(gemini, "load_gemini_settings", return_value=GeminiSettings("private-key")), \
             patch.object(gemini, "HTTPSConnection", return_value=connection):
            with self.assertRaises(GeminiError) as caught:
                gemini.extract_trip_preferences({}, "notes", request_id="request-123")
        self.assertIn("rate limit", str(caught.exception))
        self.assertIn("request-123", str(caught.exception))
        self.assertNotIn("private-key", str(caught.exception))
        self.assertNotIn("private server response", str(caught.exception))
        self.assertTrue(connection.closed)

    def test_malformed_or_truncated_response_fails_without_retry(self):
        for payload in (b"not-json", response_for(EXTRACTED, finish_reason="MAX_TOKENS")):
            response = FakeResponse(200, payload)
            connection = FakeConnection(response)
            with patch.object(gemini, "load_gemini_settings", return_value=GeminiSettings("key")), \
                 patch.object(gemini, "HTTPSConnection", return_value=connection) as connect:
                with self.assertRaises(GeminiError):
                    gemini.extract_trip_preferences({}, "notes")
            connect.assert_called_once()
            self.assertTrue(connection.closed)

    def test_timeout_has_safe_message_and_single_request(self):
        connection = FakeConnection(FakeResponse(200, b""))
        connection.request = lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("private timeout"))
        with patch.object(gemini, "load_gemini_settings", return_value=GeminiSettings("key")), \
             patch.object(gemini, "HTTPSConnection", return_value=connection) as connect:
            with self.assertRaisesRegex(GeminiError, "timed out") as caught:
                gemini.extract_trip_preferences({}, "notes", request_id="request-timeout")
        self.assertIn("request-timeout", str(caught.exception))
        self.assertNotIn("private timeout", str(caught.exception))
        connect.assert_called_once()
        self.assertTrue(connection.closed)

    def test_invalid_conflict_or_extra_response_field_rejected(self):
        invalid = EXTRACTED.copy()
        invalid["fixed_conflicts"] = [{
            "field": "api_key", "description": "secret", "needs_confirmation": False,
        }]
        with self.assertRaises(GeminiError):
            gemini._extract_json(response_for(invalid))
        invalid = {**EXTRACTED, "invented": "value"}
        with self.assertRaises(GeminiError):
            gemini._extract_json(response_for(invalid))


if __name__ == "__main__":
    unittest.main()

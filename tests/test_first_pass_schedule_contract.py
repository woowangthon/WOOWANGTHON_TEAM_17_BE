"""The actual installed AI parser and BE validator agree on the public DTO."""
import json

from app.preparation import _default_interpreter, _validate_interpret_result, validate_draft
from chungbuk_trip_ai import gemini


class ApiError(Exception):
    def __init__(self, status, code, *args, **kwargs):
        self.status, self.code = status, code
        super().__init__(code)


def test_first_analysis_booking_round_trips_public_parser_and_preparation(monkeypatch):
    original = "우리 청주호텔을 2026년 11월 12일 오후 3시에 예약했어요. 체크아웃은 2026년 11월 14일 오전 11시예요."
    result = {"suggestions": {"likes": "자연", "avoid": None, "avoidImportance": "required", "food": None,
                              "foodImportance": "preferred", "walking": None,
                              "scheduleItems": [{"kind": "lodging", "scope": "group", "placeQuery": "청주호텔",
                                                 "date": "2026-11-12", "time": "15:00", "endDate": "2026-11-14",
                                                 "endTime": "11:00", "evidence": original}]}, "needsWalkingAnswer": True}
    body = json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(result)}]}}]}).encode()
    class Connection:
        def __init__(self, *args, **kwargs): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return self
        status = 200
        def read(self, limit): return body
        def close(self): pass
    monkeypatch.setattr(gemini, "HTTPSConnection", Connection)
    monkeypatch.setattr(gemini, "load_gemini_settings", lambda: gemini.GeminiSettings("fake-test-key"))
    parsed = _default_interpreter(original, {"startDate": "2026-11-12", "endDate": "2026-11-14"})
    public = _validate_interpret_result(parsed, ApiError)
    draft = validate_draft({"originalText": original, **public["suggestions"]})
    item = draft["scheduleItems"][0]
    assert item["kind"] == "lodging" and item["scope"] == "group"
    assert (item["date"], item["time"], item["endDate"], item["endTime"]) == ("2026-11-12", "15:00", "2026-11-14", "11:00")
    assert item["id"] and item["contentid"] is None
    assert "evidence" not in item and "originalText" not in public["suggestions"]

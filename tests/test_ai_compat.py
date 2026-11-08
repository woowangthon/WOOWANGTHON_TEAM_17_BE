import pytest

ai_gemini = pytest.importorskip("chungbuk_trip_ai.gemini")
ai_preview = pytest.importorskip("chungbuk_trip_ai.trip_preview")

from app import gemini, trip_preview


def test_backend_ai_imports_remain_compatibility_exports():
    assert gemini.extract_trip_preferences is ai_gemini.extract_trip_preferences
    assert gemini.load_gemini_settings is ai_gemini.load_gemini_settings
    assert trip_preview.create_trip_preview is ai_preview.create_trip_preview
    assert trip_preview.validate_trip_input is ai_preview.validate_trip_input
    assert trip_preview.main is ai_preview.main

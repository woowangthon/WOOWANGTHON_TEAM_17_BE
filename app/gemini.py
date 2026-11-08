"""Compatibility exports for the separately maintained Chungbuk trip AI package."""

from chungbuk_trip_ai.gemini import (
    DEFAULT_MODEL,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    OUTPUT_SCHEMA,
    GeminiError,
    GeminiSettings,
    extract_trip_preferences,
    load_gemini_settings,
)

__all__ = [
    "DEFAULT_MODEL",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "OUTPUT_SCHEMA",
    "GeminiError",
    "GeminiSettings",
    "extract_trip_preferences",
    "load_gemini_settings",
]

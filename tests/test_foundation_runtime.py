"""Provider deadlines and private errors without DB or external calls."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.generation_foundation import (
    ConfirmedPreparationSnapshot, GenerationFoundationError, normalize_snapshot,
)


def test_deadline_never_revalidates_or_adopts_a_late_provider_result():
    release = threading.Event()
    started = threading.Event()
    revalidated = []
    snapshot = ConfirmedPreparationSnapshot('{"confirmedPreferences":[]}', "source")
    catalog = SimpleNamespace(supported_tags=(), digest="catalog")

    def provider(*_):
        started.set()
        release.wait(2)
        return {"late": True}

    async def exercise():
        try:
            with pytest.raises(GenerationFoundationError, match="AI_TIMEOUT"):
                await normalize_snapshot(snapshot, catalog, lambda: revalidated.append(True),
                                         provider, timeout_seconds=0.05)
            assert started.is_set()
            assert revalidated == []
        finally:
            release.set()

    asyncio.run(exercise())
    assert revalidated == []


def test_provider_error_is_safe_and_output_is_detached():
    snapshot = ConfirmedPreparationSnapshot('{"confirmedPreferences":[]}', "source")
    catalog = SimpleNamespace(supported_tags=(), digest="catalog")

    def failure(*_):
        raise RuntimeError("sensitive provider diagnostic")

    with pytest.raises(GenerationFoundationError, match="AI_NORMALIZATION_FAILED") as error:
        asyncio.run(normalize_snapshot(snapshot, catalog, lambda: snapshot, failure))
    assert "sensitive" not in str(error.value)
    provider_result = {"terms": [{"tag": "verified"}]}
    result = asyncio.run(normalize_snapshot(snapshot, catalog, lambda: snapshot, lambda *_: provider_result))
    provider_result["terms"][0]["tag"] = "changed after completion"
    assert result["normalization"]["terms"][0]["tag"] == "verified"

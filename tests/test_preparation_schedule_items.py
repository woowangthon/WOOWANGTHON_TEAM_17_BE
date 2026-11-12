import uuid

import pytest

from app.preparation import PrepFailure, _member_safe_suggestions, validate_conditions, validate_draft


def conditions(**extra):
    return {"region": "청주시", "startDate": "2026-12-12", "endDate": "2026-12-13",
            "expectedPeople": 1, "budget": 100000, "transport": "car", "departure": None,
            "returnRoute": None, "lodging": None, "fixedSchedule": None, **extra}


def draft(items=None, **extra):
    return {"originalText": "원문", "likes": "자연", "avoid": None,
            "avoidImportance": "required", "food": None, "foodImportance": "preferred",
            "walking": "40", "scheduleItems": items or [], **extra}


def item(**overrides):
    return {"id": str(uuid.uuid4()), "kind": "appointment", "scope": "personal",
            "placeQuery": "청주 박물관", "contentid": None, "date": "2026-12-12",
            "time": "11:30", "endDate": None, "endTime": None, **overrides}


def test_conditions_keep_old_payload_defaults_and_explicit_null_departure_time():
    old = validate_conditions(conditions())
    assert old["departureTime"] == "09:00" and old["departureContentid"] is None
    explicit = validate_conditions(conditions(departureTime=None, departureContentid=123))
    assert explicit["departureTime"] is None and explicit["departureContentid"] == 123


@pytest.mark.parametrize("payload", [conditions(departureTime="25:00"), conditions(departureTime=900),
                                      conditions(departureContentid=True), conditions(extraField="no")])
def test_conditions_reject_invalid_additive_fields(payload):
    with pytest.raises(PrepFailure):
        validate_conditions(payload)


def test_appointment_can_remain_pending_correction_and_accepts_explicit_group_scope():
    pending = item(scope=None, placeQuery="", date=None, time=None)
    clean = validate_draft(draft([pending]))
    assert clean["scheduleItems"][0]["scope"] is None
    assert clean["scheduleItems"][0]["time"] is None
    group = validate_draft(draft([item(scope="group")]))
    assert group["scheduleItems"][0]["scope"] == "group"


def test_lodging_keeps_explicit_checkin_checkout_and_rejects_reverse_range():
    lodging = item(kind="lodging", date="2026-12-12", time="15:00", endDate="2026-12-13", endTime="10:00")
    clean = validate_draft(draft([lodging]))
    assert clean["scheduleItems"][0]["endDate"] == "2026-12-13"
    with pytest.raises(PrepFailure):
        validate_draft(draft([item(kind="lodging", date="2026-12-13", endDate="2026-12-12")]))


def test_known_checkout_survives_unknown_checkin_and_start_day():
    partial = item(kind="lodging", date=None, time=None, endDate="2026-12-13", endTime="10:00")
    clean = validate_draft(draft([partial]))["scheduleItems"][0]
    assert clean["time"] is None and clean["endTime"] == "10:00"
    assert clean["date"] is None and clean["endDate"] == "2026-12-13"


def test_lodging_unknown_scope_requires_explicit_same_form_correction():
    from sqlalchemy import create_engine, text
    from app.preparation import _schedule_clarifications
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE places(contentid INTEGER,title TEXT,addr1 TEXT)"))
            conn.execute(text("INSERT INTO places VALUES(1,'숙소','주소')"))
            resolved, issues = _schedule_clarifications(conn, [item(kind="lodging", scope=None, contentid=1, placeQuery="숙소")])
            assert resolved[0]["contentid"] == 1
            assert issues[0]["fields"] == ["scope"]
    finally:
        engine.dispose()


def test_schedule_item_caps_unknownkeys_and_uuid_types():
    with pytest.raises(PrepFailure):
        validate_draft(draft([item(extra="bad")]))
    with pytest.raises(PrepFailure):
        validate_draft(draft([item(id="not-a-uuid")]))
    with pytest.raises(PrepFailure):
        validate_draft(draft([item() for _ in range(21)]))


def test_interpreter_cannot_promote_member_suggestion_to_group_scope():
    suggestions = {"scheduleItems": [item(scope="group")], "likes": None}
    assert _member_safe_suggestions(suggestions, "MEMBER")["scheduleItems"][0]["scope"] is None
    assert _member_safe_suggestions(suggestions, "OWNER")["scheduleItems"][0]["scope"] == "group"

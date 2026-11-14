"""Full HTTP generation flow on the explicitly isolated MySQL with AI/route stubs."""
import copy
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from preparation_helpers import (base, confirm, create_room, headers, member_client,
                                 save_conditions, save_personal)
from test_routing_validation import COORD, Router

pytestmark = pytest.mark.mysql


def normalize(preferences, tags):
    assert "자연" in tags, "normalizer must receive observed catalog vocabulary"
    terms, unmapped, walking = [], [], []
    for pref in preferences:
        assert set(pref) == {"memberRef", "likes", "avoid", "avoidImportance", "food", "foodImportance", "walking"}
        ref = pref["memberRef"]
        for field in ("likes", "avoid", "food"):
            if not pref.get(field):
                continue
            terms.append({"memberRef": ref, "field": field, "tag": "자연",
                          "evidence": pref[field], "polarity": "avoid" if field == "avoid" else "prefer",
                          "importance": "preferred" if field == "likes" else pref[field + "Importance"],
                          "requiresReview": True})
            unmapped.append({"memberRef": ref, "field": field, "text": pref[field],
                             "importance": terms[-1]["importance"], "reason": "coverage_unverified"})
        walking.append({"memberRef": ref, "scope": "between_places", "maxMinutes": 20, "unlimited": False})
    return {"terms": terms, "unmapped": unmapped, "walking": walking,
            "conflictCandidates": [], "needsReview": bool(terms or unmapped)}


def draft(dates, candidates, fixed_visits, max_visits, schedule_context=None):
    return {"days": [{"date": day, "contentids": [11]} for day in dates]}


def seed_catalog(engine):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO places(contentid,title,planningRole,recommendationUse,adminCity,mapx,mapy,geoValidated) "
                          "VALUES(11,'합성 자연 공원','sightseeing_stop','MAIN_DESTINATION','청주시',127.4341,36.6286,'yes')"))
        conn.execute(text("INSERT INTO place_details(contentid,estimatedStayMinutes,feeText,openingHoursText,closedDaysText,planningStatusV2) "
                          "VALUES(11,60,'무료','09:00~18:00','연중무휴','candidate_needs_live_validation')"))
        conn.execute(text("INSERT INTO place_preferences(contentid,preferenceTags) VALUES(11,'자연')"))


def prepare(client, room, cv=1, pv=0, text="합성 본인 원문"):
    response = save_personal(client, room, cv=cv, pv=pv, likes="자연", avoid=None, food=None, originalText=text)
    assert response.status_code == 200, response.text
    pv = response.json()["preferenceVersion"]
    response = confirm(client, room, cv=cv, pv=pv)
    assert response.status_code == 200, response.text
    return pv


def review(client, room, cv=1, pv=1):
    key = str(uuid.uuid4())
    payload = {"expectedConditionsVersion": cv, "expectedPreferenceVersion": pv}
    response = client.post(base(room) + "/generation/preferences/me", headers=headers(key=key), json=payload)
    assert response.status_code == 200, response.text
    dto = response.json()
    replay = client.post(base(room) + "/generation/preferences/me", headers=headers(key=key), json=payload)
    assert replay.status_code == 200 and replay.json() == dto
    terms = dto["normalization"]["terms"]
    response = client.put(base(room) + "/generation/preferences/me/review", headers=headers(), json={
        "normalizationId": dto["normalization"]["id"], "expectedReviewVersion": dto["reviewVersion"],
        "acceptedTermIds": [t["id"] for t in terms],
        "reviewedFields": [{"field": "likes", "termIds": [t["id"] for t in terms], "scope": "all", "fullyReviewed": True}]})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def ready(app, client):
    app.state.generation_normalizer = normalize
    app.state.generation_drafter = draft
    app.state.generation_router = Router(seconds=60)
    seed_catalog(app.state.engine)
    room = create_room(client)
    response = save_conditions(client, room, region="청주시", startDate="2026-12-12", endDate="2026-12-12",
                               transport="walk", lodging=None, fixedSchedule=None)
    assert response.status_code == 200
    prepare(client, room)
    review(client, room)
    response = client.put(base(room) + "/generation/settings", headers=headers(), json={
        "expectedConditionsVersion": 1, "expectedSettingsVersion": 0,
        "settings": {"activityWindow": {"start": "09:00", "end": "18:00"},
                     "departureAnchor": {"label": "합성 출발", "coordinate": COORD},
                     "returnAnchor": {"label": "합성 복귀", "coordinate": COORD}, "reviewedCommonFields": []}})
    assert response.status_code == 200, response.text
    return room


def generate(client, room):
    state = client.get(base(room) + "/generation")
    assert state.status_code == 200, state.text
    state = state.json()
    assert state["readyForGeneration"] is True, state
    key = str(uuid.uuid4())
    payload = {"expectedInputToken": state["inputToken"], "expectedSettingsVersion": state["settingsVersion"]}
    created = client.post(base(room) + "/generation/jobs", headers=headers(key=key), json=payload)
    assert created.status_code == 202, created.text
    job = client.get(base(room) + "/generation/jobs/" + created.json()["id"])
    assert job.status_code == 200, job.text
    return job.json(), key, payload


def test_unconfirmed_member_draft_does_not_change_generation_input_token(app, client, ready):
    initial = client.get(base(ready) + "/generation").json()
    with member_client(app, ready) as (member, _):
        assert member.get(base(ready) + "/generation").json()["inputToken"] == initial["inputToken"]
        first = save_personal(member, ready, cv=1, pv=0, likes="쇼핑", walking="20")
        assert first.status_code == 200, first.text
        after_first = member.get(base(ready) + "/generation").json()
        assert after_first["inputToken"] == initial["inputToken"]
        second = save_personal(member, ready, cv=1, pv=1, likes="문화", walking="40")
        assert second.status_code == 200, second.text
        after_second = member.get(base(ready) + "/generation").json()
        assert after_second["inputToken"] == initial["inputToken"]


def test_complete_generation_save_refresh_and_idempotency(app, client, ready):
    calls = []
    rate_checks = []
    app.state.generation_service._rate = lambda *args: rate_checks.append(args)
    def provider(*args):
        calls.append(True)
        return draft(*args)
    app.state.generation_drafter = provider
    job, key, body = generate(client, ready)
    assert job["status"] == "SUCCEEDED", job
    state = client.get(base(ready) + "/generation").json()
    assert state["latestPlan"]["current"] is True
    coalesced = client.post(base(ready) + "/generation/jobs", headers=headers(key=str(uuid.uuid4())), json=body)
    assert coalesced.status_code == 200, coalesced.text
    assert coalesced.json()["id"] == job["id"]
    assert len(rate_checks) == 1
    replay = client.post(base(ready) + "/generation/jobs", headers=headers(key=key), json=body)
    assert replay.status_code == 202 and replay.json()["id"] == job["id"]
    assert len(calls) == 1
    with app.state.engine.connect() as conn:
        assert conn.scalar(text("SELECT COUNT(*) FROM generation_jobs WHERE kind='GENERATE'")) == 1
    with app.state.engine.begin() as conn:
        conn.execute(text("UPDATE generation_jobs SET created_at=DATE_SUB(UTC_TIMESTAMP(3), INTERVAL 2 SECOND), expires_at=DATE_SUB(UTC_TIMESTAMP(3), INTERVAL 1 SECOND) WHERE id=:id"), {"id": job["id"]})
    expired_replay = client.post(base(ready) + "/generation/jobs", headers=headers(key=key), json=body)
    assert expired_replay.status_code == 202 and expired_replay.json()["id"] == job["id"]
    assert client.get(base(ready) + "/generation").json()["latestPlan"]["current"] is True

    response = client.get(base(ready) + "/plans/" + job["planId"])
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["status"] == "DRAFT" and plan["stale"] is False and plan["canSave"] is True
    assert plan["document"]["validation"]["status"] == "unknown"
    assert "originalText" not in str(plan) and "sourceStamp" not in plan
    save_body = {"versionId": plan["versionId"], "expectedLockVersion": plan["lockVersion"], "acknowledgeUnverified": False}
    save_url = base(ready) + "/plans/" + plan["planId"] + "/save"
    rejected = client.post(save_url, headers=headers(), json=save_body)
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "UNVERIFIED_ACK_REQUIRED"
    save_body["acknowledgeUnverified"] = True
    save_key = str(uuid.uuid4())
    saved = client.post(save_url, headers=headers(key=save_key), json=save_body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "SAVED" and saved.json()["versionNo"] == 1
    assert client.post(save_url, headers=headers(key=save_key), json=save_body).json() == saved.json()
    assert client.get(base(ready) + "/plans/" + plan["planId"]).json()["status"] == "SAVED"
    with app.state.engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM plans").scalar() == 1
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM plan_versions").scalar() == 1


def test_changed_generation_inputs_create_new_job_and_mark_old_plan_not_current(app, client, ready):
    job, _, body = generate(client, ready)
    assert job["status"] == "SUCCEEDED", job
    settings = client.get(base(ready) + "/generation").json()["settings"]
    settings["activityWindow"]["end"] = "17:00"
    changed = client.put(base(ready) + "/generation/settings", headers=headers(), json={
        "expectedConditionsVersion": 1, "expectedSettingsVersion": 1, "settings": settings})
    assert changed.status_code == 200, changed.text
    changed_state = client.get(base(ready) + "/generation").json()
    assert changed_state["inputToken"] != body["expectedInputToken"]
    assert changed_state["latestPlan"]["current"] is False
    new_job = client.post(base(ready) + "/generation/jobs", headers=headers(key=str(uuid.uuid4())), json={
        "expectedInputToken": changed_state["inputToken"], "expectedSettingsVersion": changed_state["settingsVersion"]})
    assert new_job.status_code == 202 and new_job.json()["id"] != job["id"]
    assert client.get(base(ready) + "/generation/jobs/" + new_job.json()["id"]).json()["status"] == "SUCCEEDED"
    with app.state.engine.connect() as conn:
        assert conn.scalar(text("SELECT COUNT(*) FROM generation_jobs WHERE kind='GENERATE'")) == 2


def test_personal_edits_during_generation_leave_no_plan(app, client, ready):
    def changed(*args):
        response = save_personal(client, ready, cv=1, pv=1, likes="자연", avoid=None, food=None)
        assert response.status_code == 200
        return draft(*args)
    app.state.generation_drafter = changed
    job, _, _ = generate(client, ready)
    assert job["status"] == "STALE" and job["errorCode"] == "STALE_GENERATION_INPUT", job
    with app.state.engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM plans").scalar() == 0


def test_plan_source_change_blocks_save_and_cross_trip_read(app, client, ready):
    job, _, _ = generate(client, ready)
    assert job["status"] == "SUCCEEDED", job
    url = base(ready) + "/plans/" + job["planId"]
    before = client.get(url).json()
    response = save_personal(client, ready, cv=1, pv=1, likes="자연", avoid=None, food=None)
    assert response.status_code == 200
    assert client.get(url).json()["stale"] is True
    saved = client.post(url + "/save", headers=headers(), json={"versionId": before["versionId"],
                        "expectedLockVersion": before["lockVersion"], "acknowledgeUnverified": True})
    assert saved.status_code == 409 and saved.json()["error"]["code"] == "STALE_GENERATION_INPUT"
    other = create_room(client)
    assert client.get(base(other) + "/plans/" + job["planId"]).status_code == 404


def test_owner_never_gets_another_members_private_normalization(app, client, ready):
    with member_client(app, ready) as (other, member):
        assert save_conditions(client, ready, version=1, region="청주시", startDate="2026-12-12", endDate="2026-12-12",
                               expectedPeople=2, transport="walk", lodging=None, fixedSchedule=None).status_code == 200
        prepare(client, ready, cv=2, pv=1, text="OWNER_PRIVATE")
        prepare(other, ready, cv=2, text="MEMBER_PRIVATE")
        owner_norm = review(client, ready, cv=2, pv=2)
        other_norm = review(other, ready, cv=2)
        assert owner_norm["normalization"]["id"] != other_norm["normalization"]["id"]
        # No route accepts a member selector; arbitrary fields must not change identity.
        own = client.get(base(ready) + "/generation/preferences/me?memberId=" + member["id"]).json()
        assert own["normalization"]["id"] == owner_norm["normalization"]["id"]
        state = client.get(base(ready) + "/generation").json()
        assert "MEMBER_PRIVATE" not in str(state) and "normalization" not in str(state)
        assert all(m["status"] == "reviewed" for m in state["reviewMembers"])
        assert other.post(base(ready) + "/generation/jobs", headers=headers(), json={
            "expectedInputToken": state["inputToken"], "expectedSettingsVersion": state["settingsVersion"]}).status_code == 403
        job, _, _ = generate(client, ready)
        assert job["status"] == "SUCCEEDED", job
        shared = other.get(base(ready) + "/plans/" + job["planId"])
        assert shared.status_code == 200 and shared.json()["canSave"] is False
        assert "MEMBER_PRIVATE" not in shared.text and "OWNER_PRIVATE" not in shared.text


def test_generation_errors_use_safe_envelope_and_writes_require_origin(app, client):
    room = create_room(client)
    response = client.get(base(room) + "/generation/preferences/me")
    assert response.status_code == 409 and response.json()["error"]["code"] == "PREFERENCES_UNCONFIRMED"
    other_id = str(uuid.uuid4())
    response = client.get(f"/api/v1/trips/{other_id}/generation")
    assert response.status_code == 404 and "error" in response.json()
    response = client.post(base(room) + "/generation/jobs", json={})
    assert response.status_code == 403


def test_normalization_catalog_change_or_expired_lease_does_not_replace_review(app, client, ready):
    current = client.get(base(ready) + "/generation/preferences/me").json()
    def changed(preferences, tags):
        with app.state.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE place_preferences SET preferenceTags='자연|합성추가'")
        return normalize(preferences, tags)
    app.state.generation_normalizer = changed
    response = client.post(base(ready) + "/generation/preferences/me", headers=headers(), json={
        "expectedConditionsVersion": 1, "expectedPreferenceVersion": 1})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] in {"STALE_GENERATION_INPUT", "STALE_PLACE_CATALOG"}
    with app.state.engine.connect() as conn:
        stored = conn.exec_driver_sql("SELECT normalization_json FROM generation_member_reviews").scalar_one()
        assert current["normalization"]["id"] in str(stored)


def test_bad_queries_reviews_and_settings_return_contract_errors(client, ready):
    response = client.get(base(ready) + "/generation/places?limit=abc")
    assert response.status_code == 400 and "error" in response.json()
    current = client.get(base(ready) + "/generation/preferences/me").json()
    response = client.put(base(ready) + "/generation/preferences/me/review", headers=headers(), json={
        "normalizationId": current["normalization"]["id"], "expectedReviewVersion": current["reviewVersion"],
        "acceptedTermIds": [[]], "reviewedFields": []})
    assert response.status_code == 400 and "error" in response.json()
    state = client.get(base(ready) + "/generation").json()
    config = copy.deepcopy(state["settings"])
    config["activityWindow"]["start"] = "０９:００"
    response = client.put(base(ready) + "/generation/settings", headers=headers(), json={
        "expectedConditionsVersion": 1, "expectedSettingsVersion": state["settingsVersion"], "settings": config})
    assert response.status_code == 400


def test_concurrent_retry_claims_generation_once(app, client, ready):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def blocking(*args):
        calls.append(True)
        entered.set()
        assert release.wait(10)
        return draft(*args)
    app.state.generation_drafter = blocking
    state = client.get(base(ready) + "/generation").json()
    payload = {"expectedInputToken": state["inputToken"], "expectedSettingsVersion": state["settingsVersion"]}
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, base(ready) + "/generation/jobs", headers=headers(key=key), json=payload)
        try:
            assert entered.wait(10)
            retry = client.post(base(ready) + "/generation/jobs", headers=headers(key=key), json=payload)
            assert retry.status_code == 202 and retry.json()["status"] == "RUNNING", retry.text
            assert len(calls) == 1
        finally:
            release.set()
        response = first.result(timeout=15)
        assert response.status_code == 202
    job = client.get(base(ready) + "/generation/jobs/" + response.json()["id"]).json()
    assert job["status"] == "SUCCEEDED", job
    with app.state.engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM plans").scalar() == 1


def test_concurrent_different_keys_coalesce_to_one_generation_job(app, client, ready):
    state = client.get(base(ready) + "/generation").json()
    payload = {"expectedInputToken": state["inputToken"], "expectedSettingsVersion": state["settingsVersion"]}

    def post_with_key(key):
        return client.post(base(ready) + "/generation/jobs", headers=headers(key=key), json=payload)

    keys = [str(uuid.uuid4()), str(uuid.uuid4())]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(post_with_key, keys))
    assert all(response.status_code in {200, 202} for response in responses), [response.text for response in responses]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    with app.state.engine.connect() as conn:
        assert conn.scalar(text("SELECT COUNT(*) FROM generation_jobs WHERE kind='GENERATE'")) == 1


def test_failure_after_plan_insert_rolls_back_plan_and_children(app, client, ready, monkeypatch):
    from app import plan_store
    create = plan_store.create_generated_plan
    def fail_after_insert(*args, **kwargs):
        create(*args, **kwargs)
        raise RuntimeError("synthetic rollback probe")
    monkeypatch.setattr(plan_store, "create_generated_plan", fail_after_insert)
    job, _, _ = generate(client, ready)
    assert job["status"] == "FAILED" and job["planId"] is None, job
    with app.state.engine.connect() as conn:
        for table in ("plans", "plan_versions", "group_snapshots", "itinerary_nodes", "itinerary_edges"):
            assert conn.exec_driver_sql(f"SELECT COUNT(*) FROM `{table}`").scalar() == 0


def test_settings_change_during_provider_invalidates_final_plan(app, client, ready):
    def changed(*args):
        state = client.get(base(ready) + "/generation").json()
        settings = copy.deepcopy(state["settings"])
        settings["activityWindow"]["end"] = "17:00"
        saved = client.put(base(ready) + "/generation/settings", headers=headers(), json={
            "expectedConditionsVersion": 1, "expectedSettingsVersion": state["settingsVersion"], "settings": settings})
        assert saved.status_code == 200, saved.text
        return draft(*args)
    app.state.generation_drafter = changed
    job, _, _ = generate(client, ready)
    assert job["status"] == "STALE", job
    with app.state.engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM plans").scalar() == 0


def test_self_normalization_does_not_require_whole_group_or_complete_dates(app, client):
    app.state.generation_normalizer = normalize
    seed_catalog(app.state.engine)
    room = create_room(client)
    prepare(client, room, cv=0)
    with member_client(app, room):
        review(client, room, cv=0)
        state = client.get(base(room) + "/generation").json()
        assert state["readyForGeneration"] is False


def test_catalog_facts_remain_locked_until_completion_transaction_commits(app, ready):
    from app.place_catalog import load_place_catalog
    from sqlalchemy.exc import OperationalError
    with app.state.engine.begin() as completion:
        load_place_catalog(completion, lock=True)
        with app.state.engine.connect() as competing:
            competing.exec_driver_sql("SET SESSION innodb_lock_wait_timeout=1")
            with pytest.raises(OperationalError) as exc:
                competing.exec_driver_sql("UPDATE places SET title='합성 수정' WHERE contentid=11")
            assert exc.value.orig.args[0] == 1205
            competing.rollback()


def test_whole_job_timeout_fences_late_provider_without_plan(app, client, ready, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    service = app.state.generation_service
    original = service._run_job_sync
    def observed(*args):
        try:
            return original(*args)
        finally:
            finished.set()
    monkeypatch.setattr(service, "_run_job_sync", observed)
    def blocked(*args):
        entered.set()
        assert release.wait(4)
        return draft(*args)
    app.state.generation_drafter = blocked
    app.state.generation_job_timeout = 0.5
    try:
        job, _, _ = generate(client, ready)
        assert entered.is_set()
        assert not release.is_set()  # Response is bounded even while the provider remains blocked.
        assert job["status"] == "FAILED" and job["errorCode"] == "AI_TIMEOUT", job
    finally:
        release.set()
    assert finished.wait(4)
    with app.state.engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM plans").scalar() == 0

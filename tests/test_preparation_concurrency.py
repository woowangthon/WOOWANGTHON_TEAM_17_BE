from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy import event

from preparation_helpers import base, confirm, create_room, save_conditions, save_personal

pytestmark = pytest.mark.mysql


def test_simultaneous_conditions_saves_allow_only_one_current_version(client):
    room = create_room(client)
    gate = threading.Barrier(2)

    def save(region):
        gate.wait(timeout=5)
        return save_conditions(client, room, region=region)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(save, ["지역 A", "지역 B"]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    actual = client.get(base(room) + "/preparation").json()
    winner = next(r.json()["conditions"] for r in responses if r.status_code == 200)
    assert actual["conditionsVersion"] == 1
    assert actual["conditions"] == winner


def test_simultaneous_personal_save_allows_only_one_version(client):
    room = create_room(client)
    gate = threading.Barrier(2)

    def save(likes):
        gate.wait(timeout=5)
        return save_personal(client, room, likes=likes)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(save, ["자연", "문화"]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    actual = client.get(base(room) + "/preferences/me").json()
    assert actual["preferenceVersion"] == 1
    assert actual["draft"]["likes"] in {"자연", "문화"}


def test_conditions_change_racing_confirmation_never_leaves_old_confirmation_valid(client):
    room = create_room(client)
    assert save_personal(client, room).status_code == 200
    gate = threading.Barrier(2)

    def save():
        gate.wait(timeout=5)
        return save_conditions(client, room)

    def accept():
        gate.wait(timeout=5)
        return confirm(client, room)

    with ThreadPoolExecutor(max_workers=2) as executor:
        saves = executor.submit(save)
        confirms = executor.submit(accept)
        assert saves.result().status_code == 200
        assert confirms.result().status_code in {200, 409}
    actual = client.get(base(room) + "/preferences/me").json()
    assert actual["conditionsVersion"] == 1
    assert actual["confirmation"]["status"] == "writing"
    assert actual["confirmation"]["confirmedAt"] is None


def test_failure_between_conditions_write_and_invalidation_rolls_back_everything(client, app):
    room = create_room(client)
    assert save_conditions(client, room).status_code == 200
    assert save_personal(client, room, cv=1).status_code == 200
    assert confirm(client, room, cv=1).status_code == 200
    before = client.get(base(room) + "/preferences/me").json()
    seen = []

    def fail_invalidation(conn, cursor, statement, parameters, context, executemany):
        if "member_preparation_documents" in statement and statement.lstrip().upper().startswith("UPDATE"):
            seen.append(True)
            raise RuntimeError("synthetic transaction failure")

    event.listen(app.state.engine, "before_cursor_execute", fail_invalidation)
    try:
        response = save_conditions(client, room, 1, region="must roll back")
    finally:
        event.remove(app.state.engine, "before_cursor_execute", fail_invalidation)
    assert seen
    assert response.status_code == 500
    assert client.get(base(room) + "/preferences/me").json() == before
    prep = client.get(base(room) + "/preparation").json()
    assert prep["conditionsVersion"] == 1
    assert prep["conditions"]["region"] != "must roll back"

import asyncio
import re
import time

import aiohttp
from livekit import api

from app import sessions
from app.sessions import SESSION_ID, SessionService, derive_status

from .conftest import API_KEY, API_SECRET, sign_webhook


def participant(agent: bool) -> api.ParticipantInfo:
    kind = api.ParticipantInfo.Kind.AGENT if agent else api.ParticipantInfo.Kind.STANDARD
    return api.ParticipantInfo(kind=kind)


def start(client, **body) -> dict:
    res = client.post("/sessions", json=body)
    assert res.status_code == 201, res.text
    return res.json()


# -- status derivation ---------------------------------------------------


def test_status_pending_until_the_agent_joins():
    assert derive_status(True, []) == "pending"
    assert derive_status(True, [participant(agent=False)]) == "pending"


def test_status_ready_then_active():
    assert derive_status(True, [participant(agent=True)]) == "ready"
    assert derive_status(True, [participant(agent=True), participant(agent=False)]) == "active"


def test_status_closing_once_the_agent_has_come_and_gone():
    assert derive_status(True, [], job_assigned=True) == "closing"
    # a user who is still there is waiting for an agent, not closing
    assert derive_status(True, [participant(agent=False)], job_assigned=True) == "pending"
    assert derive_status(True, [participant(agent=True)], job_assigned=True) == "ready"


def test_status_without_an_agent_depends_on_users_alone():
    assert derive_status(False, []) == "ready"
    assert derive_status(False, [participant(agent=False)]) == "active"


# -- start ---------------------------------------------------------------


def test_start_creates_room_and_dispatch_and_returns_token(client, lk, settings):
    session = start(client, identity="user1", metadata={"lang": "en"})

    assert SESSION_ID.match(session["id"])
    assert session["status"] == "pending"
    assert session["agent"] == "echo"  # first allowed name is the default
    assert session["metadata"] == {"lang": "en"}
    assert session["url"] == settings.livekit_public_url
    assert [d["agent"] for d in session["dispatches"]] == ["echo"]

    room = lk.rooms[session["id"]]
    assert room.empty_timeout == settings.empty_timeout_seconds
    assert room.departure_timeout == settings.departure_timeout_seconds

    claims = api.TokenVerifier(API_KEY, API_SECRET).verify(session["token"])
    assert claims.identity == "user1"
    assert claims.video.room_join and claims.video.room == session["id"]


def test_start_with_no_body_uses_defaults(client):
    res = client.post("/sessions")
    assert res.status_code == 201
    assert res.json()["agent"] == "echo"
    assert re.match(r"^user-[0-9a-f]{6}$", res.json()["identity"])


def test_start_with_null_agent_creates_room_without_dispatch(client):
    session = start(client, agent=None)
    assert session["agent"] is None
    assert session["dispatches"] == []
    assert session["status"] == "ready"


def test_start_rejects_unknown_agent(client, lk):
    res = client.post("/sessions", json={"agent": "nosuch"})
    assert res.status_code == 422
    assert lk.rooms == {}


# -- status --------------------------------------------------------------


def test_get_follows_the_session_through_its_states(client, lk):
    sid = start(client)["id"]
    assert client.get(f"/sessions/{sid}").json()["status"] == "pending"

    lk.run_job(sid)
    session = client.get(f"/sessions/{sid}").json()
    assert session["status"] == "ready"
    assert session["participants"][0]["kind"] == "agent"
    assert session["dispatches"][0]["jobs"][0]["status"] == "running"
    assert session["dispatches"][0]["jobs"][0]["worker_id"] == "AW_1"

    lk.join(sid, "user1")
    assert client.get(f"/sessions/{sid}").json()["status"] == "active"

    lk.leave_all(sid)
    assert client.get(f"/sessions/{sid}").json()["status"] == "closing"


def test_get_unknown_or_malformed_id_is_404(client, lk):
    assert client.get("/sessions/s-0000000000").status_code == 404
    # a room that exists but wasn't created as a session is not a session
    lk.rooms["spike"] = api.Room(name="spike")
    assert client.get("/sessions/spike").status_code == 404


def test_list_shows_only_session_rooms(client, lk):
    first, second = start(client)["id"], start(client)["id"]
    lk.rooms["spike"] = api.Room(name="spike")
    ids = [s["id"] for s in client.get("/sessions").json()["sessions"]]
    assert sorted(ids) == sorted([first, second])


# -- stop and history ----------------------------------------------------


def test_stop_deletes_the_room_and_is_idempotent(client, lk):
    sid = start(client)["id"]
    assert client.delete(f"/sessions/{sid}").status_code == 204
    assert sid not in lk.rooms

    ended = client.get(f"/sessions/{sid}").json()
    assert ended["status"] == "ended"
    assert ended["end_reason"] == "stopped"
    assert ended["agent"] == "echo"

    assert client.delete(f"/sessions/{sid}").status_code == 204
    assert client.delete("/sessions/s-0000000000").status_code == 404


def test_list_includes_ended_only_when_asked(client):
    live, stopped = start(client)["id"], start(client)["id"]
    client.delete(f"/sessions/{stopped}")

    assert [s["id"] for s in client.get("/sessions").json()["sessions"]] == [live]
    listed = client.get("/sessions?include=ended").json()["sessions"]
    assert [(s["id"], s["status"]) for s in listed] == [(live, "pending"), (stopped, "ended")]


def test_ended_history_is_bounded(client, settings):
    ids = [start(client)["id"] for _ in range(settings.ended_history_size + 1)]
    for sid in ids:
        client.delete(f"/sessions/{sid}")
    assert client.get(f"/sessions/{ids[0]}").status_code == 404
    assert client.get(f"/sessions/{ids[-1]}").json()["status"] == "ended"


# -- dispatch and tokens -------------------------------------------------


def test_dispatch_adds_an_agent_to_a_live_session(client):
    sid = start(client, agent=None)["id"]
    res = client.post(f"/sessions/{sid}/dispatches", json={"agent": "voice"})
    assert res.status_code == 201
    assert res.json()["agent"] == "voice"

    session = client.get(f"/sessions/{sid}").json()
    assert session["status"] == "pending"
    assert [d["agent"] for d in session["dispatches"]] == ["voice"]


def test_dispatch_validates_agent_and_session(client):
    sid = start(client)["id"]
    assert client.post(f"/sessions/{sid}/dispatches", json={"agent": "nosuch"}).status_code == 422
    assert client.post(f"/sessions/{sid}/dispatches", json={}).status_code == 422
    client.delete(f"/sessions/{sid}")
    assert client.post(f"/sessions/{sid}/dispatches", json={"agent": "echo"}).status_code == 404


def test_token_for_a_second_participant(client):
    sid = start(client)["id"]
    res = client.post(f"/sessions/{sid}/tokens", json={"identity": "user2"})
    assert res.status_code == 201
    claims = api.TokenVerifier(API_KEY, API_SECRET).verify(res.json()["token"])
    assert claims.identity == "user2" and claims.video.room == sid

    assert client.post("/sessions/s-0000000000/tokens").status_code == 404


# -- webhooks ------------------------------------------------------------


def test_room_finished_webhook_records_the_session_as_ended(client, lk):
    sid = start(client)["id"]
    room = lk.rooms.pop(sid)  # the room closed on its own
    body, auth = sign_webhook(
        api.WebhookEvent(event="room_finished", room=room, room_end_reason="ROOM_END_IDLE_TIMEOUT")
    )
    res = client.post("/webhooks/livekit", content=body, headers={"Authorization": auth})
    assert res.status_code == 204

    ended = client.get(f"/sessions/{sid}").json()
    assert ended["status"] == "ended"
    assert ended["end_reason"] == "idle_timeout"
    assert ended["agent"] == "echo"


def test_session_that_vanished_without_a_webhook_is_still_reported_ended(
    client, lk, monkeypatch
):
    monkeypatch.setattr(sessions, "WEBHOOK_GRACE_SECONDS", 0.05)
    sid = start(client)["id"]
    del lk.rooms[sid]  # closed on its own; the webhook is late or lost

    ended = client.get(f"/sessions/{sid}").json()
    assert (ended["status"], ended["end_reason"], ended["agent"]) == ("ended", "unknown", "echo")
    assert client.delete(f"/sessions/{sid}").status_code == 204


def test_webhook_after_stop_keeps_the_stop_reason(client, lk):
    sid = start(client)["id"]
    room = lk.rooms[sid]
    client.delete(f"/sessions/{sid}")
    body, auth = sign_webhook(
        api.WebhookEvent(event="room_finished", room=room, room_end_reason="ROOM_END_API_DELETE")
    )
    client.post("/webhooks/livekit", content=body, headers={"Authorization": auth})
    assert client.get(f"/sessions/{sid}").json()["end_reason"] == "stopped"


def test_webhook_with_bad_signature_is_rejected(client, lk):
    sid = start(client)["id"]
    event = api.WebhookEvent(event="room_finished", room=lk.rooms[sid])
    body, auth = sign_webhook(event, secret="not-the-secret-not-the-secret-0123")
    assert (
        client.post("/webhooks/livekit", content=body, headers={"Authorization": auth}).status_code
        == 401
    )
    _, good_auth = sign_webhook(event)
    tampered = body.replace("room_finished", "room_started")
    assert (
        client.post(
            "/webhooks/livekit", content=tampered, headers={"Authorization": good_auth}
        ).status_code
        == 401
    )
    assert client.post("/webhooks/livekit", content=body).status_code == 401
    assert client.get(f"/sessions/{sid}").json()["status"] == "pending"


# -- reaper --------------------------------------------------------------


def test_reaper_deletes_only_sessions_past_the_maximum_age(client, lk, settings):
    old, fresh = start(client)["id"], start(client)["id"]
    lk.rooms[old].creation_time = int(time.time()) - settings.session_max_seconds - 5
    lk.rooms["spike"] = api.Room(name="spike", creation_time=1)

    assert asyncio.run(SessionService(lk, settings).reap()) == [old]
    assert set(lk.rooms) == {fresh, "spike"}


def test_reaped_session_reports_expired(client, lk, settings):
    sid = start(client)["id"]
    lk.rooms[sid].creation_time = int(time.time()) - settings.session_max_seconds - 5
    service: SessionService = client.app.state.sessions
    client.portal.call(service.reap)
    assert client.get(f"/sessions/{sid}").json()["end_reason"] == "expired"


# -- health and error mapping --------------------------------------------


def test_health_endpoints(client, lk):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200

    lk.error = aiohttp.ClientConnectionError("connection refused")
    assert client.get("/readyz").status_code == 503
    assert client.get("/healthz").status_code == 200


def test_livekit_failures_map_to_gateway_errors(client, lk):
    lk.error = aiohttp.ClientConnectionError("connection refused")
    assert client.post("/sessions").status_code == 503

    lk.error = api.TwirpError("internal", "boom", status=500)
    assert client.get("/sessions").status_code == 502


def test_cors_allows_the_test_page_origin(client):
    res = client.get("/sessions", headers={"Origin": "http://localhost:8000"})
    assert res.headers["access-control-allow-origin"] == "http://localhost:8000"
    res = client.get("/sessions", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in res.headers

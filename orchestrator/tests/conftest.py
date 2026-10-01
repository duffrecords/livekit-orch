"""A fake LiveKit server API, behaving the way the real one was observed to."""

import base64
import hashlib
import time

import pytest
from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToJson
from livekit import api
from livekit.protocol import agent as agent_proto

from app.config import Settings
from app.main import create_app

API_KEY = "testkey"
API_SECRET = "testsecret-testsecret-testsecret-0123"


class FakeRooms:
    def __init__(self, lk: "FakeLiveKit"):
        self._lk = lk

    async def create_room(self, req: api.CreateRoomRequest) -> api.Room:
        self._lk.check()
        room = api.Room(
            name=req.name,
            metadata=req.metadata,
            empty_timeout=req.empty_timeout,
            departure_timeout=req.departure_timeout,
            creation_time=int(time.time()),
        )
        self._lk.rooms[req.name] = room
        self._lk.participants[req.name] = []
        # a room created without agents gets one dispatch with an empty name
        agents = [a.agent_name for a in req.agents] or [""]
        self._lk.dispatches[req.name] = [
            api.AgentDispatch(id=self._lk.next_id("AD"), agent_name=name, room=req.name)
            for name in agents
        ]
        return room

    async def list_rooms(self, req: api.ListRoomsRequest) -> api.ListRoomsResponse:
        self._lk.check()
        rooms = [r for r in self._lk.rooms.values() if not req.names or r.name in req.names]
        return api.ListRoomsResponse(rooms=rooms)

    async def list_participants(
        self, req: api.ListParticipantsRequest
    ) -> api.ListParticipantsResponse:
        self._lk.check()
        return api.ListParticipantsResponse(participants=self._lk.participants.get(req.room, []))

    async def delete_room(self, req: api.DeleteRoomRequest) -> api.DeleteRoomResponse:
        self._lk.check()
        if req.room not in self._lk.rooms:
            raise api.TwirpError("not_found", "requested room does not exist", status=404)
        del self._lk.rooms[req.room]
        return api.DeleteRoomResponse()


class FakeDispatches:
    def __init__(self, lk: "FakeLiveKit"):
        self._lk = lk

    async def list_dispatch(self, room_name: str) -> list[api.AgentDispatch]:
        self._lk.check()
        if room_name not in self._lk.rooms:
            raise api.TwirpError("unavailable", "no response from servers", status=503)
        return self._lk.dispatches[room_name]

    async def create_dispatch(self, req: api.CreateAgentDispatchRequest) -> api.AgentDispatch:
        self._lk.check()
        dispatch = api.AgentDispatch(
            id=self._lk.next_id("AD"), agent_name=req.agent_name, room=req.room
        )
        dispatch.metadata = req.metadata
        self._lk.dispatches[req.room].append(dispatch)
        return dispatch

    async def delete_dispatch(self, dispatch_id: str, room_name: str) -> api.AgentDispatch:
        self._lk.check()
        dispatches = self._lk.dispatches[room_name]
        found = next(d for d in dispatches if d.id == dispatch_id)
        dispatches.remove(found)
        return found


class FakeLiveKit:
    def __init__(self):
        self.rooms: dict[str, api.Room] = {}
        self.participants: dict[str, list[api.ParticipantInfo]] = {}
        self.dispatches: dict[str, list[api.AgentDispatch]] = {}
        self.error: Exception | None = None
        self._ids = 0
        self.room = FakeRooms(self)
        self.agent_dispatch = FakeDispatches(self)

    def check(self) -> None:
        if self.error:
            raise self.error

    def next_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}_{self._ids}"

    def join(self, room: str, identity: str, agent: bool = False) -> None:
        kind = api.ParticipantInfo.Kind.AGENT if agent else api.ParticipantInfo.Kind.STANDARD
        self.participants[room].append(
            api.ParticipantInfo(
                identity=identity, kind=kind, state=api.ParticipantInfo.State.ACTIVE
            )
        )

    def run_job(self, room: str) -> None:
        """The worker picks up the room's first dispatch and its agent joins."""
        job_id = self.next_id("AJ")
        job = agent_proto.Job(id=job_id)
        job.state.status = agent_proto.JobStatus.JS_RUNNING
        job.state.worker_id = "AW_1"
        self.dispatches[room][0].state.jobs.append(job)
        self.join(room, f"agent-{job_id}", agent=True)

    def leave_all(self, room: str) -> None:
        """Everyone leaves; the job keeps reading "running", as on the real server."""
        self.participants[room].clear()


class FakeWorkers:
    """Stands in for the worker pods: `pods` maps an agent name to its workers."""

    def __init__(self):
        self.pods: dict[str, list[dict]] = {}

    def add(self, agent: str, jobs: int, capacity: int = 5) -> None:
        self.pods.setdefault(agent, []).append(
            {
                "agent": agent,
                "address": f"10.0.0.{sum(map(len, self.pods.values())) + 1}",
                "jobs": jobs,
                "load": jobs / capacity,
                "full": jobs >= capacity,
            }
        )

    def knows(self, agent: str) -> bool:
        return agent in self.pods

    async def for_agent(self, agent: str) -> list[dict]:
        return self.pods.get(agent, [])

    async def everything(self) -> list[dict]:
        return [w for pods in self.pods.values() for w in pods]

    async def aclose(self) -> None:
        pass


def sign_webhook(event: api.WebhookEvent, secret: str = API_SECRET) -> tuple[str, str]:
    """Body and Authorization header as the LiveKit server would send them."""
    body = MessageToJson(event)
    digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    return body, api.AccessToken(API_KEY, secret).with_sha256(digest).to_jwt()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        livekit_api_key=API_KEY,
        livekit_api_secret=API_SECRET,
        agent_names=("echo", "voice"),
        ended_history_size=3,
        reaper_interval_seconds=3600,
        redispatch_interval_seconds=3600,
    )


@pytest.fixture
def lk() -> FakeLiveKit:
    return FakeLiveKit()


@pytest.fixture
def workers() -> FakeWorkers:
    return FakeWorkers()


@pytest.fixture
def client(settings: Settings, lk: FakeLiveKit, workers: FakeWorkers):
    with TestClient(create_app(settings, lk, workers)) as test_client:
        yield test_client

"""Session operations on top of LiveKit.

One session is one LiveKit room, and the room name is the session ID. LiveKit is
the source of truth for live sessions; the only state kept here is a bounded
in-memory record of sessions that have ended.
"""

import asyncio
import json
import logging
import re
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from livekit import api
from livekit.protocol import agent as agent_proto
from livekit.protocol import models as models_proto

from .config import Settings
from .workers import WorkerDirectory

logger = logging.getLogger("orchestrator.sessions")

SESSION_ID = re.compile(r"^s-[0-9a-f]{10}$")

# How long a lookup waits for the room_finished webhook of a room that has just
# disappeared. Measured: the webhook lands about 10 ms after the room is gone.
WEBHOOK_GRACE_SECONDS = 1.0


class SessionNotFound(Exception):
    def __init__(self, session_id: str):
        super().__init__(f"session {session_id} not found")
        self.session_id = session_id


class UnknownAgent(Exception):
    def __init__(self, agent: str, allowed: tuple[str, ...]):
        super().__init__(f"unknown agent {agent!r}; allowed: {', '.join(allowed)}")


def new_session_id() -> str:
    return f"s-{secrets.token_hex(5)}"


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _enum_label(name: str, prefix: str) -> str:
    return name.removeprefix(prefix).lower()


def _room_attributes(room: api.Room) -> dict[str, Any]:
    """Attributes stored in the room metadata when the session was started."""
    try:
        attributes = json.loads(room.metadata) if room.metadata else {}
    except ValueError:
        return {}
    return attributes if isinstance(attributes, dict) else {}


def derive_status(
    agent_expected: bool,
    participants: list[api.ParticipantInfo],
    job_assigned: bool = False,
) -> str:
    """Status of a live session, from who is in the room.

    Agent presence comes from the participant list rather than dispatch job
    state: a job shows up there seconds after its agent has joined, and still
    reads "running" after the agent has left. So job state is only used as
    evidence that an agent was assigned at some point.
    """
    agents = [p for p in participants if p.kind == api.ParticipantInfo.Kind.AGENT]
    users = [p for p in participants if p.kind != api.ParticipantInfo.Kind.AGENT]
    if agent_expected and not agents:
        # an empty room whose agent has come and gone is waiting out its
        # departure timeout
        return "closing" if job_assigned and not users else "pending"
    return "active" if users else "ready"


def _participant(p: api.ParticipantInfo) -> dict[str, Any]:
    return {
        "identity": p.identity,
        "kind": api.ParticipantInfo.Kind.Name(p.kind).lower(),
        "state": api.ParticipantInfo.State.Name(p.state).lower(),
    }


def _dispatch(d: api.AgentDispatch) -> dict[str, Any]:
    return {
        "id": d.id,
        "agent": d.agent_name,
        "jobs": [
            {
                "id": job.id,
                "status": _enum_label(agent_proto.JobStatus.Name(job.state.status), "JS_"),
                "worker_id": job.state.worker_id or None,
            }
            for job in d.state.jobs
        ],
    }


# statuses in which a session occupies, or is waiting for, a worker slot
NEEDS_WORKER = ("pending", "ready", "active")


class SessionService:
    def __init__(
        self,
        lk: api.LiveKitAPI,
        settings: Settings,
        clock: Callable[[], float] = time.time,
        workers: WorkerDirectory | None = None,
    ):
        self._lk = lk
        self._settings = settings
        self._clock = clock
        self._workers = workers or WorkerDirectory(settings)
        # when each session's agent was last dispatched again
        self._redispatched: dict[str, float] = {}
        self._ended: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # live sessions this process has seen, as last observed
        self._seen: dict[str, api.Room] = {}

    # -- lookups ----------------------------------------------------------

    async def ping(self) -> None:
        """Round trip to the LiveKit server API; raises if it can't be reached."""
        await self._lk.room.list_rooms(api.ListRoomsRequest(names=["-readyz-"]))

    async def _live_room(self, session_id: str) -> api.Room | None:
        if not SESSION_ID.match(session_id):
            return None
        # ListDispatch on a missing room fails slowly with `unavailable` and
        # ListParticipants returns an empty list, so existence is checked here.
        res = await self._lk.room.list_rooms(api.ListRoomsRequest(names=[session_id]))
        return res.rooms[0] if res.rooms else None

    async def _require_live_room(self, session_id: str) -> api.Room:
        room = await self._live_room(session_id)
        if room is None:
            raise SessionNotFound(session_id)
        return room

    async def _room_state(
        self, room: api.Room
    ) -> tuple[str, list[api.ParticipantInfo], list[api.AgentDispatch]]:
        """Status, participants and named dispatches of a live room."""
        participants, dispatches = await asyncio.gather(
            self._lk.room.list_participants(api.ListParticipantsRequest(room=room.name)),
            self._lk.agent_dispatch.list_dispatch(room.name),
        )
        # A room created without agents still carries one dispatch with an empty
        # name, addressed to automatically dispatched (unnamed) workers. We only
        # run named agents, so it never produces a job.
        dispatches = [d for d in dispatches if d.agent_name]
        status = derive_status(
            bool(dispatches),
            list(participants.participants),
            job_assigned=any(d.state.jobs for d in dispatches),
        )
        return status, list(participants.participants), dispatches

    async def _pending_reason(self, dispatches: list[api.AgentDispatch]) -> str | None:
        """Why a pending session has no agent, as far as the workers can tell."""
        agent = dispatches[0].agent_name
        if not self._workers.knows(agent):
            return None
        workers = await self._workers.for_agent(agent)
        if not workers:
            return "no_workers"
        if all(w["full"] for w in workers):
            return "workers_full"
        # a worker has room: the job is being assigned, or the dispatch was
        # dropped and is about to be made again
        return "waiting"

    async def _describe(self, room: api.Room) -> dict[str, Any]:
        self._seen[room.name] = room
        status, participants, dispatches = await self._room_state(room)
        attributes = _room_attributes(room)
        return {
            "id": room.name,
            "status": status,
            "pending_reason": (
                await self._pending_reason(dispatches) if status == "pending" else None
            ),
            "agent": attributes.get("agent"),
            "created_at": _iso(room.creation_time),
            "metadata": attributes.get("metadata") or {},
            "participants": [_participant(p) for p in participants],
            "dispatches": [_dispatch(d) for d in dispatches],
        }

    async def get(self, session_id: str) -> dict[str, Any]:
        room = await self._live_room(session_id)
        if room is not None:
            return await self._describe(room)
        ended = await self._ended_record(session_id)
        if ended is None:
            raise SessionNotFound(session_id)
        return ended

    async def _ended_record(self, session_id: str) -> dict[str, Any] | None:
        """The record of a session whose room is gone, if we ever knew of it.

        A room disappears from LiveKit slightly before its room_finished
        webhook arrives. A session seen live earlier and missing now is in that
        gap, so wait for the webhook; if it never comes, record the session as
        ended anyway, without a reason.
        """
        if session_id not in self._ended and session_id in self._seen:
            deadline = time.monotonic() + WEBHOOK_GRACE_SECONDS
            while session_id not in self._ended and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            if session_id not in self._ended:
                self._record_ended(self._seen[session_id], "unknown")
        return self._ended.get(session_id)

    async def list_sessions(self, include_ended: bool = False) -> list[dict[str, Any]]:
        res = await self._lk.room.list_rooms(api.ListRoomsRequest())
        rooms = [room for room in res.rooms if SESSION_ID.match(room.name)]
        described = await asyncio.gather(
            *(self._describe(room) for room in rooms), return_exceptions=True
        )
        # a room can close between the listing and the per-room calls; skip it
        sessions = [s for s in described if isinstance(s, dict)]
        sessions.sort(key=lambda s: s["created_at"])
        if include_ended:
            live = {s["id"] for s in sessions}
            sessions += [s for s in reversed(self._ended.values()) if s["id"] not in live]
        return sessions

    async def demand(self, agent: str) -> dict[str, Any]:
        """How many live sessions need a worker for this agent. Read by the autoscaler."""
        self._check_agent(agent)
        wanting = [
            s
            for s in await self.list_sessions()
            if s["status"] in NEEDS_WORKER and any(d["agent"] == agent for d in s["dispatches"])
        ]
        return {
            "agent": agent,
            "sessions": len(wanting),
            "pending": sum(s["status"] == "pending" for s in wanting),
        }

    async def workers(self) -> list[dict[str, Any]]:
        return await self._workers.everything()

    # -- operations -------------------------------------------------------

    def _check_agent(self, agent: str) -> None:
        if agent not in self._settings.agent_names:
            raise UnknownAgent(agent, self._settings.agent_names)

    def credentials(self, session_id: str, identity: str | None = None) -> dict[str, Any]:
        """A join token for the session's room, and where to use it."""
        identity = identity or f"user-{secrets.token_hex(3)}"
        token = (
            api.AccessToken(self._settings.livekit_api_key, self._settings.livekit_api_secret)
            .with_identity(identity)
            .with_grants(api.VideoGrants(room_join=True, room=session_id))
            .with_ttl(timedelta(seconds=self._settings.token_ttl_seconds))
            .to_jwt()
        )
        return {
            "url": self._settings.livekit_public_url,
            "token": token,
            "identity": identity,
        }

    async def start(
        self,
        agent: str | None,
        identity: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if agent is not None:
            self._check_agent(agent)
        session_id = new_session_id()
        request = api.CreateRoomRequest(
            name=session_id,
            empty_timeout=self._settings.empty_timeout_seconds,
            departure_timeout=self._settings.departure_timeout_seconds,
            metadata=json.dumps({"agent": agent, "metadata": metadata or {}}),
        )
        if agent is not None:
            # Dispatching as part of room creation means the agent is already
            # on its way before any token is handed out.
            request.agents.append(
                api.RoomAgentDispatch(agent_name=agent, metadata=json.dumps(metadata or {}))
            )
        room = await self._lk.room.create_room(request)
        logger.info("session started", extra={"session_id": session_id, "agent": agent})
        session = await self._describe(room)
        return {**session, **self.credentials(session_id, identity)}

    async def token(self, session_id: str, identity: str | None = None) -> dict[str, Any]:
        await self._require_live_room(session_id)
        return self.credentials(session_id, identity)

    async def dispatch(
        self, session_id: str, agent: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._check_agent(agent)
        await self._require_live_room(session_id)
        dispatch = await self._lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=agent, room=session_id, metadata=json.dumps(metadata or {})
            )
        )
        logger.info(
            "agent dispatched",
            extra={"session_id": session_id, "agent": agent, "dispatch_id": dispatch.id},
        )
        return _dispatch(dispatch)

    async def stop(self, session_id: str, reason: str = "stopped") -> None:
        room = await self._live_room(session_id)
        if room is None:
            if await self._ended_record(session_id) is None:
                raise SessionNotFound(session_id)
            return
        await self._delete(room, reason)

    async def _delete(self, room: api.Room, reason: str) -> None:
        # Recorded before the delete so that our reason wins over the one in
        # the room_finished webhook the delete triggers.
        recorded = self._record_ended(room, reason)
        try:
            await self._lk.room.delete_room(api.DeleteRoomRequest(room=room.name))
        except api.TwirpError as err:
            if err.code != "not_found":  # not_found: it closed on its own meanwhile
                if recorded:
                    self._ended.pop(room.name, None)
                raise

    def _record_ended(self, room: api.Room, reason: str) -> bool:
        self._seen.pop(room.name, None)
        if room.name in self._ended:
            return False
        attributes = _room_attributes(room)
        now = self._clock()
        self._ended[room.name] = {
            "id": room.name,
            "status": "ended",
            "agent": attributes.get("agent"),
            "created_at": _iso(room.creation_time) if room.creation_time else None,
            "ended_at": _iso(now),
            "end_reason": reason,
            "metadata": attributes.get("metadata") or {},
            "participants": [],
            "dispatches": [],
        }
        while len(self._ended) > self._settings.ended_history_size:
            self._ended.popitem(last=False)
        duration = round(now - room.creation_time, 1) if room.creation_time else None
        logger.info(
            "session ended",
            extra={"session_id": room.name, "end_reason": reason, "duration_seconds": duration},
        )
        return True

    # -- background -------------------------------------------------------

    async def reap(self) -> list[str]:
        """Delete sessions older than the maximum duration, occupied or not."""
        res = await self._lk.room.list_rooms(api.ListRoomsRequest())
        cutoff = self._clock() - self._settings.session_max_seconds
        expired = [
            room
            for room in res.rooms
            if SESSION_ID.match(room.name) and room.creation_time < cutoff
        ]
        for room in expired:
            await self._delete(room, "expired")
        return [room.name for room in expired]

    async def redispatch(self) -> list[str]:
        """Dispatch again for sessions that have waited too long for their agent.

        LiveKit offers a dispatch to the workers once. If none is registered, or
        all are full, it is dropped: a worker that appears or frees up later is
        not offered it. Observed for both cases against server v1.13.7.
        """
        after = self._settings.redispatch_after_seconds
        now = self._clock()
        res = await self._lk.room.list_rooms(api.ListRoomsRequest())
        rooms = [room for room in res.rooms if SESSION_ID.match(room.name)]
        live = {room.name for room in rooms}
        self._redispatched = {k: v for k, v in self._redispatched.items() if k in live}
        done = []
        for room in rooms:
            if now - max(room.creation_time, self._redispatched.get(room.name, 0)) < after:
                continue
            try:
                if await self._redispatch_room(room):
                    done.append(room.name)
            except api.TwirpError:
                continue  # the room closed meanwhile
        return done

    async def _redispatch_room(self, room: api.Room) -> bool:
        status, _, dispatches = await self._room_state(room)
        if status != "pending":
            return False
        by_agent: dict[str, list[api.AgentDispatch]] = {}
        for d in dispatches:
            by_agent.setdefault(d.agent_name, []).append(d)
        redone = False
        for agent, stale in by_agent.items():
            if self._workers.knows(agent) and all(
                w["full"] for w in await self._workers.for_agent(agent)
            ):
                continue  # nowhere to send it yet
            # replaced rather than added to, so a late answer to the old
            # dispatch can't put a second agent in the room
            for d in stale:
                await self._lk.agent_dispatch.delete_dispatch(d.id, room.name)
            fresh = await self._lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=agent, room=room.name, metadata=stale[0].metadata
                )
            )
            logger.info(
                "agent re-dispatched",
                extra={"session_id": room.name, "agent": agent, "dispatch_id": fresh.id},
            )
            redone = True
        if redone:
            self._redispatched[room.name] = self._clock()
        return redone

    def handle_webhook(self, event: api.WebhookEvent) -> None:
        room = event.room.name or None
        participant = event.participant.identity or None
        logger.info(
            "livekit event",
            extra={"event": event.event, "session_id": room, "participant": participant},
        )
        if event.event == "room_finished" and room and SESSION_ID.match(room):
            reason = models_proto.RoomEndReason.Name(event.room_end_reason)
            self._record_ended(event.room, _enum_label(reason, "ROOM_END_"))

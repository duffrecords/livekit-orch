"""Voice agent: a spoken conversation through external speech and LLM APIs.

User audio goes through voice-activity and end-of-turn detection (both run in
this process), then speech-to-text, an LLM and text-to-speech at the configured
providers. Nothing is sent to a provider until a user has joined the room.
"""

import json
import logging
import os
import sys
import time

from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    AutoSubscribe,
    ChatMessage,
    CloseEvent,
    CloseReason,
    ConversationItemAddedEvent,
    ErrorEvent,
    JobContext,
    cli,
    inference,
)
from livekit.agents.voice.room_io import RoomOptions

import providers

logger = logging.getLogger("voice-agent")

# provider/model for each stage; see providers.py
SPECS = {
    "stt": os.getenv("VOICE_STT") or "openai/gpt-4o-mini-transcribe",
    "llm": os.getenv("VOICE_LLM") or "openai/gpt-4.1-mini",
    "tts": os.getenv("VOICE_TTS") or "openai/gpt-4o-mini-tts",
}
TTS_VOICE = os.getenv("VOICE_TTS_VOICE", "")
INSTRUCTIONS = os.getenv("VOICE_INSTRUCTIONS") or (
    "You are a friendly voice assistant in a demo of a voice-agent platform. "
    "Your replies are spoken aloud: keep them to one or two short sentences, "
    "in plain words, without lists, markup or emoji."
)
# spoken as soon as the user joins; empty for none
GREETING = os.getenv("VOICE_GREETING", "Hi, I'm listening. What would you like to talk about?")

# per-turn delays worth a place in the log, from ChatMessage.metrics
TURN_METRICS = (
    "transcription_delay",
    "end_of_turn_delay",
    "llm_node_ttft",
    "tts_node_ttfb",
    "e2e_latency",
)

# Name the worker registers under; sessions are dispatched to it by this name.
AGENT_NAME = os.getenv("AGENT_NAME", "voice")
# Sessions this worker pod runs at once. Capacity is counted in jobs, as for
# the echo agent.
MAX_JOBS = int(os.getenv("MAX_JOBS", "5"))
# How long a worker told to stop (SIGTERM) waits for its sessions to end. Keep
# the pod's terminationGracePeriodSeconds above this.
DRAIN_TIMEOUT_SECONDS = int(os.getenv("DRAIN_TIMEOUT_SECONDS", "1800"))


try:
    providers.load(SPECS)
except providers.ConfigError as err:
    sys.exit(f"voice agent configuration: {err}")


def job_load(worker: AgentServer) -> float:
    return len(worker.active_jobs) / MAX_JOBS


def job_options(metadata: str) -> tuple[str, str]:
    """Instructions and greeting for one session.

    The dispatch metadata is the JSON object given as `metadata` when the
    session was started; its `instructions` and `greeting` replace the defaults.
    """
    try:
        given = json.loads(metadata) if metadata else {}
    except ValueError:
        given = {}
    if not isinstance(given, dict):
        given = {}
    instructions = given.get("instructions")
    greeting = given.get("greeting")
    return (
        instructions if isinstance(instructions, str) and instructions else INSTRUCTIONS,
        greeting if isinstance(greeting, str) else GREETING,
    )


def usage_summary(session: AgentSession) -> dict[str, dict[str, float]]:
    """What the session used at each provider, keyed `provider/model`."""
    summary = {}
    for usage in session.usage.model_usage:
        fields = usage.model_dump(exclude={"type", "provider", "model"})
        summary[f"{usage.provider}/{usage.model}"] = {k: v for k, v in fields.items() if v}
    return summary


server = AgentServer(
    load_fnc=job_load,
    # full, and refusing jobs, at exactly MAX_JOBS
    load_threshold=1.0,
    # One warm process per slot. This is also what makes the worker count a
    # job it has accepted but not yet started as one slot.
    num_idle_processes=MAX_JOBS,
    drain_timeout=DRAIN_TIMEOUT_SECONDS,
)


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext):
    instructions, greeting = job_options(ctx.job.metadata)
    logger.info("connecting to room %s", ctx.room.name)
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # The pipeline starts only once someone is there to talk to, so a session
    # nobody joins costs nothing at the providers.
    try:
        participant = await ctx.wait_for_participant()
    except RuntimeError:
        # the room closed (e.g. empty/departure timeout) before anyone joined
        logger.info("room %s closed before a participant joined", ctx.room.name)
        return

    session = AgentSession(
        stt=providers.build_stt(SPECS["stt"]),
        llm=providers.build_llm(SPECS["llm"]),
        tts=providers.build_tts(SPECS["tts"], TTS_VOICE),
        # Both are pinned to the models that run in this process. Left to
        # their defaults they call LiveKit Cloud when run with `dev`.
        turn_handling={
            "turn_detection": inference.TurnDetector(version="v1-mini"),
            "interruption": {"mode": "vad"},
        },
    )
    started = time.monotonic()

    @session.on("error")
    def on_error(event: ErrorEvent):
        # The framework logs every failed attempt with its traceback. This is
        # the one line that says which room and stage, once retries are spent.
        error = event.error
        if getattr(error, "recoverable", False):
            return
        logger.error(
            "provider error in room %s: %s: %.300s",
            ctx.room.name,
            getattr(error, "label", type(error).__name__),
            getattr(error, "error", error),
        )

    @session.on("conversation_item_added")
    def on_item(event: ConversationItemAddedEvent):
        item = event.item
        if not isinstance(item, ChatMessage):
            return
        text = item.text_content or ""
        # what was said is logged at debug level only
        logger.debug("room %s %s: %s", ctx.room.name, item.role, text)
        logger.info(
            "turn in room %s: %s chars=%d%s%s",
            ctx.room.name,
            item.role,
            len(text),
            " interrupted" if item.interrupted else "",
            "".join(
                f" {name}={item.metrics[name]:.2f}s"
                for name in TURN_METRICS
                if name in (item.metrics or {})
            ),
        )

    @session.on("close")
    def on_close(event: CloseEvent):
        logger.info(
            "session in room %s closed: reason=%s duration=%.0fs usage=%s",
            ctx.room.name,
            event.reason.value,
            time.monotonic() - started,
            json.dumps(usage_summary(session)),
        )
        if event.reason == CloseReason.ERROR:
            # Stay in the room, silent. If the agent left, the orchestrator
            # would see a session without its agent and dispatch a new one,
            # which would fail the same way, every few seconds.
            logger.error(
                "room %s: giving up after a provider error; staying in the room without a pipeline",
                ctx.room.name,
            )
            return
        # end the job so the agent doesn't linger silently in the room
        ctx.shutdown(reason=event.reason.value)

    logger.info(
        "starting session in room %s with %s: stt=%s llm=%s tts=%s",
        ctx.room.name,
        participant.identity,
        SPECS["stt"],
        SPECS["llm"],
        SPECS["tts"],
    )
    await session.start(
        Agent(instructions=instructions),
        room=ctx.room,
        room_options=RoomOptions(participant_identity=participant.identity),
        # recording uploads to LiveKit Cloud
        record=False,
    )
    if greeting:
        session.say(greeting)


if __name__ == "__main__":
    # Fail at startup, with the reason, rather than in a session's first turn.
    missing = providers.missing_keys(SPECS)
    if missing:
        sys.exit(f"voice agent configuration: {', '.join(missing)} not set")
    cli.run_app(server)

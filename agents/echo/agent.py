"""Keyless echo agent: replays the user's microphone audio after a short delay.

Needs no speech/LLM provider, so it exercises dispatch and agent media in both
directions (subscribe and publish) without API keys.
"""

import asyncio
import logging
import os
from collections import deque

from livekit import rtc
from livekit.agents import AgentServer, AutoSubscribe, JobContext, cli

logger = logging.getLogger("echo-agent")

SAMPLE_RATE = 48000
ECHO_DELAY_SECONDS = float(os.getenv("ECHO_DELAY_SECONDS", "1.0"))
LOG_INTERVAL_SECONDS = 5.0

# Name the worker registers under; sessions are dispatched to it by this name.
AGENT_NAME = os.getenv("AGENT_NAME", "echo")
# Sessions this worker pod runs at once. The framework's default load measure
# is CPU use, which an echo job barely moves, so capacity is counted in jobs.
MAX_JOBS = int(os.getenv("MAX_JOBS", "5"))
# How long a worker told to stop (SIGTERM) waits for its sessions to end. Keep
# the pod's terminationGracePeriodSeconds above this.
DRAIN_TIMEOUT_SECONDS = int(os.getenv("DRAIN_TIMEOUT_SECONDS", "1800"))


def job_load(worker: AgentServer) -> float:
    return len(worker.active_jobs) / MAX_JOBS


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
    logger.info("connecting to room %s", ctx.room.name)
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    source = rtc.AudioSource(sample_rate=SAMPLE_RATE, num_channels=1)
    track = rtc.LocalAudioTrack.create_audio_track("echo", source)
    await ctx.room.local_participant.publish_track(
        track,
        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
    )

    try:
        participant = await ctx.wait_for_participant()
    except RuntimeError:
        # the room closed (e.g. empty/departure timeout) before anyone joined
        logger.info("room %s closed before a participant joined", ctx.room.name)
        return
    logger.info("echoing participant %s", participant.identity)
    stream = rtc.AudioStream.from_participant(
        participant=participant,
        track_source=rtc.TrackSource.SOURCE_MICROPHONE,
        sample_rate=SAMPLE_RATE,
        num_channels=1,
    )

    delayed: deque[rtc.AudioFrame] = deque()
    buffered = 0.0
    received = 0
    played = 0
    loop = asyncio.get_running_loop()
    last_log = loop.time()

    async for event in stream:
        frame = event.frame
        received += 1
        delayed.append(frame)
        buffered += frame.duration
        while buffered > ECHO_DELAY_SECONDS:
            out = delayed.popleft()
            buffered -= out.duration
            await source.capture_frame(out)
            played += 1

        now = loop.time()
        if now - last_log >= LOG_INTERVAL_SECONDS:
            logger.info("frames received=%d played=%d", received, played)
            last_log = now

    logger.info("audio stream ended: frames received=%d played=%d", received, played)
    # The stream ends when the participant leaves; end the job so the agent
    # doesn't linger silently in the room.
    ctx.shutdown(reason="participant left")


if __name__ == "__main__":
    cli.run_app(server)

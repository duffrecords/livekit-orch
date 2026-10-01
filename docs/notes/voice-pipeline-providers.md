# Voice pipeline: external APIs vs. local models

## Decision

External APIs for STT/LLM/TTS, plus a keyless mock agent. No local models in the cluster. The project's focus is orchestration (dispatch, concurrency, scaling, cleanup, health); local models make those harder to demonstrate.

## Why not local models

- **No GPU in containers on Mac/Windows** (no Metal passthrough; Windows GPU setup fragile). CPU-only Whisper + LLM + TTS likely means multi-second turns — feels broken for voice.
- **Concurrency multiplies load.** Several simultaneous sessions on CPU models would overwhelm a laptop; scaling demo would measure model performance, not orchestration.
- **Heavy for reviewers:** multi-GB images, high memory.
- **Muddies scaling story:** model serving has separate concerns (GPU scheduling, batching). Want: stateless workers scaled by active jobs.

## Why external APIs

- Small, CPU-light agent pods; scaling is purely sessions-per-worker.
- Sub-second turn latency (with streaming providers; see the measurements below).
- Trade-offs to document in README: API keys required, audio leaves the machine, per-session cost (cents), provider uptime/rate limits.

## The voice agent (`agents/voice/`, agent name `voice`)

Original plan: `plans/plan-the-provider-backed-voice-ancient-pascal.md`.

- **Pipeline:** `AgentSession` with STT, LLM and TTS plugins, the bundled Silero VAD and the local end-of-turn model. It greets the user when they join, answers in short spoken sentences, and can be interrupted.
- **Providers are configuration.** Each stage is `provider/model` in `.env`; `make deploy` copies `VOICE_*` into the `agent-voice-config` ConfigMap and restarts the workers when it changed.

  | Variable | Default | Alternatives |
  |---|---|---|
  | `VOICE_STT` | `openai/gpt-4o-mini-transcribe` | `deepgram/nova-3` |
  | `VOICE_LLM` | `openai/gpt-4.1-mini` | `anthropic/claude-haiku-4-5` |
  | `VOICE_TTS` | `openai/gpt-4o-mini-tts` | `deepgram/aura-2-andromeda-en` |
  | `VOICE_TTS_VOICE` | plugin default (`ash`) | OpenAI voices only; a Deepgram voice is part of the model name |
  | `VOICE_INSTRUCTIONS`, `VOICE_GREETING` | in `agent.py` | free text; an empty greeting means none |

- **Keys:** `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY` in `.env`, whichever the configured providers need. `make deploy` pipes them into the `provider-keys` Secret through stdin (never on a command line). The default pipeline needs only `OPENAI_API_KEY`.
- **Deployed only with a key.** With no provider key in `.env`, `make up` neither builds nor deploys the voice agent and removes one that exists; everything keyless is unchanged. The orchestrator always lists `voice`, so a voice session without workers reads `pending (no_workers)`.
- **Bad configuration fails at startup:** an unknown provider, or a missing key for a configured one, exits with one line (`voice agent configuration: OPENAI_API_KEY not set`).
- **Per-session options:** the session's `metadata` reaches the agent as dispatch metadata; `instructions` and `greeting` there replace the defaults: `POST /sessions {"agent": "voice", "metadata": {"instructions": "You are a pirate."}}`.
- **No provider cost without a user.** The pipeline starts only after a participant joins, so `make smoke AGENT=voice` and `make load AGENT=voice` are free.
- **Logs:** one line when a session starts (room, user, the three `provider/model` values), one per turn (role, length, delays), one per provider error that is out of retries, and one at the end with the usage per provider. What was said is logged at debug level only (`LIVEKIT_LOG_LEVEL=debug`).
- **Browser client:** `web/index.html` shows the transcript (the `lk.transcription` text stream) and the agent's state (`lk.agent.state`).

## What the framework does (`livekit-agents` 1.8.3; findings that shaped this)

- **A model given as a string goes to LiveKit Cloud.** `AgentSession(llm="openai/…")` uses `agents.inference`, whose endpoint is LiveKit Cloud's agent gateway. The agent always builds plugin objects (`providers.py`).
- **VAD and turn detection are in the `livekit-agents` install** (the `livekit-local-inference` wheel, 34 MB): no Silero or turn-detector plugin and no `download-files` step in the image. Every worker preloads both models, echo included.
- **Two defaults reach for LiveKit Cloud when run with `dev`:** the turn detector's `v1` model and adaptive interruption detection. The agent pins `TurnDetector(version="v1-mini")` and `interruption: {"mode": "vad"}`.
- `AgentSession.start(record=…)` defaults to the job's server-side setting (off on a self-hosted server); the agent passes `record=False` anyway.
- **A provider plugin first imported inside a job blocks its event loop** (298 ms measured for Anthropic). The configured plugins are imported when the worker process starts.
- **Session end:** the session closes when its user leaves on purpose and the agent then ends the job. A user who vanishes (browser killed) is dropped by LiveKit after about 18 s; the room closes on its departure timeout with the agent still in it, which ends the job.
- **A greeting spoken to a participant that is not subscribed to the agent's audio is never recorded as played.** (Seen with `lk room join` without `--auto-subscribe`.)
- **After a provider error the agent stays in the room.** A failed turn is logged and the next turn is tried again; when the framework gives up it closes the session with reason `error`, and the agent then stays, silent, until the room closes. Leaving would make the session read `pending` and the orchestrator would dispatch a new agent every 5 s.

## Verified (2026-10-01, macOS, kind)

Conversations were driven by synthesized speech (`say`), published with `lk room join --publish` or through headless Chrome's fake microphone.

| Check | Result |
|---|---|
| Default pipeline (all OpenAI) | Greeting, two questions answered correctly, second question interrupted the first answer |
| Swap by configuration | Deepgram STT + Claude Haiku 4.5 + Deepgram TTS, and Deepgram + `openai/gpt-4.1-mini`, with no code change |
| Per-session metadata | `instructions` and `greeting` applied |
| Browser client | Transcript of both sides and agent state shown; agent audio received |
| User leaves / browser killed / `DELETE` | Job ends; room closes (`closing` → `ended`); nothing left behind |
| Missing key, unknown provider | Worker exits at startup with the reason |
| Nonexistent model | One error line per failed turn, agent stays in the room, no re-dispatch |
| Provider failing for a whole session (rate limit) | Session closed with reason `error`, agent stayed until the room closed |
| No provider key (`make VOICE= deploy`) | Voice objects, Secret and ConfigMap removed; `make smoke` passes; a voice session reads `pending (no_workers)` |
| `make smoke AGENT=voice` (10), `make load AGENT=voice COUNT=8` | Pass, 1 → 3 → 1 pods, all ready after 13 s; no provider calls |
| `rollout restart` during a conversation | The old pod drained: the conversation ran to its end, then the pod exited |
| `make test` | 33 orchestrator tests, 23 voice agent tests |

Turn latency (end of user speech to start of agent speech) and where it goes:

| | OpenAI (default) | Deepgram + Claude Haiku 4.5 |
|---|---|---|
| End-to-end, five concurrent sessions | median 2.5 s (1.7 to 3.6) | median 0.93 s (0.85 to 1.02) |
| Transcript after end of speech | 0.8 to 1.6 s | 0.2 s |
| LLM first token | 0.65 to 1.1 s | 0.45 to 0.65 s |
| TTS first audio | 0.9 to 2.1 s | 0.1 to 0.2 s |

Usage of one 30 s conversation (greeting and two short answers), from the end-of-session log line:

| | OpenAI | Deepgram + Claude |
|---|---|---|
| STT | 4.4 s of audio (only speech segments are sent) | 25 s of audio (the stream is open for the whole session) |
| LLM | 174 input, 14 output tokens | 156 input, 59 output tokens |
| TTS | 119 characters, 14 s of audio | 286 characters, 16.5 s of audio |

Not verified: a person talking through a real microphone and listening (all audio was synthetic and checked through transcripts, logs and inbound audio energy); Firefox and Safari with the voice agent; Deepgram or Anthropic keys that are missing or invalid, as opposed to a wrong model name.

## Dependencies and trade-offs

- **Audio and text leave the machine** for whichever providers are configured, and the worker pods need outbound internet access.
- **Provider rate limits cap concurrency before the cluster does.** On a new OpenAI account with little credit, transcription and TTS were limited to 10 requests per minute: five concurrent conversations exceeded it and turns failed with 429. After credits were added the same five conversations ran all 20 turns without an error, as they did on Deepgram and Anthropic from the start. Check the account's limits before a multi-session demo.
- **The default trades latency for one key.** OpenAI transcription is not streaming (each speech segment is uploaded after it ends) and its TTS is slower to first audio, so turns take about 2.5 s against about 0.9 s.
- **Changing providers restarts the workers**, and a worker waits for its sessions before exiting. Stop sessions first.
- **A silent agent after a provider error reads `active`** in the API. The error is only in the worker log.
- **Three plugins in one image.** Only the configured ones are imported.
- Not used: LiveKit's hosted inference gateway (ties to LiveKit Cloud, conflicts with the self-hosted requirement).

## Keyless mock agent

Second agent type (`echo`: replays user audio after a delay), same worker framework, dispatched by `agent_name`. Enables:
- Reviewers verifying all orchestration features without signing up for anything.
- Free load tests (10–20 concurrent sessions) to show scale-up/down.
- CI tests.

## Other details

- **Secrets:** git-ignored `.env` → Kubernetes Secret; Helm chart references an existing Secret; never in `values.yaml`.
- **Stretch goal:** optional profile pointing the LLM at Ollama on the **host** (uses Mac GPU), not in-cluster. One README paragraph, not a core feature.

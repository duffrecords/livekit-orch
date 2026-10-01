# Provider-backed voice agent

> **Status: done.** Kept as the original plan. Differences in the result: the agent was
> developed in the cluster, not from the host; tests are in `tests/test_agent.py`; plugins are
> imported at worker start, not lazily (an import inside a job stalled it for 0.3 s);
> interruption detection is pinned to `vad` as well as the turn detector, since both default to
> LiveKit Cloud under `dev`; provider keys and settings are applied by `make deploy`, not
> `make secret`, so editing `.env` and redeploying is enough; a failed turn does not close the
> session, so the agent only goes silent after the framework gives up; each turn is logged with
> its delays; requests are `400m` / `900Mi` with a `1536Mi` limit, and no CPU trigger was
> needed; `load.sh` lists only the tested agent's workers. The OpenAI account's rate limit
> (10 requests per minute for transcription and TTS) at first failed five concurrent
> conversations; they passed once credits were added to the account. A person on a real
> microphone was checked afterwards, by hand, in Chrome, Firefox and Safari. Not checked:
> "provider dashboards show no usage" (checked through the worker log instead).
> What was built and measured: `docs/notes/voice-pipeline-providers.md`.

## Context

Networking, the orchestrator API and worker scaling are done, all demonstrated with the keyless
`echo` agent. The requirement is *voice-agent* workers: something a person can talk to. This
milestone adds a second agent, `voice`, that runs a real STT → LLM → TTS conversation through
external APIs, on the same dispatch, capacity, autoscaling and drain machinery as `echo`. Helm
and the README remain later milestones.

Confirmed with you: **OpenAI pipeline by default** (STT, LLM and TTS behind one
`OPENAI_API_KEY`), with **OpenAI, Deepgram and Anthropic swappable per stage** by config.

**Outcome:** with `OPENAI_API_KEY` in `.env`, `make up` deploys an `agent-voice` worker.
`make web` → agent `voice` → Start session gives a spoken greeting and a conversation with
interruptions, and the page shows the transcript. `make smoke AGENT=voice` and
`make load AGENT=voice` pass. Without a provider key, `make up` behaves exactly as today.

## What the framework does (probed 2026-09-30, `livekit-agents` 1.8.3 in the running pod)

| Probe | Result | Consequence |
|---|---|---|
| `AgentSession(stt=, llm=, tts=, vad=, turn_handling=)` | `stt`/`llm`/`tts` take a plugin object **or a string**. A string (`"openai/…"`) goes through `agents.inference`, whose base URL is LiveKit Cloud's agent gateway | Always pass plugin objects. Strings would send audio to LiveKit Cloud signed with our local keys, which the notes rule out |
| VAD | Defaults to `inference.VAD(model="silero")`, native code in the `livekit-local-inference` wheel (a dependency of `livekit-agents`) | No `livekit-plugins-silero`, and **no `download-files` step in the Dockerfile**. The notes' line about that is outdated |
| Turn detection | Defaults to `inference.TurnDetector()`. It picks the cloud `v1` model only when `LIVEKIT_REMOTE_EOT_URL` or `LIVEKIT_DEV_MODE=1` is set, else the local `v1-mini` (also in that wheel) | Pin `TurnDetector(version="v1-mini")` so it can never reach for the gateway. No `livekit-plugins-turn-detector` |
| Preload | The worker's fork server already loads the VAD and end-of-turn weights in every worker, echo included | The 490 MB idle figure already contains them; the voice pod should idle near the same, plus plugin imports |
| Session end | `RoomOptions.close_on_disconnect` defaults to true: the session closes when its user leaves. `close` carries a `CloseReason` (`participant_disconnected`, `error`, …) and the error | Hook `close` to end the job, as echo does with `ctx.shutdown` |
| Usage | `session_usage_updated` event (`metrics_collected` is deprecated) | Source for the per-session usage log line |
| Dispatch metadata | `ctx.job.metadata` is the JSON the orchestrator already sends (`SessionService.start` → `RoomAgentDispatch.metadata`) | Per-session `instructions` / `greeting` with no orchestrator code change |
| Plugins | `livekit-plugins-openai`, `-deepgram`, `-anthropic` are all at 1.8.3 on PyPI | Pin `~=1.8` next to `livekit-agents~=1.8` |

Not yet probed (first tasks of step 1): the plugin constructors (`openai.STT/LLM/TTS`,
`deepgram.STT/TTS`, `anthropic.LLM`) and current model names; whether `AgentSession.start`'s
`record` default sends anything to LiveKit Cloud (pass `record=False` if so); whether a closed
session already ends the job or the agent lingers in the room.

## Design

| Piece | Choice | Why |
|---|---|---|
| Agent | `agents/voice/`, `agent_name="voice"`, own image `livekit-orch/agent-voice:dev`, own Deployment `agent-voice` | Same shape as echo. Echo stays the first (default) agent, so everything keyless is untouched |
| Provider config | Three env vars, each `provider/model`: `VOICE_STT`, `VOICE_LLM`, `VOICE_TTS`, plus `VOICE_TTS_VOICE`. Defaults `openai/gpt-4o-mini-transcribe`, `openai/gpt-4.1-mini`, `openai/gpt-4o-mini-tts` (model names to be confirmed in step 1). A small `providers.py` maps the prefix to a plugin constructor with a lazy import: `openai` (all three), `deepgram` (STT, TTS), `anthropic` (LLM) | Swap = config change. Per-stage vars cover both rows of the notes' provider table (all-OpenAI; Deepgram + Claude Haiku 4.5) |
| Keys | `.env`: `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY` (all optional). `make secret` pipes those lines into `kubectl create secret generic provider-keys --from-env-file=/dev/stdin`, so keys never appear on a command line or in a make variable. The Deployment reads them with `envFrom` | Follows the existing `.env` → Secret convention; the Helm chart later references this existing Secret |
| Deployed only with a key | Voice manifests live in `deploy/voice/` (not picked up by `kubectl apply -f deploy/`, which is not recursive). `make image` / `make deploy` build and apply them only when `.env` has a provider key. `VOICE_*` values from `.env` go into a `agent-voice-config` ConfigMap, the same way `node-ip` builds `livekit-node` | `make up` must keep working with no keys. This becomes `voice.enabled` in Helm |
| Missing key for a configured provider | Worker checks at startup and exits with one log line naming the variable | A crash loop with a clear message beats sessions that fail on the first word |
| Orchestrator | Manifest only: `AGENT_NAMES=echo,voice`, `WORKER_SERVICES` gains `voice=agent-voice.livekit.svc.cluster.local:8081`. No code change | It is already agent-agnostic. With no voice workers a voice session reads `pending (no_workers)` until the empty timeout, which is the truth |
| Capacity and scaling | Copy echo's `AgentServer` settings (`job_load`, `load_threshold=1.0`, `num_idle_processes=MAX_JOBS`, `drain_timeout`), grace period 1860, `maxUnavailable: 0`. Headless Service and KEDA `ScaledObject` on `/scaling/voice`, target 3, min 1, **max 3** | Required by the convention in `CLAUDE.md`. Max 3 because six echo pods already use about 3 GB of the 8 GB Docker VM. `MAX_JOBS` and requests start at echo's values and are corrected by measurement (step 4) |
| No provider cost without a user | `ctx.connect()`, then `ctx.wait_for_participant()` (same `RuntimeError` handling as echo), and only then start the `AgentSession` and greet | `make load` and `make smoke` create sessions nobody joins; they must stay free |
| Session end | On `close` for `participant_disconnected`: `ctx.shutdown`. On `close` for `error`: log the provider error and **stay in the room, silent** | If the agent left on a bad key or an outage, the session would read `pending` and the orchestrator would re-dispatch a new failing agent every 5 s |
| Per-session options | Dispatch metadata `instructions` and `greeting` override the defaults (`VOICE_INSTRUCTIONS`, `VOICE_GREETING`) | Shows metadata flowing end to end: `POST /sessions {"agent":"voice","metadata":{"instructions":"…"}}` |
| Logs | Same `logging` setup as echo. One line each for: session start (room, the three `provider/model` values, never keys), provider error, and session end with usage (LLM tokens, STT seconds, TTS characters, duration). Transcripts at DEBUG only | "Basic logs", and the usage line is the input for the cost table in the notes |
| Web client | `web/index.html`: transcript panel fed by the `lk.transcription` text stream, and the agent's `lk.agent.state` attribute (listening / thinking / speaking) in Status | Makes the demo legible, and gives a check that doesn't depend on hearing audio |

## Files

Create:

- `agents/voice/agent.py`: `AgentServer` with the capacity settings from `agents/echo/agent.py`; `@server.rtc_session(agent_name="voice")` entrypoint as described above.
- `agents/voice/providers.py`: `build_stt/llm/tts(spec)`, `required_keys(specs)`.
- `agents/voice/tests/test_providers.py`: spec parsing, unknown provider or stage, required-key check, metadata overrides. No network, no keys.
- `agents/voice/pyproject.toml`, `uv.lock`, `Dockerfile` (echo's Dockerfile plus `providers.py`; no model download).
- `deploy/voice/agent-voice.yaml` (Deployment + headless Service) and `deploy/voice/agent-voice-scaling.yaml`, modelled on `deploy/agent-echo.yaml` and `deploy/agent-echo-scaling.yaml`.

Change:

- `Makefile`: `VOICE_IMAGE`; voice detection from `.env`; `image`, `_secret`, `deploy` as above; `logs` and `dispatch` take `AGENT`; `test` also runs `agents/voice` tests.
- `deploy/orchestrator.yaml`: `AGENT_NAMES`, `WORKER_SERVICES`.
- `.env.example`: the three optional keys and the `VOICE_*` settings, commented.
- `web/index.html`: transcript and agent state.
- `scripts/load.sh`: comment only, if `HOLD`/timeouts need different values for `voice`.
- `docs/notes/voice-pipeline-providers.md`, `docs/notes/scaling.md`, `CLAUDE.md`; this plan marked done with differences.

No change to orchestrator code or `kind-config.yaml`; the cluster does not need recreating.

## Steps and pass criteria

1. **Agent, run from the host.** Confirm the three unprobed points and the plugin APIs against the current LiveKit docs and the installed plugin source. Write `agent.py`, `providers.py`, tests. Run it on the host against the cluster (`uv run agent.py dev` with `LIVEKIT_URL=ws://localhost:7880`) for a fast loop. Pass: unit tests green; a browser session gets a greeting, answers a question, and can be interrupted.
2. **In the cluster.** Dockerfile, manifests, Makefile, orchestrator env. Pass: `make up` with a key brings up `agent-voice`; `GET /workers` lists it; `make session AGENT=voice` reaches `ready`; the browser conversation works; the user leaving ends the job and the room closes after the departure timeout; `DELETE` ends it at once. With the keys removed from `.env` on a fresh cluster, `make up` and `make smoke` pass and no voice objects exist.
3. **Swap and failure.** Pass: `VOICE_LLM=anthropic/claude-haiku-4-5` (or a Deepgram stage, whichever key you have) works after `make deploy` with no code change; a key missing for a configured provider gives the startup log line; an invalid key gives one provider-error line, the agent stays in the room, and the orchestrator log shows no re-dispatch loop.
4. **Measure and scale.** `make top` and the cgroup method from `docs/notes/scaling.md` with several live conversations (fake-microphone Chrome or `lk room join --publish`). Set `MAX_JOBS`, requests and the ScaledObject target from the numbers; decide whether a CPU trigger is needed (the open point in the scaling notes). Pass: `make smoke AGENT=voice` and `make load AGENT=voice COUNT=8` green, the provider dashboards show no usage from them; a `rollout restart` during a live conversation does not cut it off.
5. **Client and wrap-up.** Transcript and agent state in `web/index.html`. Notes updated: what was verified, per-session resource and API cost, turn latency from the logs, dependencies and trade-offs (keys, audio leaving the machine, provider limits). `CLAUDE.md` decisions and next milestone. No commits unless you ask.

## Risks and open points

- **Model names and plugin signatures** are from memory until step 1 confirms them; the defaults in the table may change.
- **Concurrency against provider rate limits.** A new OpenAI key may allow only a few parallel STT/TTS requests; that, not CPU, may cap real concurrent conversations. Record what is observed.
- **Non-streaming STT.** OpenAI transcription runs per VAD segment, so turns are slower than with Deepgram's streaming STT. Measure both if a Deepgram key is available; otherwise document it.
- **Changing providers restarts workers**, and a restarting worker waits for its sessions (up to 30 minutes by design). Stop sessions first.
- **Silent agent after a provider error** reads `active` to the API. Surfacing agent errors in session status is left out; note it as a known gap.
- **Memory.** One idle voice pod adds about 0.5 GB; three voice plus six echo pods would not fit the 8 GB VM together. Demo one agent type under load at a time.

## Not in this milestone

Helm, the README, tools/function calling, the Realtime speech-to-speech model, LLM on host
Ollama, session-status reporting of agent errors, scale-to-zero, API authentication.

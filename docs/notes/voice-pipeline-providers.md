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
- Sub-second turn latency.
- Trade-offs to document in README: API keys required, audio leaves the machine, per-session cost (cents), provider uptime/rate limits.

## Provider options

| Option | Keys | Notes |
|---|---|---|
| OpenAI only (STT+LLM+TTS, or one Realtime speech-to-speech model) | 1 | Easiest for reviewers; default pick |
| Deepgram (STT + Aura TTS) + Claude Haiku 4.5 (LLM) | 2 | Fast STT/TTS, strong low-latency LLM; Agents plugins exist for both |
| Deepgram + LLM + Cartesia/ElevenLabs | 3 | Best latency/voice quality; too many keys for reviewers |

- Make providers swappable via env vars / Helm values — config change, not code change.
- Avoid LiveKit's hosted inference gateway: ties to LiveKit Cloud, conflicts with self-hosted requirement.

## Keyless mock agent

Second agent type (echo user audio after a delay, or play a tone/clip), same worker framework, dispatched by `agent_name` (e.g. `POST /sessions {"agent": "echo"}`). Enables:
- Reviewers verifying all orchestration features without signing up for anything.
- Free load tests (10–20 concurrent sessions) to show scale-up/down.
- CI tests.

## Other details

- **Silero VAD and LiveKit turn-detector run locally in the worker** regardless of provider (small, CPU-friendly). Download at image build time (Agents CLI `download-files`) so pods don't fetch models at startup and readiness isn't blocked on network.
- **Secrets:** git-ignored `.env` → Kubernetes Secret; Helm chart references an existing Secret; never in `values.yaml`.
- **Stretch goal:** optional profile pointing the LLM at Ollama on the **host** (uses Mac GPU), not in-cluster. One README paragraph, not a core feature.

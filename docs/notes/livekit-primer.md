# LiveKit primer

## LiveKit server

Open-source WebRTC **SFU** (Selective Forwarding Unit) written in Go. Each participant uploads its media once; the server forwards it to the other participants (no peer-to-peer mesh).

| Concept | Meaning |
|---|---|
| Room | A session participants join |
| Participant | Anything connected to a room: browser user, phone bridge, or bot/agent |
| Track | One published media stream (mic, camera) |
| Access token | JWT signed with the server's API key/secret; grants a participant permission to join a specific room |
| Server API | HTTP/Twirp APIs (same key/secret): RoomService (create/delete/list rooms, participants), AgentDispatchService (dispatch agents) |

Operational facts:
- Signaling: HTTP/WebSocket on **7880**.
- Media: WebRTC over **UDP** (port range or single mux port, e.g. **7882**); ICE/TCP fallback on **7881**.
- Server must advertise an IP clients can reach. Media doesn't go through a normal Ingress, so production setups usually use `hostNetwork`.
- Single node works standalone; multi-node coordinates through **Redis**.
- Tooling: official Helm chart (`livekit-server`), `--dev` mode with built-in key/secret, `lk` CLI.
- Rooms support `empty_timeout` / `departure_timeout` for automatic cleanup; server can send **webhooks** (e.g. `room_finished`, `participant_left`).

## LiveKit Agents

Framework (Python or Node) for AI participants that join rooms like humans. Typical voice loop:

```
user audio → VAD (Silero) → STT → LLM → TTS → audio back into room
```

Alternatively, one realtime speech-to-speech model replaces STT/LLM/TTS. Provider plugins exist for OpenAI, Anthropic, Deepgram, Cartesia, ElevenLabs, etc.

### Worker / job model (central to orchestration)

1. Agent code runs as a long-lived **worker** process (our container).
2. Worker opens a WebSocket to LiveKit, registers under an `agent_name`, and continuously reports **load**.
3. When an agent is needed, the server sends a **job** to an available worker below its load threshold.
4. Worker starts a **subprocess per job**; it joins the room and runs the pipeline. One pod can serve several sessions.
5. Room closes / agent leaves → job ends, subprocess exits.

Dispatch modes:
- **Automatic:** every new room gets an agent.
- **Explicit:** agent sent to a specific room via `AgentDispatchService` (used when `agent_name` is set). This is what "dispatch an agent to a room" maps to.

LiveKit already handles job → worker matching; this project wraps it in a session-level API and runs it on Kubernetes.

## Architecture sketch

```
                    ┌───────────────────────── kind cluster ─────────────────────────┐
  client/curl ──►   │  Orchestrator API (ours)                                       │
  POST /sessions    │   • create room ───────────┐                                   │
  GET  /sessions/id │   • dispatch agent ────────┤                                   │
  DELETE /sessions  │   • mint user token        ▼                                   │
                    │                     LiveKit server ◄── (Redis if multi-node)   │
  browser client ◄══╪══ WebRTC audio ═══► (rooms, media)                             │
                    │                            ▲ jobs via WebSocket                │
                    │              Agent worker Deployment (N replicas, HPA)         │
                    │              each pod: worker → subprocess per session ────────┼──► STT/LLM/TTS APIs
                    └────────────────────────────────────────────────────────────────┘
```

## Requirement → implementation mapping

| Requirement | Implementation idea |
|---|---|
| Self-hosted LiveKit | `livekit-server` in-cluster (Helm chart or own manifests); UDP reachable via kind port mappings |
| Containerized workers | Docker image of an Agents worker, run as a Deployment |
| Start session | Create room → create dispatch → return join token (+ URL) |
| Stop session | Delete room → agent job ends |
| Status | Room, participants, dispatch state from LiveKit APIs |
| Concurrent sessions | Many rooms, each with its own job, spread across worker pods |
| Worker scaling | Manual replicas or HPA. CPU is a weak proxy; active jobs / worker load is better (KEDA or custom metrics as stretch goal). Explain trade-off. |
| Cleanup | Delete room on stop; room empty/departure timeouts; optionally consume `room_finished` webhooks to sync orchestrator state. Workers drain on SIGTERM; long `terminationGracePeriodSeconds` so scale-down doesn't cut live calls. |
| Health/logs/manifests | Liveness/readiness probes on every component; structured logs; Helm chart or manifests |
| Demo | Browser client (Agents Playground or minimal page) joins with returned token and talks to agent; show concurrent sessions, scaling, and cleanup |

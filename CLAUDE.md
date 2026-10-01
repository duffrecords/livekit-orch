# livekit-orch

Local orchestration layer for LiveKit voice agents, running on a local Kubernetes cluster.

## Requirements (source of truth)

- Run a **self-hosted LiveKit server** and **containerized voice-agent workers** on local Kubernetes (kind, k3d, or Minikube).
- Expose an **API** to: start sessions, stop sessions, dispatch an agent to a room, report session status.
- Support **concurrent sessions**, **worker scaling**, and **cleanup when sessions end**.
- Include **health checks**, **basic logs**, and **Kubernetes manifests or Helm charts**.
- Deliverables: Git repo with **setup instructions**, a **brief architecture explanation**, and a **working demo**.
- External speech/LLM APIs are allowed; **dependencies and trade-offs must be clearly documented**.

## Decisions so far

- **Cluster: kind.** Declarative `kind-config.yaml` with UDP `extraPortMappings`; consistent across macOS/Windows/Linux. Rationale and k3d/Minikube comparison: `docs/notes/local-networking.md`.
- **LiveKit media:** single UDP mux port 7882 + TCP 7881 fallback + 7880 signaling. LiveKit pod uses `hostNetwork` so the port is identical at every hop (no NodePort remapping). Advertise the host's LAN address (detected by `make deploy`, passed as `NODE_IP`, never committed; `127.0.0.1` breaks Firefox) plus the node IP (`rtc.advertise_internal_ip: true`) so in-cluster agents can reach it. Kind ports are published on all host interfaces. Also required: `rtc.interfaces.includes: [eth0]` and `enableServiceLinks: false`. Verified by the networking spike; see "Spike results" in `docs/notes/local-networking.md`.
- **Agents: Python** (`livekit-agents` 1.8, `AgentServer` + `@server.rtc_session(agent_name=...)`), connecting to LiveKit through the in-cluster Service. The browser uses `ws://localhost:7880`.
- **Voice pipeline: external APIs, not in-cluster local models.** Provider selected via config/env. Default to a single provider to minimize keys. Details: `docs/notes/voice-pipeline-providers.md`.
- **Orchestrator API: Python/FastAPI in `orchestrator/`**, one replica, at `http://localhost:8080` (loopback only, no auth). One session = one LiveKit room; LiveKit is the source of truth and nothing is persisted. Start is a single `CreateRoom` with `agents=[…]`; stop is `DeleteRoom`; status comes from the participant list (dispatch job state is unreliable). Cleanup by room timeouts, a max-age reaper, and `room_finished` webhooks. Endpoints, statuses and findings: `docs/notes/orchestrator.md`.
- **Worker scaling: job-count capacity, KEDA on session demand, re-dispatch, drain.** Each worker pod takes `MAX_JOBS` (5) sessions via a job-count `load_fnc`. A KEDA `ScaledObject` (metrics-api trigger on the orchestrator's `GET /scaling/{agent}`) keeps about 3 sessions per pod, 1 to 6 pods. LiveKit drops a dispatch no worker can take, so the orchestrator re-dispatches sessions left `pending`. Pods drain on SIGTERM under a 1860 s grace period. `GET /workers` reads each pod through a headless Service. Findings and measurements: `docs/notes/scaling.md`.
- **Keyless mock agent** (echo/tone) as a second `agent_name`, so orchestration, load tests, and CI work without API keys.
- Host audio never enters the cluster directly: a browser client (or LiveKit Agents Playground) captures the mic and sends WebRTC to LiveKit.

## Background notes (read when relevant)

- `docs/notes/livekit-primer.md` — LiveKit server & Agents concepts, worker/job/dispatch model, how requirements map to LiveKit features, architecture sketch.
- `docs/notes/local-networking.md` — WebRTC-in-local-k8s constraints, cluster tool comparison, risks.
- `docs/notes/voice-pipeline-providers.md` — external vs. local models, provider options, secrets handling.
- `docs/notes/scaling.md` — worker capacity, autoscaling, re-dispatch, drain, per-session cost, dependencies and trade-offs.
- `docs/notes/orchestrator.md` — orchestrator API endpoints, session statuses, cleanup paths, LiveKit behaviours it works around, what was verified.

## Conventions

- Secrets (API keys, LiveKit API key/secret) live in a git-ignored `.env` → Kubernetes Secret; Helm charts reference an existing Secret. Never put keys in `values.yaml` or commits.
- LiveKit Agents APIs have changed across versions — verify against current docs before writing worker code.
- The networking spike is done (Chrome, Firefox, Safari and an in-cluster echo agent exchange audio; Chrome and Firefox verified over both UDP and TCP). `make up` / `make down` manage the cluster; `make env` generates local LiveKit keys into `.env`.
- The orchestrator API is done. `make test` runs its unit tests (no cluster needed); `make smoke` runs the end-to-end check against the cluster; `make session` / `make sessions` / `make stop ID=…` drive it by hand. After rebuilding an image under the same tag, `kubectl -n livekit rollout restart deploy/<name>`.
- Worker scaling is done. `make addons` installs metrics-server and KEDA (part of `make up`); `make load` runs the scaling demo; `make workers`, `make scale N=…` and `make top` inspect and adjust it. Worker pods wait for their sessions before exiting, so stop sessions before restarting them. A new agent needs the same `AgentServer` capacity settings as `agents/echo/agent.py`, its own headless Service and `ScaledObject`, and an entry in the orchestrator's `WORKER_SERVICES`. Next: the provider-backed voice agent, then Helm and the README.

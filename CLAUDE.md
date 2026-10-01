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
- **Keyless mock agent** (echo/tone) as a second `agent_name`, so orchestration, load tests, and CI work without API keys.
- Host audio never enters the cluster directly: a browser client (or LiveKit Agents Playground) captures the mic and sends WebRTC to LiveKit.

## Background notes (read when relevant)

- `docs/notes/livekit-primer.md` — LiveKit server & Agents concepts, worker/job/dispatch model, how requirements map to LiveKit features, architecture sketch.
- `docs/notes/local-networking.md` — WebRTC-in-local-k8s constraints, cluster tool comparison, risks.
- `docs/notes/voice-pipeline-providers.md` — external vs. local models, provider options, secrets handling.

## Conventions

- Secrets (API keys, LiveKit API key/secret) live in a git-ignored `.env` → Kubernetes Secret; Helm charts reference an existing Secret. Never put keys in `values.yaml` or commits.
- LiveKit Agents APIs have changed across versions — verify against current docs before writing worker code.
- The networking spike is done (Chrome, Firefox and an in-cluster echo agent exchange audio over UDP and TCP). `make up` / `make down` manage the cluster; `make env` generates local LiveKit keys into `.env`. Next is the orchestrator API.

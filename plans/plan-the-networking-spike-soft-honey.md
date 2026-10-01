# Networking spike: kind + LiveKit + browser + in-cluster agent

> **Status: done.** Kept as the original plan; the result differs in places. LiveKit
> advertises the host's LAN address rather than `127.0.0.1`, ports are published on all
> interfaces, and LiveKit keys are generated locally instead of using the `--dev` pair.
> See "Spike results" in `docs/notes/local-networking.md` for what was built and found.

## Context

The repo has only notes; nothing is built. `CLAUDE.md` makes a networking spike the first
milestone, because WebRTC media through a local cluster is the riskiest part of the project.
The spike as originally scoped only tested a browser on the host. That misses the second
media client: agent worker pods are WebRTC clients too, and they sit on the other side of
the network from the browser.

Reading LiveKit's source (`mediatransportutil/pkg/rtcconfig/webrtc_config.go`) confirmed the
concern. With an explicit `rtc.node_ip: 127.0.0.1`, LiveKit *replaces* every host ICE
candidate with `127.0.0.1`, which inside an agent pod is the pod itself. The designed fix is
`rtc.advertise_internal_ip: true`, which keeps the node's real IP alongside the mapped one.
The spike exists to prove that both clients get working media at the same time.

**Outcome:** a browser on the host and an echo agent in a pod exchange audio in one room,
with the selected ICE path for each confirmed, and the findings written back into the notes.

## Preconditions (found on this machine)

- Docker daemon is **not running**: start Docker Desktop first.
- `lk` CLI is **not installed**: `brew install livekit-cli` (used for tokens, dispatch, room inspection).
- Present: kind 0.31, kubectl 1.36, helm 4.2, uv 0.12, Python 3.14. Ports 7880–7882 are free.

## Design

| Piece | Choice | Why |
|---|---|---|
| Port mappings | `7880/TCP`, `7881/TCP`, `7882/UDP`, each `listenAddress: 127.0.0.1` | Same number at every hop; loopback-only so dev keys aren't exposed on the LAN |
| LiveKit pod | `hostNetwork: true`, `dnsPolicy: ClusterFirstWithHostNet` | Binds directly on the kind node; no NodePort or CNI portmap in the path |
| LiveKit image | `livekit/livekit-server:v1.13.7` (pinned, current latest) | Reproducible |
| LiveKit args | `--dev --bind 0.0.0.0 --config /etc/livekit/livekit.yaml` | `--dev` alone binds signaling to 127.0.0.1, unreachable from other pods |
| LiveKit config | `port: 7880`; `rtc: {tcp_port: 7881, udp_port: 7882, use_external_ip: false, node_ip: 127.0.0.1, advertise_internal_ip: true}` | Browser uses `127.0.0.1`; agent uses the node's internal IP |
| Agent signaling | ClusterIP Service `livekit:7880`; agent `LIVEKIT_URL=ws://livekit.livekit.svc.cluster.local:7880` | Agents never go through the host mapping |
| Agent | Python, `livekit-agents~=1.8`, `AgentServer` + `@server.rtc_session(agent_name="echo")`, run with `start` | Explicit dispatch by name, as the orchestrator will do later; keyless |
| Agent behaviour | Subscribe to the user's audio track, republish frames on its own track, log frame counts | Proves agent media in **both** directions |
| Agent probes | readiness + liveness `GET /` on `8081` | Built-in worker health server |
| Browser client | Static page using `livekit-client` from jsdelivr, served on `http://localhost:8000` | Mic works on localhost; no dependency on a hosted https page connecting to `ws://` |
| Packaging | Raw manifests under `deploy/`; Helm comes later | Spike should stay small |

The files go in their long-term locations so the spike is the first layer of the real
project, not throwaway code.

## Files to create

- `kind-config.yaml` — one control-plane node with the three `extraPortMappings`.
- `deploy/namespace.yaml` — namespace `livekit`.
- `deploy/livekit.yaml` — ConfigMap (config above), Deployment (1 replica, hostNetwork, TCP readiness on 7880), ClusterIP Service.
- `deploy/agent-echo.yaml` — Deployment (1 replica, `imagePullPolicy: Never`, env from Secret `livekit-keys`, probes on 8081).
- `agents/echo/agent.py`, `agents/echo/pyproject.toml`, `agents/echo/Dockerfile` (`python:3.13-slim` + uv; `CMD ["python","agent.py","start"]`).
- `web/index.html` — fields for URL and token, connect button, mic publish, remote audio playback, and a panel showing connection state and the selected ICE candidate pair from `getStats()`.
- `.env.example` — `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` / `LIVEKIT_URL`.
- `Makefile` — `up` (create cluster, build image, `kind load docker-image`, create Secret from `.env`, apply), `token`, `dispatch`, `web`, `logs`, `down`.

**Secrets note:** the only key material is LiveKit's built-in public `--dev` placeholder pair.
It reaches the agent via git-ignored `.env` → Secret `livekit-keys`, following the project
convention. `.env.example` would carry that placeholder pair so `cp .env.example .env` works;
say so if you'd rather it hold empty values.

Before writing `agent.py`, check the echo pattern against the current `livekit-agents` 1.8
docs/examples (`rtc.AudioStream` → `rtc.AudioSource` → `LocalAudioTrack`), per the
project rule on API drift.

## Steps and pass criteria

1. **Cluster + LiveKit.** `make up`. Pass: `curl http://localhost:7880` returns OK; LiveKit log shows the explicit node IP with `advertiseInternalIP: true`.
2. **Browser media.** `make web`, `make token`, join room `spike`. Pass: state `connected`, and the selected candidate pair is **UDP `127.0.0.1:7882`**.
3. **Agent registration.** Pass: agent pod Ready; LiveKit log shows a worker registered as `echo`.
4. **Dispatch + agent media.** `make dispatch` (`lk dispatch create --agent-name echo --room spike`). Pass: `lk room participants list spike` shows the agent; speaking into the mic is heard back; agent log shows received-frame counts rising; LiveKit debug log shows the agent's ICE pair on the node's internal IP, not `127.0.0.1`.
5. **TCP fallback.** Set `rtc.force_tcp: true`, restart LiveKit, rejoin. Pass: candidate pair is TCP `127.0.0.1:7881` and echo still works. Revert afterwards.
6. **Two rooms at once.** Second tab, room `spike2`, second dispatch. Pass: both echo independently through the single UDP port.
7. **Teardown.** `make down`; ports 7880–7882 are free again.

## If step 4 fails

In order: confirm `advertise_internal_ip` is honoured by v1.13.7 (the source read was `master`);
restrict candidates with `rtc.interfaces.includes: [eth0]`; drop `advertise_internal_ip` and
test whether peer-reflexive candidates alone connect; last resort, advertise the host's LAN IP
instead of `127.0.0.1` (works for both clients, but is machine-specific and must stay out of git).

## Wrap-up

- Update `docs/notes/local-networking.md` with the measured results: the `advertise_internal_ip` requirement, `--bind 0.0.0.0`, and observed candidate pairs.
- Update the "Decisions so far" section of `CLAUDE.md` (hostNetwork chosen, in-cluster agent path, Python agents).
- No commits unless you ask. Windows/Linux and VPN behaviour stay untested by this spike.

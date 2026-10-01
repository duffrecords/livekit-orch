# livekit-orch

A local orchestration layer for LiveKit voice agents. A self-hosted LiveKit server, a small
session API and autoscaled agent workers run on a kind cluster; a browser page lets you talk to
an agent. Everything except the spoken conversation works without any API key, using a built-in
`echo` agent that plays your voice back.

## Requirements and where they are met

| Requirement | How | See it |
|---|---|---|
| Self-hosted LiveKit and containerized workers on local Kubernetes | LiveKit server and Python agent workers as Deployments on kind | `make up`, then `kubectl -n livekit get pods` |
| API to start, stop, dispatch and report status | FastAPI orchestrator at `http://localhost:8080` | [API](#api) |
| Concurrent sessions | One LiveKit room and one agent job per session; a worker pod runs up to five | [Demo](#demo), step 3 |
| Worker scaling | KEDA scales workers on the number of sessions that need one | [Demo](#demo), step 4 |
| Cleanup when sessions end | Explicit stop, room timeouts, a maximum age, webhooks; workers drain before exiting | [Demo](#demo), step 5 |
| Health checks, logs, manifests or Helm | Probes on every pod, JSON logs, one Helm chart | [Operations](#operations), [Deployment](#deployment) |
| Dependencies and trade-offs documented | | [Dependencies and trade-offs](#dependencies-and-trade-offs), [Limits](#limits) |

## Prerequisites

- Docker with about 8 GB of memory for its VM
- [kind](https://kind.sigs.k8s.io/), `kubectl`, [Helm](https://helm.sh/)
- `make`, `curl`, `jq`, `openssl`, `python3`
- Internet access: container images, KEDA's chart and the browser client library are downloaded
- Optional: [`uv`](https://docs.astral.sh/uv/) for `make test`; the [`lk` CLI](https://docs.livekit.io/home/cli/) for `make token`, `make dispatch` and `make status`

Tested on macOS (Apple silicon) with Docker Desktop 29.8, kind 0.31, kubectl 1.36 and Helm 4.2.
Ports 7880 to 7882 and 8080 must be free.

## Quick start

```sh
make up       # cluster, add-ons, images, secrets, Helm release
make smoke    # 10 sessions started, joined by an agent, stopped, nothing left behind
make web      # serves the test page; leave it running
```

Open <http://localhost:8000>, press **Start session**, allow the microphone, and speak: the echo
agent plays your voice back about a second later. **Stop session** ends it.

`make up` took two minutes here with the base images already downloaded; allow longer the first
time. It does the following, and each step can be run on its own:

1. `make env`: writes a git-ignored `.env` with random LiveKit API keys (kept if it exists)
2. `make cluster`: creates the kind cluster from `kind-config.yaml`
3. `make addons`: installs metrics-server and KEDA
4. `make image`: builds the orchestrator and agent images and loads them into the cluster
5. `make secret`: turns the keys in `.env` into a Kubernetes Secret
6. `make deploy`: installs or upgrades the Helm release

`make down` deletes the cluster.

## Demo

Each step says what to run and what to expect. Sample output is from a real run, shortened.

**1. One session.** With `make web` running, open <http://localhost:8000> and press **Start
session**. The page shows the session going from `pending` to `active`, the agent joining, and
the audio counters rising; you hear yourself echoed.

**2. Status through the API.** In another terminal:

```console
$ make sessions
$ curl -s localhost:8080/sessions/s-7a9e9d8591 | jq '{id, status, agent, participants: [.participants[].identity]}'
{
  "id": "s-7a9e9d8591",
  "status": "active",
  "agent": "echo",
  "participants": ["agent-AJ_uYZxL5Lkp8Dh", "user-e05436"]
}
```

**3. Concurrent sessions.** Open the page in a second tab or another browser and start a
session there too. Each has its own room and its own agent, and both echo at once:

```console
$ make workers
echo  <pod IP>  jobs=2  load=0.4
```

**4. Scaling.** `make load` starts 12 sessions at once against one worker pod, which holds
five. The rest wait, the autoscaler adds pods, the orchestrator dispatches the waiting sessions
again, and the pods go away after the sessions stop:

```console
$ make load
worker pods before: 1 Running
starting 12 sessions
  +  0s  sessions: 12 pending (waiting)                         pods: 1 Running
  +  4s  sessions: 7 pending (workers_full), 5 ready            pods: 3 ContainerCreating, 1 Running
  +  7s  sessions: 7 pending (waiting), 5 ready                 pods: 4 Running
  + 10s  sessions: 12 ready                                     pods: 4 Running
all 12 sessions ready after 10s
holding for 30s
...
jobs per worker:
  <pod IP>  jobs=5  FULL
  <pod IP>  jobs=3
  <pod IP>  jobs=2
  <pod IP>  jobs=2
stopping sessions
waiting for the workers to scale back down
  + 50s  pods: 4 Running
  + 60s  pods: 1 Running
PASS: 12 sessions ready; workers went 1 -> 4 -> 1
```

Time to all ready was between 10 and 28 seconds in the runs made here; it depends on where the
burst falls in the autoscaler's 15-second cycle.

**5. Cleanup.** Start two sessions from two tabs, then:

- Press **Stop session** in one. The browser is disconnected at once and the agent's job ends.
- Close the other tab. About 10 seconds later that session is `ended` with no API call. (If the
  browser is killed instead, LiveKit takes about 20 seconds to notice first.)

```console
$ curl -s 'localhost:8080/sessions?include=ended' | jq -c '.sessions[] | {id, status, end_reason}'
{"id":"s-78b6a43c9a","status":"ended","end_reason":"stopped"}
{"id":"s-7a9e9d8591","status":"ended","end_reason":"idle_timeout"}
```

A session nobody joins closes after 120 seconds, and any session is ended after 30 minutes.

**6. Health and logs.**

```console
$ curl -s localhost:8080/readyz
{"status":"ok"}
$ kubectl -n livekit get pods,hpa
$ make logs
```

## Voice agent (optional)

The `voice` agent holds a spoken conversation: speech-to-text, a language model, and
text-to-speech, all through external APIs. It is built and deployed only when `.env` holds a
provider key.

```sh
echo 'OPENAI_API_KEY=<your key>' >> .env    # .env is git-ignored
make up
```

On the test page set **Agent** to `voice` and start a session. The agent greets you, answers in
short sentences and can be interrupted; the page shows the transcript.

Each stage is `provider/model`, set in `.env` and applied by `make deploy`:

| Variable | Default | Alternative |
|---|---|---|
| `VOICE_STT` | `openai/gpt-4o-mini-transcribe` | `deepgram/nova-3` (`DEEPGRAM_API_KEY`) |
| `VOICE_LLM` | `openai/gpt-4.1-mini` | `anthropic/claude-haiku-4-5` (`ANTHROPIC_API_KEY`) |
| `VOICE_TTS` | `openai/gpt-4o-mini-tts` | `deepgram/aura-2-andromeda-en` |
| `VOICE_INSTRUCTIONS`, `VOICE_GREETING` | in `agents/voice/agent.py` | free text |

A session can override the instructions and greeting:

```sh
curl -s -X POST localhost:8080/sessions -H 'Content-Type: application/json' \
  -d '{"agent": "voice", "metadata": {"instructions": "You are a pirate."}}'
```

Things to know before a demo:

- Audio and text go to the configured providers, and each conversation costs a few cents at most.
- Nothing is sent until a user joins, so `make smoke AGENT=voice` and `make load AGENT=voice` are free.
- A new OpenAI account with little credit is limited to 10 transcription and speech requests per
  minute, which fails several conversations at once. With credit added, five ran cleanly.
- The default trades speed for needing one key: about 2.5 s from the end of your speech to the
  start of the answer, against about 0.9 s with Deepgram and Claude.

More in [docs/notes/voice-pipeline-providers.md](docs/notes/voice-pipeline-providers.md).

## Architecture

```
                        ┌─────────────────── kind cluster (one node) ───────────────────┐
 curl / make ── HTTP ──►│ orchestrator ──── server API ────► LiveKit server             │
 test page     :8080    │ (FastAPI)    ◄─── webhooks ─────── (rooms, media)             │
                        │    ▲    │                             ▲        ▲              │
                        │    │    │ job count per pod           │ jobs,  ║              │
               sessions │    │    ▼                             │ audio  ║              │
              per agent │  KEDA ──► agent workers ──────────────┘        ║              │
                        │  (HPA)    (echo, voice) ───► STT, LLM, TTS     ║              │
                        │  replicas                    providers,        ║              │
                        │                              voice only        ║              │
                        └────────────────────────────────────────────────║──────────────┘
 browser ◄════════ WebRTC: 7880 signaling, 7882/udp, 7881/tcp ═══════════╝
 (microphone)
```

| Component | Role |
|---|---|
| LiveKit server | Rooms, media forwarding, and assigning agent jobs to workers. Self-hosted, one pod on the host network so its ports reach the host unchanged |
| Orchestrator (`orchestrator/`) | The session API. Stateless in front of LiveKit: a session is a room, and LiveKit is the source of truth |
| Agent workers (`agents/`) | Long-lived processes that register with LiveKit under an agent name and run one subprocess per session. `echo` needs no keys; `voice` calls external APIs |
| KEDA | Reads the orchestrator's count of sessions per agent and sets the number of worker pods through an ordinary HPA |
| Test page (`web/`) | Captures the microphone and joins the room. Host audio never enters the cluster any other way |

**A session's life.** `POST /sessions` creates a room and dispatches an agent in one LiveKit
call, and returns a join token. LiveKit offers the job to a worker with a free slot; its agent
joins (`ready`). A user joins with the token (`active`). The session ends by `DELETE`, by the
user leaving, by a timeout, or at the maximum age; the agent's job ends with the room.

Four decisions shape the rest:

- **Nothing is persisted.** Status is read from LiveKit on every request, so restarting the
  orchestrator loses no live session. Only the list of ended sessions is in memory.
- **Capacity is counted in jobs, not CPU.** A worker takes at most five sessions. An echo
  session uses about 45m of CPU, so CPU would never fill a pod or trigger scaling.
- **Waiting sessions are dispatched again.** LiveKit offers a dispatch to the workers once and
  drops it if none has room. The autoscaler keeps spare slots (three sessions per pod of five),
  and the orchestrator re-dispatches any session still without an agent after five seconds.
- **Workers drain.** A pod being removed stops taking jobs and exits when its last session
  ends. Its grace period is longer than the longest session, so scaling down or rolling out a
  new image never cuts a conversation.

Background and measurements: [LiveKit primer](docs/notes/livekit-primer.md),
[networking](docs/notes/local-networking.md), [orchestrator](docs/notes/orchestrator.md),
[scaling](docs/notes/scaling.md).

## API

Base URL `http://localhost:8080`, no authentication, reachable from the host only. Interactive
documentation is at `/docs`.

| Method and path | Purpose |
|---|---|
| `POST /sessions` | Start a session. Body, all optional: `agent` (default `echo`; `null` for none), `identity`, `metadata`. Returns the session with `url`, `token`, `identity` |
| `GET /sessions` | Live sessions; `?include=ended` adds recently ended ones |
| `GET /sessions/{id}` | Status, participants, dispatches |
| `DELETE /sessions/{id}` | Stop a session. 204, also if it has already ended |
| `POST /sessions/{id}/dispatches` | Dispatch another agent to the room: `{"agent": "echo"}` |
| `POST /sessions/{id}/tokens` | A join token for another participant: `{"identity": "user2"}` |
| `GET /workers` | Worker pods with their job count and load |
| `GET /scaling/{agent}` | Sessions that need a worker for that agent (read by the autoscaler) |
| `GET /healthz`, `GET /readyz` | Liveness, and readiness (LiveKit answers) |

| Status | Meaning |
|---|---|
| `pending` | No agent in the room yet. `pending_reason`: `no_workers`, `workers_full` or `waiting` |
| `ready` | Agent in the room, no user |
| `active` | A user and the agent are in the room |
| `closing` | Everyone has left; the room is about to close |
| `ended` | Room gone. `end_reason`: `stopped`, `expired`, or LiveKit's reason such as `idle_timeout` |

```sh
curl -s -X POST localhost:8080/sessions -H 'Content-Type: application/json' -d '{"agent": "echo"}'
curl -s localhost:8080/sessions/<id>
curl -s -X DELETE localhost:8080/sessions/<id>
```

`make session`, `make sessions` and `make stop ID=<id>` wrap these. The response to a start
usually says `pending`; the agent joins within a second.

## Operations

**Health checks**

| Pod | Readiness | Liveness |
|---|---|---|
| LiveKit | HTTP `/` on 7880 | same |
| Orchestrator | `/readyz`: the LiveKit API answers within 2 s | `/healthz`: process up |
| Workers | HTTP `/` on 8081: worker process up | same |

A worker's readiness does not mean it has registered with LiveKit or has free capacity;
`GET /workers` shows capacity.

**Logs.** `make logs` (add `AGENT=voice` for the voice workers). Every component writes to
stdout; the orchestrator and the workers write JSON lines:

```json
{"message":"session started","level":"INFO","session_id":"s-aa0b1d5330","agent":"echo","timestamp":"…"}
{"message":"request","level":"INFO","method":"POST","path":"/sessions","status":201,"duration_ms":9.4,"session_id":"s-aa0b1d5330","timestamp":"…"}
```

The orchestrator also logs each agent dispatch and re-dispatch, each session end with its
reason, and every LiveKit webhook event. The voice agent logs each turn with its delays and, at
the end, usage per provider; what was said is logged at debug level only.

**Cleanup paths**

| Trigger | What happens |
|---|---|
| `DELETE /sessions/{id}` | Room deleted; users disconnected and the agent's job ends at once |
| Nobody joins | Room closes after 120 s |
| Last user leaves | Room closes 10 s later |
| Session older than 30 minutes | Deleted by the orchestrator, occupied or not |
| Worker pod removed | Finishes its sessions first |

**Commands**

| Command | Purpose |
|---|---|
| `make up`, `make down` | Create everything, delete the cluster |
| `make deploy` | Apply changes to `.env`, the chart, or rebuilt images |
| `make smoke`, `make load` | End-to-end check, scaling demo (`AGENT=voice`, `COUNT=…`) |
| `make test` | Unit tests and chart lint; no cluster needed |
| `make session`, `make sessions`, `make stop ID=…` | Drive the API |
| `make workers`, `make top` | Jobs per worker pod; CPU and memory per pod |
| `make scale N=3` | Hold at least N worker pods |
| `make web`, `make logs`, `make manifests` | Test page, logs, rendered Kubernetes manifests |

## Deployment

One Helm chart, [`charts/livekit-orch`](charts/livekit-orch), installed as one release in the
`livekit` namespace. `make manifests` prints the plain manifests it renders.

- **Agents are a map in `values.yaml`.** Each entry under `agents:` gets a worker Deployment, a
  headless Service and a KEDA `ScaledObject`, and is added to the agents the API accepts. The
  key is the name the worker registers under.
- **Secrets stay out of the chart.** LiveKit keys and provider keys go from `.env` into
  Kubernetes Secrets, which the chart references by name. `helm get values` shows no key.
- **`make deploy` passes what only the machine knows:** the address LiveKit advertises (the
  host's LAN address), the local image IDs, and whether the voice agent is enabled.
- **After a code change:** `make image deploy`. Pods whose image changed are rolled; workers
  finish their sessions first.
- **Extra values:** `make deploy HELM_ARGS="-f my-values.yaml"`.

Details: [docs/notes/deployment.md](docs/notes/deployment.md).

## Dependencies and trade-offs

| Choice | Cost | Why |
|---|---|---|
| External speech and LLM APIs, no local models | API keys; audio and text leave the machine; per-use cost; provider rate limits cap concurrency before the cluster does | No GPU in containers on macOS or Windows; local models would make turns take seconds and turn the scaling demo into a model benchmark |
| OpenAI for all three stages by default | About 2.5 s per turn against 0.9 s with Deepgram and Claude | One key to obtain |
| kind, LiveKit on the host network, LAN address advertised | Ports 7880 to 7882 are reachable from the local network; `make deploy` must be re-run after changing networks | The same port at every hop is what WebRTC needs, and Firefox cannot use a loopback address |
| LiveKit as the only state | Nothing survives a LiveKit restart; ended-session history is in memory and limits the orchestrator to one replica | No database to run, and no state to drift from LiveKit's |
| Capacity by job count | Ignores real CPU pressure, so the per-pod limit has to be measured for each agent | Deterministic, and the only signal that moves for a light agent |
| KEDA (three pods) and metrics-server | More to install and download | Scales on sessions, including those still waiting, and produces a standard HPA. metrics-server is only for `kubectl top` |
| Spare capacity (pods about 60 % full) | Idle pods: 12 sessions hold 4 pods | LiveKit drops a dispatch no worker can take |
| Re-dispatch | A burst beyond the spare slots waits 10 to 30 s for its agents | Sessions are never lost to a missed dispatch |
| Draining scale-down | A removed pod can stay `Terminating` for up to 30 minutes | No conversation is cut off |
| Unjoined sessions count as demand | Abandoned sessions hold a slot for 120 s | They do hold a job |
| One node | Scales pods, not nodes; both agent types under load together exceed the 8 GB VM | A laptop demo |
| No authentication, loopback only | Not usable beyond the host | The API mints room tokens; keeping it off the network is the simplest safe default |
| A chart for this cluster, own LiveKit templates | Side-loaded images, host network and a NodePort; not a production chart and does not track LiveKit's official one | The settings the networking tests found necessary are kept exactly |

## Limits

Not done:

- API authentication, and more than one orchestrator replica.
- Multi-node LiveKit (needs Redis) and node autoscaling.
- An agent name listed in the chart but without workers is accepted and waits as
  `pending (no_workers)` rather than being refused.
- A voice agent that has gone silent after repeated provider errors still reads `active`; the
  error is only in the worker log.

Not verified:

- Windows and Linux hosts, a second device on the LAN, VPNs and firewalls.
- Helm 3 (tested with 4.2).
- Audio heard in a browser while its worker pod was draining. Agent presence was checked
  through the API.
- A cold `make up` with no cached images was not timed.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Firefox cannot connect, or nothing connects after changing networks | LiveKit advertises the host's LAN address, detected at deploy time (loopback if there was no network, which Firefox cannot use). Run `make deploy`; override with `make deploy NODE_IP=<address>` |
| No media on a VPN or restrictive network | Not tested. UDP 7882 may be blocked; LiveKit also offers TCP 7881, and disconnecting the VPN is the first thing to try |
| A worker pod stays `Terminating` | It is finishing its sessions. Stop them (`make sessions`, `make stop ID=…`), or wait |
| A session stays `pending (workers_full)` | All pods are full and the maximum (6 for echo, 3 for voice) is reached, or new pods are still starting |
| A voice session stays `pending (no_workers)` | No provider key in `.env`, so the voice agent is not deployed |
| Voice turns fail; the worker log shows 429 | The provider's rate limit; see [Voice agent](#voice-agent-optional) |
| `curl: (52) Empty reply` just after `make deploy` | The orchestrator pod was being replaced. Retry after a few seconds |
| Worker pods stay `Pending` during `make load` | The Docker VM is short of memory: six echo pods need about 3 GB and three busy voice pods about 2.7 GB. Give it 8 GB and load one agent type at a time |

## Repository layout

```
agents/echo/, agents/voice/   agent workers (Python, livekit-agents)
orchestrator/                 session API (Python, FastAPI) and its tests
charts/livekit-orch/          Helm chart for LiveKit, the orchestrator and the workers
deploy/addons/                metrics-server kustomization
kind-config.yaml              cluster definition with the port mappings
scripts/                      smoke.sh, load.sh
web/index.html                browser test client
docs/notes/                   design notes: findings, measurements, what was verified
plans/                        the plan written before each milestone
```

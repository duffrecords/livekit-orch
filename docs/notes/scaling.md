# Worker scaling

How worker pods get a fixed capacity, how their number follows demand, and what keeps a
session alive through a scale-down. Manifests: `deploy/agent-echo.yaml`,
`deploy/agent-echo-scaling.yaml`, `deploy/addons/`. Original plan: `plans/worker-scaling.md`.

## Model

- **Capacity is counted in jobs.** A worker pod runs at most `MAX_JOBS` sessions (5). The
  agent sets `load_fnc = active_jobs / MAX_JOBS`, `load_threshold = 1.0` and
  `num_idle_processes = MAX_JOBS` on its `AgentServer`. The framework default (CPU use against
  a 0.7 threshold) never fills a pod here: five echo sessions with audio use 0.3 of 16 CPUs.
- **Demand is counted by the orchestrator.** `GET /scaling/{agent}` returns the live sessions
  that need a worker for that agent, including the ones still waiting for one.
- **KEDA scales the worker Deployment on that number.** The `ScaledObject` aims for 3 sessions
  per pod, between 1 and 6 pods: `replicas = ceil(sessions / 3)`. KEDA generates an ordinary
  HPA (`kubectl -n livekit get hpa`). Pods therefore run about 60 % full, which leaves spare
  slots for the next sessions.
- **The orchestrator re-dispatches sessions that were not picked up.** Every 5 s, a session
  that has been `pending` for more than `REDISPATCH_AFTER_SECONDS` (5) has its dispatch
  deleted and created again, once a worker has a free slot.
- **A pod being removed finishes its sessions.** On SIGTERM the worker stops taking jobs and
  exits when the last one ends. `terminationGracePeriodSeconds` is 1860, above the 1800 s
  session limit the orchestrator enforces.

## Commands

| Command | Purpose |
|---|---|
| `make addons` | Install metrics-server and KEDA (part of `make up`) |
| `make load` | Start `COUNT` (12) sessions at once, show them being picked up as pods are added, stop them after `HOLD` (30) seconds, show the pods going away |
| `make workers` | Jobs and load per worker pod, and the pod list |
| `make scale N=3` | Hold at least N worker pods (sets the autoscaler's minimum; `make deploy` resets it to 1) |
| `make top` | CPU and memory per pod |

`kubectl scale deploy/agent-echo` does not stick: the HPA overrides it within seconds.

## What LiveKit and the worker do (findings that shaped this)

- **LiveKit offers a dispatch to the workers once.** With no worker registered, or with every
  worker full, the dispatch is dropped. A worker that registers or frees a slot later is not
  offered it: sessions stayed `pending` for as long as they were watched (80 s and 30 s).
  This is why there is headroom and a re-dispatch, and why scaling on demand alone is not
  enough.
- A dispatch's default `restart_policy` did not bring back an agent whose pod was killed. The
  re-dispatch does, because such a session reads `pending`.
- LiveKit keeps a force-killed agent in the participant list for about 24 s. Only then does
  the session read `pending`.
- With `num_idle_processes = MAX_JOBS`, a burst is counted correctly: 12 sessions started at
  once against one empty pod gave exactly 5 accepted and 7 refused.
- An idle worker pod uses about 490 MB whatever the number of warm processes: the framework's
  fork server preloads about 330 MB and the warm processes share it.
- The worker's `GET :8081/worker` reports `active_jobs` and `worker_load`. `GET :8081/` stays
  200 when the worker is full or draining, so readiness says nothing about capacity.
- A draining pod is `Terminating`, drops out of the headless Service, and is not listed by
  `/workers`, although it still runs jobs.
- KEDA removes its HPA while a `ScaledObject` is paused and recreates it afterwards.

## Verified (2026-09-30, macOS, kind, one node with 16 CPUs and 8 GB)

| Check | Result |
|---|---|
| Capacity | One pod took 5 sessions, logged `worker is at full capacity`, and refused the sixth |
| `make load` (12 sessions, 1 pod) | 5 `ready` at once, 7 `pending (workers_full)`; 4 pods at 15 s; all 12 `ready` at 18 s; back to 1 pod 61 s after the sessions were stopped |
| Manual scale 1 → 3 with 7 sessions waiting | All 12 `ready` 6 s after the scale command; 7 re-dispatches logged; one agent per session |
| Scale 3 → 1 with 12 sessions live | Two pods stayed `Terminating` with every session keeping its agent, and exited when their sessions ended (about 90 s later) |
| `rollout restart` with 4 sessions live | Every session kept its agent; the old pod stayed until they were stopped |
| Zero workers, then one | Session read `pending (no_workers)`, and was `ready` 4 s after the autoscaler was unpaused |
| Force-killed pod with a user connected | Agent gone from the room at 24 s; a new agent in the room at 29 s |
| `make smoke` (10 sessions) | Passes against the autoscaled cluster |
| `make test` | 33 unit tests |

Cost of a session, from the pod's cgroup counters over 30 s with five sessions each receiving
and echoing an Opus stream (`lk room join --publish`):

| | CPU | Memory |
|---|---|---|
| Idle pod | about 70m | about 490 MB |
| Pod with 5 echo sessions | about 300m | about 570 MB |
| Per echo session | about 45m | about 15 MB |

Requests are set from this: `cpu: 300m`, `memory: 640Mi`, memory limit `1Gi`, no CPU limit.

Not verified: audio heard in a browser during a scale-down. Agent presence was checked through
the API, and audio through the agent's frame counters with an `lk` publisher.

## Dependencies and trade-offs

- **KEDA** (v2.21.0, three pods in the `keda` namespace) and **metrics-server** (v0.9.0, with
  `--kubelet-insecure-tls` because kind's kubelet certificates are self-signed). Both are
  pulled from the internet by `make addons`. metrics-server is only for `kubectl top`; the
  scaling path does not use it.
- **Job count, not CPU.** Deterministic and right for the echo agent. It ignores real CPU
  pressure, so `MAX_JOBS` has to be sized by measurement for each agent. The provider-backed
  agent may want a CPU trigger next to the session trigger.
- **Reaction time.** The HPA evaluates every 15 s, so a burst beyond the spare slots waits up
  to about 15 s for pods, then up to 5 s for the re-dispatch. Pod start is about 3 s with the
  image already on the node.
- **Headroom costs idle pods.** At 60 % target, 12 sessions hold 4 pods (20 slots).
- **Scale-down is slow by design.** A removed pod can stay `Terminating` for up to 30 minutes
  while its sessions run. `rollout restart deploy/agent-echo` behaves the same way; in
  development, stop the sessions first or delete the pod with `--grace-period=0 --force`.
- **Sessions nobody joined count as demand** until their room's `empty_timeout` (120 s).
- **One node.** This scales pods, not nodes. Six worker pods need about 3 GB of the 8 GB
  Docker VM.
- **`full` in `/workers` is a convention.** It means `worker_load >= 1.0`, which holds for
  workers using the job-count load function above.

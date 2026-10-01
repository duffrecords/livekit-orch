# Worker scaling

> **Status: done.** Kept as the original plan; KEDA was confirmed. Differences in the result:
> an idle worker pod uses about 490 MB, not under 300 MB (the framework's fork server, not the
> warm processes), so requests are `300m` / `640Mi`; re-dispatch also replaces an agent that
> died while a user was connected; it only fires once a worker has a free slot; the
> ScaledObject has no `pollingInterval` (KEDA ignores it with a minimum of 1); metrics-server
> is installed through a kustomization. Audio in a browser during a scale-down was not checked.
> What was built and measured: `docs/notes/scaling.md`.

## Context

The orchestrator API is done: one `agent-echo` pod serves every session, with no resource
requests, no defined capacity, and the default 30 s termination grace period. This milestone
covers the "worker scaling" requirement and the worker side of "concurrent sessions" and
"cleanup": a defined capacity per worker pod, more pods when demand rises, fewer when it
falls, and no live session cut off by a scale-down. The provider-backed voice agent, Helm and
the README are later milestones.

**Outcome:** `make load COUNT=12` starts 12 sessions against a cluster running one worker
pod. Every session reaches `ready`, the worker Deployment grows to three or more pods without
anyone touching it, `make workers` shows the jobs spread over them, and after the sessions end
it shrinks back to one pod. A session that is on a pod chosen for removal keeps running until
it ends.

## What LiveKit and the worker do (probed 2026-09-30)

Read from the `livekit-agents` 1.8.3 source in the running pod and checked against the
cluster (server v1.13.7).

| Probe | Result | Consequence |
|---|---|---|
| Default load function | Average CPU use over 2.5 s as a fraction of the pod's CPU count (cgroup quota, else the node's 16). The worker reports `WS_FULL` and refuses jobs at `load_threshold` 0.7 | With no CPU limit an echo job is a rounding error of 16 CPUs: one pod never fills and nothing would ever scale. Capacity has to be defined by job count |
| `AgentServer(load_fnc=…, load_threshold=…, num_idle_processes=…, drain_timeout=…)` | All four are constructor arguments; `load_fnc` receives the server, which exposes `active_jobs` | Capacity can be set in `agent.py` from env, no framework patching |
| Availability check | `load + reserved × estimate < threshold`. With no jobs running, the estimate per reserved job is `threshold / num_idle_processes` | Setting `num_idle_processes` equal to the job limit makes the estimate exactly one slot, so a burst onto an empty pod is counted correctly |
| Warm processes | Default is one per CPU: 16 here, about 630 MB for an idle pod | Set it explicitly; it is also what makes six pods fit in the 8 GB Docker VM |
| `GET :8081/worker` | JSON with `agent_name`, `worker_load`, `active_jobs` (omitted when 0) | Per-pod job counts are available without Prometheus |
| `GET :8081/` | 200 unless the connection to LiveKit has failed. Stays 200 when full or draining | Readiness says nothing about capacity. That is fine: workers dial out, no Service routes to them |
| **Session started with zero workers, worker added afterwards** | The new pod was Ready in 3 s. The session stayed `pending` for the 80 s it was watched; the dispatch was never assigned | **LiveKit does not hand a waiting dispatch to a worker that registers later.** Scaling up after the fact does not rescue a session. Needs headroom, and a re-dispatch for sessions that still miss |
| SIGTERM with a job running | Worker logs `draining worker`, `timeout: 3600`, stops taking jobs and keeps the running one. Kubernetes killed it at 30 s | Scale-down and rollouts cut live sessions today. Grace period must cover the longest session |
| After that SIGKILL | The session still showed the agent as a participant 12 s after the pod was gone | LiveKit notices a dead agent late. How late, and what the session reads afterwards, is measured in step 2 |
| Prometheus | `prometheus_port` exposes running-job and load gauges | Available later; not needed for this milestone |

Not yet probed (first tasks of steps 2 and 3): what LiveKit does with a dispatch when workers
are registered but all full, as opposed to none registered; CPU and memory per echo job with
real audio flowing.

## Design

| Piece | Choice | Why |
|---|---|---|
| Capacity per pod | `MAX_JOBS` env (default 5). `load_fnc = active_jobs / MAX_JOBS`, `load_threshold = 1.0`, `num_idle_processes = MAX_JOBS` | Deterministic and the same for the keyless agent and the provider-backed one. Trade-off: it ignores real CPU pressure, so `MAX_JOBS` has to be sized from measurement (step 3). 5 is deliberately small so a laptop demo needs several pods |
| Resources | Requests `cpu: 250m`, `memory: 384Mi`; memory limit `1Gi`; no CPU limit. Adjusted from the step 3 measurement | Requests make scheduling and `kubectl top` meaningful. A CPU limit would throttle audio processing and buys nothing once load is counted in jobs |
| Scaling signal | Demand: live sessions expecting that agent, counted by the orchestrator from LiveKit rooms. New `GET /scaling/{agent}` → `{"agent": "echo", "sessions": 7, "pending": 1}` | It includes sessions waiting for a worker, which per-pod job counts cannot see. The orchestrator already has the number |
| Autoscaler | KEDA `ScaledObject` on `agent-echo` with a `metrics-api` trigger reading that endpoint. Target 3 sessions per pod (60 % of `MAX_JOBS`), min 1, max 6, scale-up immediate, scale-down stabilization 60 s for the demo | KEDA generates a standard HPA, so `kubectl get hpa` shows it; no Prometheus or adapter. The 60 % target is the headroom the probe showed is needed |
| metrics-server | Installed with `--kubelet-insecure-tls` (kind's kubelet certs are self-signed) | Needed to measure a job's CPU and memory in step 3 and for `kubectl top` in the demo. Not part of the scaling path |
| Sessions that still miss | Orchestrator loop every 5 s: a session `pending` for more than `REDISPATCH_AFTER_SECONDS` (5) has its dispatch deleted and created again. Bounded by the room's `empty_timeout` (120 s) | A burst larger than the headroom would otherwise leave sessions stuck forever. Chosen over refusing new sessions with a 503, which would need the orchestrator to track free slots and would fail bursts the cluster can absorb a few seconds later |
| Scale-down and rollouts | `terminationGracePeriodSeconds: 1860` (`SESSION_MAX_SECONDS` + 60), `drain_timeout` from `DRAIN_TIMEOUT_SECONDS` (1800), rolling update `maxUnavailable: 0`, `maxSurge: 1` | A pod picked for removal stops taking jobs and exits as soon as its last session ends; the reaper bounds that at 30 minutes. Trade-off: a terminating pod can linger that long, and `rollout restart deploy/agent-echo` no longer completes at once when sessions are live |
| Worker visibility | Headless Service `agent-echo`; orchestrator `GET /workers` resolves it and reads each pod's `/worker` → pod IP, agent, jobs, load, capacity | Shows the spread in the demo, and lets `pending` say whether no worker is registered or all are full (an open point in `docs/notes/orchestrator.md`). DNS only, so no Kubernetes API access or RBAC for the orchestrator |
| Manual scaling | `make scale N=3` patches the ScaledObject's min replicas | With an autoscaler attached, `kubectl scale` is overwritten within seconds |
| Manifests | `replicas` removed from `deploy/agent-echo.yaml`; ScaledObject in `deploy/agent-echo-scaling.yaml`; add-ons applied by a new `make addons`, versions pinned in the Makefile | `kubectl apply` would otherwise reset the replica count on every `make deploy`. Raw manifests until the Helm milestone |

### Decision to confirm

CLAUDE.md lists "metrics-server, replicas/HPA" and the primer calls KEDA a stretch goal. The
plan promotes KEDA to the main path because of two probe results: CPU does not track echo
sessions, and scaling late loses sessions. The alternatives:

| Option | Cost | What you get |
|---|---|---|
| **KEDA on session count (planned)** | Three extra pods in a `keda` namespace, one more dependency to document | Scales on the real signal, works with the keyless agent, standard HPA underneath |
| HPA on CPU via metrics-server only | No extra component | Does not scale the echo agent in a demo unless sessions carry real audio and requests are set artificially low; reacts after the load arrives. Reasonable for the provider-backed agent later, weak now |
| Orchestrator scales the Deployment itself | RBAC for the orchestrator, a hand-written control loop | Can scale before dispatching, so no missed sessions; reinvents the HPA and ties the API to Kubernetes |

If you prefer CPU-only HPA, steps 1, 2 and 3 are unchanged, step 4 becomes a plain
`HorizontalPodAutoscaler`, and the demo needs audio publishers to trigger it.

## Files

Create:

- `deploy/agent-echo-scaling.yaml`: KEDA `ScaledObject`.
- `deploy/addons/metrics-server-patch.yaml` (or a `kubectl patch` in the Makefile) for `--kubelet-insecure-tls`.
- `scripts/load.sh`: start `COUNT` sessions, print session statuses and worker pods every few seconds for `HOLD` seconds, stop the sessions, then watch the scale-down. `curl`, `jq`, `kubectl` only.
- `docs/notes/scaling.md`: what was built and measured, dependencies and trade-offs.

Change:

- `agents/echo/agent.py`: `AgentServer(load_fnc=…, load_threshold=1.0, num_idle_processes=MAX_JOBS, drain_timeout=…)`, values from env.
- `deploy/agent-echo.yaml`: drop `replicas`; add resources, `MAX_JOBS`, `DRAIN_TIMEOUT_SECONDS`, grace period, update strategy; add the headless Service.
- `orchestrator/app/sessions.py`, `main.py`, `config.py`: `GET /scaling/{agent}`, `GET /workers`, the re-dispatch loop, `pending_reason` on status. Tests for each in `orchestrator/tests/`.
- `deploy/orchestrator.yaml`: `WORKER_SERVICES` (agent name → headless Service), `REDISPATCH_AFTER_SECONDS`.
- `Makefile`: `addons` (metrics-server, KEDA; part of `up`), `scale`, `workers`, `load`, `top`; `deploy` waits on the ScaledObject instead of a fixed replica count.
- `scripts/smoke.sh`: unchanged in behaviour, but `COUNT=10` now spans pods; raise `READY_TIMEOUT` to cover one scale-up.
- `CLAUDE.md`: decisions and next milestone.

No change to `kind-config.yaml`, so the cluster does not need to be recreated.

## Steps and pass criteria

1. **Capacity.** `load_fnc`, `MAX_JOBS`, idle processes, resources. Pass: one pod holds exactly 5 sessions and logs `worker is at full capacity`; `/worker` reports 5 jobs; an idle pod uses under 300 MB. Record what the sixth session does (the "registered but full" probe).
2. **Manual scaling and drain.** Grace period, drain timeout, rollout strategy, headless Service, `GET /workers`, `make workers`. Pass: with 3 replicas, 12 sessions spread across all three pods; scaling to 1 while sessions are live with a browser attached leaves every echo audible and the removed pods exit only when their sessions end; a `rollout restart` under load drops nothing. Measure how long LiveKit keeps a hard-killed agent in the participant list and what the session reads.
3. **Measure.** metrics-server, `make top`. Join real audio publishers to a handful of sessions (`lk room join --publish-demo` or the fake-microphone Chrome from the last milestone; confirm the `lk` flags first). Pass: CPU and memory per echo job written down, and requests and `MAX_JOBS` adjusted to match.
4. **Autoscaling.** KEDA, `GET /scaling/{agent}`, ScaledObject. Confirm the `metrics-api` trigger fields against the current KEDA docs before writing it. Pass: `kubectl get hpa` shows the generated HPA with the session metric; 12 sessions take the Deployment from 1 pod to at least 3 with no manual step; after they end it returns to 1 within about two minutes.
5. **Re-dispatch.** Pass: a burst of 12 against one pod ends with all 12 `ready`, and the log shows which sessions were re-dispatched; with the worker scaled to 0 and back, a waiting session recovers (the probe that failed above); no session ever has two agents.
6. **Load script and smoke.** `make load`, `make smoke` green against the autoscaled cluster, no rooms or job processes left afterwards.
7. **Wrap-up.** `docs/notes/scaling.md`, `CLAUDE.md`, mark this plan done with differences. No commits unless you ask.

## Risks and open points

- **Reaction time.** HPA syncs every 15 s and KEDA polls on its own interval, so a burst waits roughly 15 to 30 s for new pods; pod start itself is 3 s. Headroom and re-dispatch cover the gap, they do not remove it. Record the measured time to `ready` for the 12-session burst.
- **Re-dispatch races.** If LiveKit assigns the old dispatch just as it is replaced, a room could get two agents. Deleting before creating should prevent it; step 5 checks.
- **Single-node cluster.** All worker pods share one node and one Docker VM (16 CPUs, 8 GB). This shows pod scaling, not node scaling, and six pods plus KEDA is near the comfortable limit.
- **Empty sessions count as load.** A session nobody joins holds a job slot until `empty_timeout` (120 s). That is correct for capacity but makes scale-down lag abandoned sessions by two minutes.
- **Long-lived terminating pods.** Up to 30 minutes by design. For development, stop sessions first or force-delete the pod.
- **Job count is not CPU.** Right for the echo agent. The provider-backed agent may need a CPU trigger alongside the session trigger; decide in that milestone with step 3's numbers.

## Not in this milestone

The provider-backed voice agent, Helm, the README, API authentication, multi-node LiveKit
(Redis), Prometheus/Grafana, scale-to-zero, and node autoscaling.

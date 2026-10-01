# Deployment: the Helm chart

Everything in the `livekit` namespace is one Helm release, `livekit-orch`, from
`charts/livekit-orch`. It replaced the raw manifests that used to be in `deploy/`
(`make manifests` prints what the chart renders). `deploy/addons/` still holds the
metrics-server kustomization. Original plan: `plans/helm-chart.md`.

## Model

- **One chart, own templates, no dependencies.** LiveKit's Deployment, ConfigMap and Service are
  ported from the manifests the networking spike verified, not taken from the official
  `livekit-server` chart.
- **Fixed object names** (`livekit`, `orchestrator`, `agent-<name>`), because the Makefile, the
  scripts and in-cluster DNS names use them. One release per namespace.
- **Secrets are referenced, never rendered.** The chart names existing Secrets
  (`secrets.livekitKeys`, `agents.voice.envFromSecret`); `make secret` and `make deploy` create
  them from `.env` through stdin. `helm get values` holds no key.
- **Agents are a map.** Each enabled entry under `agents:` gets a worker Deployment, a headless
  Service and a KEDA `ScaledObject`. The orchestrator's `AGENT_NAMES` and `WORKER_SERVICES` are
  derived from the map, `orchestrator.defaultAgent` first. The key is the name the worker
  registers under: the chart sets `AGENT_NAME`, which both agents read.
- **Disabled agents stay listed.** A session for an agent with no workers reads
  `pending (no_workers)`, as before.
- **`sessionMaxSeconds` (1800) is set once** and becomes the orchestrator's
  `SESSION_MAX_SECONDS`, the workers' `DRAIN_TIMEOUT_SECONDS`, and the grace period (+ 60).

## What `make deploy` passes

`helm upgrade --install` with values only known on the machine. None is committed.

| Value | From | Effect of a change |
|---|---|---|
| `livekit.nodeIP` | Host's LAN address, detected (or `NODE_IP=…`) | LiveKit restarts |
| `orchestrator.image.id`, `agents.<name>.image.id` | `docker image inspect` of the local image | A pod annotation changes, so an image rebuilt under the same tag rolls its pods |
| `agents.voice.enabled` | A provider key in `.env` | Voice objects are created or removed |
| `agents.voice.configVersion` | `resourceVersion` of the `provider-keys` Secret and `agent-voice-config` ConfigMap | Voice workers roll when a key or a `VOICE_*` setting changed |

`HELM_ARGS` adds arguments, e.g. `make deploy HELM_ARGS="-f my-values.yaml"`. `make scale N=3`
is a `helm upgrade --reuse-values` that sets `agents.<name>.scaling.minReplicas`; the next
`make deploy` puts it back. `make lint` (part of `make test`) lints the chart and renders it with
and without the voice agent.

KEDA is installed by its own chart (`make addons`, version pinned in the Makefile) with
`--repo`, so nothing is added to the local Helm repository list.

## Findings

- Helm will not adopt objects that `kubectl apply` created. Moving an existing cluster to the
  chart took a `make down` and `make up`. (`helm upgrade --take-ownership` exists; not tried.)
- The KEDA chart names its Deployments differently from the release manifest
  (`keda-operator-metrics-apiserver`, `keda-admission-webhooks`). `make addons` waits with
  `helm --wait` instead of naming them.
- The image ID Docker reports is not the one the kind node reports for the same image. It still
  changes on every rebuild, which is all the annotation needs.
- **LiveKit runs without `--dev`.** Nothing else had to be set: keys come from `LIVEKIT_KEYS`,
  `--bind 0.0.0.0` was already passed. The log has no debug lines and no tokens.
  `livekit.dev: true` turns it back on.
- `livekit.forceTcp: true` makes the server offer TCP 7881 only, to test the fallback used when
  UDP is blocked: `make deploy HELM_ARGS="--set livekit.forceTcp=true"`, and `make deploy` to
  go back.
- No `--wait` on the application release. A worker rollout waits for its sessions to drain;
  `kubectl rollout status` returns once the new pod is ready and leaves the old one
  `Terminating`, and the release stays `deployed`.
- The HPA and KEDA write to objects the chart owns. With no `replicas` on the worker
  Deployments, repeated upgrades under Helm 4's server-side apply raised no conflict.
- A change to the agent list restarts the orchestrator. For a few seconds after the rollout a
  request can still reach the old pod and get an empty reply (the behaviour already noted in
  `docs/notes/orchestrator.md`).
- The workers log a deprecation warning: `agent_name` set in code is to move to `livekit.toml`
  in a later `livekit-agents` release.

## Verified (2026-10-01, macOS, kind, Helm 4.2.4)

| Check | Result |
|---|---|
| Rendered chart against the old manifests | Same objects and fields, except: added labels and annotations, `NODE_IP` as a literal, `SESSION_MAX_SECONDS` set explicitly, no Namespace object |
| `make up` on a new cluster | 2 min with images cached; releases `keda` and `livekit-orch` deployed |
| `make test`, `make smoke`, `make smoke AGENT=voice`, `make load` | Pass; load went 1 → 4 → 1 pods, all 12 sessions ready after 22 s and 28 s in two runs |
| Second `make deploy` | No pod restarted |
| `make deploy NODE_IP=…` | Only LiveKit restarted |
| `VOICE_GREETING` added to `.env`, then removed | Only the voice workers rolled, each time |
| Orchestrator image rebuilt under `:dev` | Only the orchestrator rolled |
| `make VOICE= deploy` | Voice objects, Secret and ConfigMap removed; smoke passes; a voice session reads `pending (no_workers)` |
| `make scale N=3`, then `make deploy` | Three pods held; back to a minimum of 1, no conflict |
| Echo image rebuilt with a session live | Session kept its agent on the `Terminating` pod, which exited when the session was stopped; release `deployed` |
| Without `--dev` | Headless Chrome and Firefox (fake microphone) received echoed audio over UDP; workers registered; webhooks arrived (a session ended with LiveKit's reason); no token in the log |
| Agent from a values file only | An entry `parrot` using the echo image got workers, a session reached `ready` on it, and it was removed again by the next deploy |
| Spoken conversation with the voice agent, by hand | Worked in Chrome, Firefox and Safari on the chart-deployed cluster, without `--dev` |
| TCP-only media without `--dev` (`livekit.forceTcp=true`) | Headless Chrome and Firefox, and Safari by hand, connected to `:7881` over TCP and received the echo; the agent logged frames received and played |

Not verified: Helm 3; the chart on anything but this kind cluster.

## Dependencies and trade-offs

- **Helm is a new prerequisite** (and `kedacore.github.io` is fetched by `make addons`).
- **The chart is for this cluster.** Images are side-loaded (`pullPolicy: Never`), LiveKit uses
  `hostNetwork` with ports that must match `kind-config.yaml`, the API is a NodePort. It is a
  packaging of the local setup, not a production chart.
- **Not the official LiveKit chart.** Simpler and already verified; it does not follow upstream
  changes.
- **No raw manifests in the repo.** Reading them takes `make manifests`.
- **Machine-specific values are passed by the Makefile**, so a bare `helm install` needs
  `--set livekit.nodeIP=…` and the Secret created first.
- **Every rebuild rolls pods on the next deploy.** For workers that means a drain, which waits
  for live sessions.
- metrics-server is still installed by kustomize, not by chart.

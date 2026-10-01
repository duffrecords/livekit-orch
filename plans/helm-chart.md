# Helm chart

> **Status: done.** Kept as the original plan. Differences in the result: both agents now read
> `AGENT_NAME` (set by the chart from the key under `agents:`), since their names were fixed in
> code and step 7 could not pass otherwise, so `agents/` did change; `HELM_ARGS` was added to
> the Makefile to pass a values file; LiveKit's pod also carries a checksum of its config, so a
> config change restarts it; the "echo heard in a browser" checks used headless Chrome and
> Firefox with a fake microphone, not a person. A spoken conversation with the voice agent was
> checked by hand afterwards, in Chrome, Firefox and Safari. Not checked: Helm 3. What was built and verified: `docs/notes/deployment.md`.

## Context

Everything runs from raw manifests in `deploy/`, applied by `make deploy` with shell around them:
a ConfigMap for the advertised address, a compare-and-restart for voice settings, a
`kubectl delete` when there is no provider key. The requirement is "Kubernetes manifests or Helm
charts", so this milestone is not about ticking a box. It is about what the manifests make
awkward: adding an agent means three files plus an edit to the orchestrator's env; a rebuilt
image is not picked up; three numbers that must agree (session limit, drain timeout, grace
period) are written out in four places; and LiveKit still runs with `--dev`, which the notes
deferred to "the real chart". The README is written after this, against the final commands.

Confirmed with you:

- **Own templates for LiveKit**, not the official `livekit-server` chart.
- **The chart replaces `deploy/*.yaml` and `deploy/voice/`.** `make manifests` prints the
  rendered YAML. `deploy/addons/` stays.
- **In scope beyond a one-to-one port:** agents as a values map, restart on a rebuilt image,
  drop `--dev`, and KEDA installed through its Helm chart.

**Outcome:** `make up` on a fresh machine installs one Helm release, `livekit-orch`, in
namespace `livekit`, and `make smoke`, `make load`, `make test` and a browser session behave as
they do today. `helm get values` shows no secret. Adding an agent is one entry under `agents:`.
`make image deploy` after a code change rolls the affected pods with no further command.
LiveKit's log no longer prints join tokens.

## Design

| Piece | Choice | Why |
|---|---|---|
| Chart | `charts/livekit-orch`, `apiVersion: v2`, no dependencies. Release `livekit-orch`, namespace from the release | One install, no chart repository for the application itself |
| Object names | Fixed, as today: `livekit`, `orchestrator`, `agent-<name>`. `app:` labels kept; `app.kubernetes.io/*` and `helm.sh/chart` added | The Makefile, `scripts/*.sh`, the notes and in-cluster DNS names all use them. Cost: one release per namespace, stated in the chart |
| Secrets | The chart only references Secrets by name (`secrets.livekitKeys`, `agents.voice.secretRef`). `make secret` and `make deploy` keep creating them from `.env` through stdin | Project convention: no key in values, on a command line, or in the release record |
| Advertised address | `livekit.nodeIP`, required, passed by `make deploy` with `--set`; rendered straight into the pod's `NODE_IP` env | A changed address changes the pod template, so Helm restarts LiveKit. Replaces the `livekit-node` ConfigMap and the `node-ip` target. Never in a committed file |
| Agents | `agents:` map. One template renders Deployment, headless Service and `ScaledObject` per enabled entry. Per agent: `enabled`, `image`, `maxJobs`, `resources`, `scaling.{minReplicas,maxReplicas,sessionsPerPod,scaleDownStabilizationSeconds}`, `env`, `envFromSecret`, `envFromConfigMap` | Echo and voice differ only in those fields today |
| Orchestrator wiring | `AGENT_NAMES` and `WORKER_SERVICES` derived from the map, `orchestrator.defaultAgent` first. Disabled agents stay listed | Keeps today's verified behaviour: a voice session with no voice workers reads `pending (no_workers)` |
| Linked numbers | `sessionMaxSeconds` (1800) set once: orchestrator `SESSION_MAX_SECONDS`, workers' `DRAIN_TIMEOUT_SECONDS`, and grace period = that + 60 | They must agree and are copied by hand now |
| Voice on/off | `agents.voice.enabled`, set by `make deploy` from the existing `VOICE` check on `.env` | Helm removes the objects when it turns false; the `kubectl delete -f deploy/voice/` goes away |
| Voice settings | Still the `agent-voice-config` ConfigMap and `provider-keys` Secret made from `.env`. `make deploy` passes their `resourceVersion`s as a pod annotation | Free-text settings (`VOICE_INSTRUCTIONS`) do not survive `--set`; the annotation replaces the compare-and-restart shell |
| Rebuilt image | `make deploy` passes `docker image inspect -f '{{.Id}}'` per image as `image.id` → pod annotation | Same tag, new ID, new pod template, rollout. Workers still drain first |
| `--dev` | `livekit.dev: false` by default; the flag stays available for debugging | Stops tokens being logged. Tried as its own step and reverted if a client stops connecting |
| KEDA | `helm upgrade --install keda keda --repo https://kedacore.github.io/charts --version <pinned> -n keda --create-namespace --wait` | `--repo` avoids touching the user's Helm repo list. metrics-server stays on its kustomization (not asked for; only used by `kubectl top`) |
| Waiting | No `--wait` on the application release; the existing `kubectl rollout status` lines stay | A worker rollout waits for sessions to drain; a Helm wait would mark the release failed |
| `make scale` | `helm upgrade --reuse-values --set agents.$(AGENT).scaling.minReplicas=$(N)` | A `kubectl patch` on an object Helm applies server-side can conflict on the next upgrade. `make deploy` still resets it |

## Files

Create:

- `charts/livekit-orch/Chart.yaml`, `values.yaml` (commented, the measured numbers carried over
  from the manifest comments), `templates/_helpers.tpl`, `templates/livekit.yaml`,
  `templates/orchestrator.yaml`, `templates/agents.yaml`, `templates/NOTES.txt`.
- `docs/notes/deployment.md`: values, what `make deploy` passes and why, findings, what was verified.

Change:

- `Makefile`: `deploy` becomes `helm upgrade --install` plus the rollout checks; `addons`
  installs KEDA by chart; `node-ip` and the restart logic in `voice` go; `scale` as above; new
  `manifests` (`helm template`) and `lint` (`helm lint`, and a render with voice on and off);
  `test` also runs `lint`; namespace created inline instead of from `deploy/namespace.yaml`.
- `docs/notes/{orchestrator,scaling,local-networking,voice-pipeline-providers}.md`: file paths,
  and the statements this milestone makes untrue (manual `rollout restart` after a rebuild,
  `--dev`, the `livekit-node` ConfigMap, how KEDA is installed).
- `CLAUDE.md`: decision entry, the "new agent" convention, next milestone.
- `plans/plan-the-readme-dapper-cloud.md`: resolve the `[Helm]` markers.

Delete: `deploy/namespace.yaml`, `deploy/livekit.yaml`, `deploy/orchestrator.yaml`,
`deploy/agent-echo*.yaml`, `deploy/voice/`.

No change to `kind-config.yaml`, `agents/`, `orchestrator/`, `scripts/` or `web/`.

## Steps and pass criteria

1. **Chart, offline.** Write the chart. Pass: `helm lint` clean; a scratchpad script compares
   `helm template` (voice on) with `deploy/` object by object, and the only differences are the
   intended ones: added labels and annotations, `NODE_IP` as a literal, no `livekit-node`
   ConfigMap, `--dev` (still on at this step), no Namespace object.
2. **Makefile and add-ons.** Confirm first: the KEDA chart version matching 2.21.0, its
   Deployment names (they differ from the release manifest's), and that the `helm` flags used
   exist in both Helm 3 and 4. Pass: `make lint`, `make manifests`.
3. **Fresh install.** Stop sessions, `make down`, `make up`. A fresh cluster is needed once:
   objects created by `kubectl apply` are not owned by a release and Helm refuses to adopt them.
   Pass: `helm list -A` shows `keda` and `livekit-orch` deployed; `make smoke`; `make load`
   (1 → 4 → 1); `make test`; echo heard in a browser; `helm get values livekit-orch` holds no key.
4. **Upgrade behaviour.** Pass, each checked separately: a second `make deploy` restarts
   nothing; `make deploy NODE_IP=…` restarts only LiveKit; a changed `VOICE_GREETING` restarts
   only the voice workers; a rebuilt orchestrator image rolls only the orchestrator; removing
   the provider key removes the voice objects and `make smoke` still passes; `make scale N=3`
   holds three pods and the next `make deploy` returns to one without a conflict.
5. **Drain through a Helm upgrade.** With a live session on a worker, rebuild that agent's image
   and `make deploy`. Pass: the session keeps its agent, the old pod exits when it ends, and the
   release is `deployed`, not `failed`.
6. **Drop `--dev`.** Pass: Chrome and Firefox exchange audio with the echo agent, the agent
   registers, webhooks still arrive (`session ended` logged with LiveKit's reason), and
   `make logs` shows no token. Otherwise set the default back to `true` and record why.
7. **An agent from values only.** Add a second echo-image agent under `agents:` in a scratch
   values file. Pass: it gets a session and appears in `make workers`; nothing else was edited.
   Not committed.
8. **Remove `deploy/` manifests, update notes and `CLAUDE.md`,** resolve the README plan's
   markers, mark this plan done with differences. No commit unless you ask.

## Verification

Steps 3 to 7 are the end-to-end check. Before finishing: `make down && make up` once more from a
clean tree, then `make test`, `make smoke`, `make load`, `make smoke AGENT=voice`; `git diff`
and the chart grepped for addresses, keys and tokens (the address must appear only as a
`--set` in the Makefile, computed at run time).

## Risks and open points

- **Helm 4 applies server-side.** KEDA and the HPA write to objects the chart owns (finalizers,
  replica count). The chart sets no `replicas` on workers, which should avoid a conflict; step 4
  shows whether it does.
- **Only Helm 4.2 is installed here.** Helm 3 goes in the notes as not verified.
- **Without `--dev`** LiveKit may want settings it defaulted before (bind address, log level,
  key length). The generated secret is 48 characters. Step 6 has a fallback.
- **Listing disabled agents** keeps current behaviour but means a typo-free name with no
  workers waits rather than failing. Left as is; noted in the README's limits.
- **Live sessions end at step 3.** The cluster is recreated.

## Not in this milestone

The README, a values schema, `helm test` hooks, publishing the chart or images to a registry,
metrics-server by chart, multi-node LiveKit, and API authentication.

# WebRTC networking in local Kubernetes

Target: cross-platform (macOS, Windows, Linux). On macOS/Windows Docker runs in a VM, so container IPs aren't routable from the host — only published ports work.

## Constraints

1. **Publish UDP to the host.** Use LiveKit's single UDP mux port (`rtc.udp_port`, e.g. 7882) instead of a port range. Also publish TCP 7881 (ICE/TCP fallback) and 7880 (signaling).
2. **Same port number at every hop.** LiveKit advertises its port in ICE candidates; a NodePort remap (30882→7882) breaks connectivity. Use `hostPort`/`hostNetwork` on the LiveKit pod with identical host-side numbers.
3. **Advertise a reachable IP.** For local demo: the host's LAN address via `node_ip` (`NODE_IP` env var), `use_external_ip: false`. Not `127.0.0.1`: Firefox can't use a loopback candidate (see spike results).
4. **Agents are media clients too.** Worker pods can't use the host's address; add `rtc.advertise_internal_ip: true` so the node's own IP is advertised as well (see spike results).

## Cluster tool comparison

| | kind | k3d | Minikube |
|---|---|---|---|
| UDP to host | `extraPortMappings` with `protocol: UDP` | `-p 7882:7882/udp@server:0` | Docker driver on Mac/Win: NodePorts unreachable; `minikube tunnel` doesn't carry UDP |
| Cross-OS consistency | Yes (Docker everywhere) | Yes (Docker everywhere) | No — varies by driver (docker, hyperkit, qemu, Hyper-V) |
| Config in repo | `kind-config.yaml` | k3d config YAML | Mostly CLI flags/profiles |
| Port ranges | One entry per port | Ranges supported | n/a |
| Defaults to fight | None | Bundled Traefik + ServiceLB; ports go through k3d-proxy (nginx) — use `:direct` mapping or verify UDP | n/a |
| Local images | `kind load docker-image` | `k3d image import` / built-in registry | `minikube image load` |

**Decision: kind.** Direct port mapping to the node container (no proxy), declarative config, most likely installed on reviewers' machines. k3d is a close second; Minikube's driver-dependent networking undermines a cross-platform demo.

## Risks

- **Windows:** Docker Desktop + WSL2 forwards published UDP, but keep the TCP 7881 fallback.
- **VPNs/firewalls** may block UDP. Fallbacks: LiveKit's embedded TURN, or ICE/TCP. Document in README.
- **De-risk first:** spike kind + LiveKit `--dev` + browser join through mapped ports before building anything else. Done — results below.

## Spike results (2026-09-30)

Environment: macOS (Apple silicon), Docker Desktop 29.8, kind 0.31, LiveKit server v1.13.7, `livekit-agents` 1.8.3 (Python), `livekit-client` 2.22.3. Browser was headless Chrome with its fake microphone (a repeating beep); "echo heard" below means the browser's inbound audio energy rose while the echo agent logged frames received and played. The first round used Chrome only and advertised `127.0.0.1`; a manual test in Firefox then failed, which led to the "Firefox and the advertised address" section below.

Setup under test: `kind-config.yaml`, `deploy/`, `agents/echo/`, `web/index.html`, driven by the `Makefile` (`make up`, `make token`, `make dispatch`, `make web`, `make down`).

### What was tested

| Test | Result |
|---|---|
| Signaling from host (`curl localhost:7880`) | Pass |
| Browser media over UDP | Pass. Two candidate pairs succeed: the published port (`127.0.0.1:7882`) and a direct path where the server reaches the host's LAN address through Docker's outbound NAT. Chrome usually picked the direct path |
| Browser media over the published UDP port only (direct path blackholed inside the node) | Pass. Selected pair `127.0.0.1:7882`, echo heard |
| In-cluster worker registers as `echo` | Pass, via `ws://livekit.livekit.svc.cluster.local:7880` |
| Explicit dispatch → agent joins → echo round trip | Pass. Server-side pair for the agent: node IP `:7882` ↔ pod IP |
| Same, with `advertise_internal_ip` removed | Pass over UDP. The agent connects through a peer-reflexive candidate because the server can send to pod IPs directly |
| `rtc.force_tcp: true` | Pass. Browser on `127.0.0.1:7881`, agent on node IP `:7881`, echo heard |
| `force_tcp` with `advertise_internal_ip` removed | **Fail.** Agent's peer connection fails: the server's TCP candidates are passive, so the agent needs a reachable address to dial |
| Two rooms at once (two browsers, two dispatches) | Pass. One worker pod ran both jobs as separate subprocesses; both echoed through the single UDP port |
| `make down` | Pass. Ports 7880–7882 released; a fresh `make up` works |

### Firefox and the advertised address

With `node_ip: 127.0.0.1`, Firefox (154, real microphone) failed with `could not establish pc connection`; reproduced in headless Firefox 155.

- Firefox sends its host candidates as mDNS names, which LiveKit ignores, so the server cannot open a path towards the browser.
- Firefox never completes a check to the server's `127.0.0.1` candidate. Enabling `media.peerconnection.ice.loopback` did not help.
- Chrome was unaffected: it reaches `127.0.0.1:7882`, and it also sent a real host candidate that the server could reach directly.

Fix: advertise the host's LAN address instead of loopback, and publish the kind ports on all interfaces so that address is served. A manual run in Firefox 154 with a real microphone then worked: the echo was audible. Headless results with the fix:

| Browser | UDP | TCP only (`force_tcp`) |
|---|---|---|
| Firefox (default settings) | Pass, `<host LAN address>:7882`, echo heard | Pass, `:7881`, echo heard |
| Chrome | Pass, echo heard | Pass, `:7881`, echo heard |

The in-cluster agent is unchanged (node IP ↔ pod IP).

How it is wired: `make deploy` detects the host's primary address and stores it in ConfigMap `livekit-node`; the LiveKit pod reads it as `NODE_IP`. The address is never written to the repo. Consequences:

- Ports 7880–7882 are reachable from the local network, not just the host. Keys are random per machine, but treat the cluster as LAN-visible.
- After switching networks, re-run `make deploy`; it restarts LiveKit if the address changed.
- With no network the Makefile falls back to `127.0.0.1` (Chrome only). `make deploy NODE_IP=…` overrides detection.

The test page also acquires the microphone before connecting. That alone did not fix headless Firefox (fake microphone); whether it helps real Firefox was not established and the fix does not depend on it.

### Configuration that turned out to be required

- **`enableServiceLinks: false` on the LiveKit pod.** A Service named `livekit` makes Kubernetes inject `LIVEKIT_PORT=tcp://…`; the server reads `LIVEKIT_*` variables as config and crash-loops parsing it as a port number. Set on the agent pod too, since the worker also reads `LIVEKIT_*`.
- **`rtc.interfaces.includes: [eth0]`.** With `hostNetwork` the server enumerates every pod veth on the node. kindnet gives them all the same address, and binding the UDP mux to each fails with `address already in use`.
- **`rtc.advertise_internal_ip: true`** alongside the explicit `node_ip`. An explicit `node_ip` otherwise replaces every host candidate with that one address; tested with `127.0.0.1`, which for an agent pod is itself. UDP survives without it (see table) but the agent's TCP fallback does not. Side effect: browsers also receive the node-IP candidate, which they can't reach; it is ignored.
- **`--bind 0.0.0.0`.** Passed from the start because the server source shows `--dev` with no keys binds signaling to loopback only; not tested without it.
- **API keys:** `make env` generates a random key/secret into git-ignored `.env`; `make up` turns that into Secret `livekit-keys`, passed to the server as `LIVEKIT_KEYS`. No key appears in the repo.

### Behaviour worth knowing for the orchestrator

- Dispatching to a room that doesn't exist creates it; the agent joins and waits before any user arrives.
- The worker pod reported Ready while it could not reach LiveKit (it was retrying its registration). Readiness on `:8081` means "process up", not "registered".
- A client that vanishes without a leave message is only dropped after about 20 s; the agent keeps receiving silence until then.
- After the last participant leaves, the room lingers for the default `departure_timeout` (20 s) and then closes on its own. Faster cleanup needs an explicit room delete.
- An agent does not keep a room alive. Seen in a manual test: user joined and left, the agent was dispatched 8 s later, and the room still closed on the departure timeout, ejecting the agent; the user's next join created a fresh room with no agent. The orchestrator should create the room and dispatch as one step, before handing out a token.
- `--dev` turns on debug logging, which prints join tokens in the server log. Drop `--dev` once the real chart sets keys and ports explicitly.
- kind has no metrics API by default (`kubectl top` fails), so an HPA needs metrics-server installed first.

### Not covered

Safari, Windows and Linux hosts, a second device on the LAN, VPN or firewall interference, LiveKit's embedded TURN, and more than one worker replica.

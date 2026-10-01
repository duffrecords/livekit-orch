# README

## Context

Networking, the orchestrator API, worker scaling and the voice agent are done and written up in
`docs/notes/`, but the repo has no `README.md`. Three of the deliverables live there: **setup
instructions**, a **brief architecture explanation**, and a **working demo**, plus the
requirement that **dependencies and trade-offs are clearly documented**. Today that material is
spread over five notes written as working records (findings, measurements, dated results), which
a reviewer should not have to read to run the project.

Confirmed with you:

- **Helm comes first.** Done (`plans/helm-chart.md`, `docs/notes/deployment.md`); the sections
  that depended on it are filled in below.
- **The demo is a scripted walk-through** the reader runs, with expected output captured from a
  real run. No recording, no new `make demo` target.

**Outcome:** someone with Docker and kind, and no knowledge of LiveKit, clones the repo, follows
the README top to bottom, and within about ten minutes hears the echo agent in a browser, sees
12 sessions scale the workers 1 → 4 → 1, and sees everything cleaned up. With an OpenAI key they
talk to the voice agent. They can find every requirement and every trade-off without opening
another file.

## Approach

| Question | Choice | Why |
|---|---|---|
| One file or several | One `README.md` at the root, about 300 lines. Notes stay as they are and are linked for depth | The reviewer reads one file; the notes keep the evidence |
| Order | Run it first, explain it second: quick start → demo → architecture → API → operations → trade-offs | A reviewer checks that it works before reading why |
| Default path | Keyless (`echo`) everywhere. The voice agent is one optional section | Everything except the conversation is verifiable without signing up for anything |
| Diagram | ASCII, updated from the sketch in `docs/notes/livekit-primer.md` | Renders in any viewer; the repo has no remote yet, so Mermaid rendering is not a given |
| Numbers | A few headline measurements (time to ready, pods, turn latency), each linked to its note | Shows it was measured without copying the tables |
| Requirements | A table near the top mapping each requirement to where it is met and how to see it | Makes review a checklist |
| Commands | Only `make` targets and `curl` against `localhost:8080`. Every one is run before it is written down | The Makefile is already the interface; no command in the README is untested |
| Output samples | Real output, shortened. Tokens truncated, addresses replaced by placeholders such as `<host LAN address>` and `<pod IP>` | Global rule: no real addresses or credentials in tracked files |

## Outline

| # | Section | Content | Source |
|---|---|---|---|
| 1 | Title and summary | Three sentences: what it is, what runs where, that it works without API keys | `CLAUDE.md` |
| 2 | Requirements | Table: requirement → how it is met → how to see it (command or section) | `CLAUDE.md`, primer mapping table |
| 3 | Prerequisites | Docker (8 GB for the VM), kind, kubectl, helm, make, curl, jq, openssl, python3. Optional: `uv` (`make test`), `lk` (`make token`, `dispatch`, `status`). Internet access for images and KEDA. Versions it was tested with | `Makefile`, spike results, `deployment.md` |
| 4 | Quick start | `make up`, `make smoke`, `make web`, open the page, Start session, speak, hear the echo. What `make up` does in one list. `make down` | `Makefile` |
| 5 | Demo walk-through | Six numbered steps, each with the command and what to expect (below) | notes' "Verified" tables |
| 6 | Voice agent (optional) | Add `OPENAI_API_KEY` to `.env`, `make up`, agent `voice`. The `VOICE_*` table, per-session `instructions`/`greeting`, cost and rate-limit warning | `voice-pipeline-providers.md`, `.env.example` |
| 7 | Architecture | Diagram; component table (LiveKit, orchestrator, workers, KEDA, browser client); a session's life from `POST /sessions` to room close; the four design decisions that matter: LiveKit as source of truth, job-count capacity, re-dispatch, drain | all notes |
| 8 | API | Endpoint table, status table, three `curl` examples, pointer to `/docs` | `orchestrator.md` |
| 9 | Operations | Health checks per component (probe, what it means), logs (`make logs`, line types), scaling commands, cleanup paths table, `make` target table | `orchestrator.md`, `scaling.md`, manifests |
| 10 | Deployment | The chart and its values (agents as a map, how to add one), what `make deploy` passes, how secrets get in (`.env` → Secret, never in values), `make image deploy` after a code change, `make manifests` | `deployment.md` |
| 11 | Dependencies and trade-offs | One table: choice → what it costs → why it was taken (below) | every note's trade-offs section |
| 12 | Limits | Not done or not verified: no auth, one orchestrator replica, one node, Windows and Linux hosts untested, Helm 3 untested, audio during scale-down not heard in a browser | notes' "Limits" and "Not verified" |
| 13 | Troubleshooting | Firefox cannot connect / changed network → `make deploy`; VPN blocking UDP; worker pod stuck `Terminating`; voice turns failing with 429; an empty reply just after a deploy; out of memory under load | notes |
| 14 | Repository layout and further reading | Tree of top-level directories; links to the notes and `plans/` | `git ls-files` |

### Demo walk-through (section 5)

1. **One session.** `make web`, Start session with agent `echo`; the page shows `ready` then `active`, and the echo is heard.
2. **Status through the API.** `make sessions`; `curl localhost:8080/sessions/<id>`.
3. **Concurrent sessions.** A second tab starts its own session; both echo; `make workers` shows two jobs.
4. **Scaling.** `make load`: 12 sessions, 5 `ready` and 7 `pending (workers_full)`, pods 1 → 4, all `ready` in about 18 s, back to 1 pod about a minute after they stop. Shortened sample output.
5. **Cleanup.** Stop one session with the button (browser disconnects at once); close the other tab (session `ended` about 10 s later, no API call); `make sessions` shows both with their `end_reason`; mention the 120 s empty timeout and the 30 minute limit.
6. **Health and logs.** `curl localhost:8080/readyz`, `make logs`, `kubectl -n livekit get pods,hpa`.

### Trade-offs table (section 11), rows

External speech/LLM APIs instead of local models (keys, audio leaves the machine, cost, rate
limits) · OpenAI default: one key but about 2.5 s per turn against 0.9 s · kind with
`hostNetwork` and a LAN-advertised address (ports visible on the LAN) · LiveKit as the only
state (nothing survives a LiveKit restart; ended history in memory) · job-count capacity instead
of CPU · KEDA as an extra dependency · headroom costs idle pods · slow scale-down by design ·
re-dispatch delay for bursts · one node, pods not nodes · no authentication, loopback only ·
a chart for this cluster only (side-loaded images, `hostNetwork`, NodePort), own LiveKit
templates instead of the official chart.

## Files

Create: `README.md`.

Change:

- `CLAUDE.md`: mark the README done, point "setup instructions" at it.
- `docs/notes/*.md`: only where the walk-through run shows a note is now wrong.

No code changes. If a command in the walk-through does not behave as the notes say, fix the
cause in its own change rather than documenting around it.

## Steps and pass criteria

0. **Helm milestone.** Done.
1. **Baseline run.** `make down`, then from a fresh clone in the scratchpad: `make up`, the six
   walk-through steps, `make down`. Record the wall-clock time of `make up`, the output to quote,
   and anything that differs from the notes.
2. **Draft sections 1 to 5** (run it) and check each command against step 1's record.
3. **Draft sections 6 to 10** (how it works). Rebuild the diagram to include KEDA, the
   webhook, and the headless Services.
4. **Draft sections 11 to 14** from the notes' trade-off and limit sections; nothing in a note's
   trade-offs is missing from the README table.
5. **Follow the README as written**, second fresh clone, no other file open. With a provider key
   in `.env`, section 6 as well (one spoken conversation, by you, since it needs a microphone).
6. **Wrap-up.** `CLAUDE.md`, mark this plan done with differences. No commit unless you ask.

## Verification

- Step 5 passes without leaving the README: cluster up, echo heard, `make load` scales up and
  back, sessions cleaned up, `make down` releases ports 7880 to 7882 and 8080.
- Every relative link resolves (`grep` the links, test each path); every `make` target named in
  the README exists in the `Makefile`.
- Each of the six requirement bullets in `CLAUDE.md` has a row in section 2.
- `git diff` holds no address, hostname, key or token: grep for JWT prefixes (`eyJ`), the
  values in `.env`, and IPv4 patterns other than `127.0.0.1` and `192.0.2.1`.
- `make test` and `make smoke` still pass.

## Not in this plan

The Helm chart itself, a recorded demo, a `make demo` target, a licence file, CI, and testing on
Windows or Linux hosts (listed under Limits instead).

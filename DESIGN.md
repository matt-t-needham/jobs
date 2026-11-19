# jobs: internals and connections

A local dashboard of every running Claude task, grouped by project. Each tile is one task
and shows a name, its project, and a shape and colour that give its status. This document
covers the data sources, the data model, the process layout, how it is deployed on each
kind of machine, and the styling every instance shares.

Written 2026-11-18 against Claude Code 2.1.327. The design is deliberately not tied to one
machine: §3 describes roles, §4 the two ways of deploying them, and §9 the bix.computer
deployment (jobs.bix.computer).

## 1. What Claude Code already records

Everything needed for a first version is already on disk under `~/.claude`. None of it is
a documented API, so it can change between Claude Code releases (see §7).

| Source | What it gives | Notes |
|---|---|---|
| `sessions/<pid>.json` | One file per live Claude Code process: `pid`, `pidDomain`, `sessionId`, `cwd`, `kind` (`interactive` / `bg`), `name`, `status` (`busy`, `idle`, …), `statusUpdatedAt`, `version`, `procStart`, `startedAt` | The main list of what is running. A file can outlive its process, so check that the pid is alive and that `procStart` still matches (pids are reused). `pidDomain` says which pid namespace the pid belongs to: ignore files from a domain other than our own rather than marking them dead. |
| `jobs/<id>/state.json` | Background jobs only: `sessionId`, `cwd`, `name`, `state` (`working` / `blocked` / `done`), `detail`, `needs` (what it is waiting on you for), `suggestedReply`, `children` (subagent fan-out), `tokens`, `cliVersion` | The best status signal. `needs` is the "what does it want from me" line on a tile. Join to sessions by `sessionId`; an interactive session file carries no job id. |
| `jobs/<id>/timeline.jsonl` | State history with timestamps and one-line details | Gives "last did X, N min ago" and progress over time. |
| `projects/<encoded-cwd>/<sessionId>.jsonl` | Full transcript; its tail holds `cost-state` (cost, tokens, model, duration) | Large. Read only the tail, and only for metadata. Also covers sessions that have ended, for history. |
| `projects/<encoded-cwd>/<sessionId>/` | Subagent transcripts for that session | Optional: count subagents still running. |
| `stats-cache.json`, `policy-limits.json` | Usage and plan limits | For a usage-limit panel. |
| `gh-pr-status-cache.json` | PR status Claude Code has already looked up | Could mark a task "done" on merge without calling GitHub ourselves. |
| transcript records' `gitBranch`; a `.git` entry above the `cwd` | Branch; repo root or worktree | The repo label (a worktree's `.git` is a file, so it gives the task slug or JIRA key, `abc-1234-…`) and the branch, without running git. |

Interactive sessions have only `sessions/*.json` and their transcript; there is no
`jobs/` entry, so their status is coarser (busy or idle) unless hooks add more (§3).

**Fields we must never copy.** `state.json` also carries `providerEnv`, `respawnFlags` and
similar, which can hold environment values and credentials. Adapters read an explicit
allow-list of fields (the `Task` model below), never "everything in the file". The
messaging socket path and `.key` files beside the session files are never read.

## 2. Data model

```
Machine  { id, label ("work-laptop" | "bix"), lastSeen }
Project  { id, name, roots[] }
Task     { id = sessionId, machineId, projectId, name, kind (interactive | bg),
           status, detail, needs, repo?, branch?, pr?, startedAt, lastActivityAt,
           costUsd?, tokens?, pid, alive }
```

### Project mapping

A session's `cwd` maps to a project through an ordered list of path-prefix rules in a
per-machine config file, first match wins. Anything that matches no rule is ignored.

Work laptop:

```json
{ "machine": { "id": "work-laptop", "label": "work laptop" },
  "projects": [
    { "match": "~/work-a", "name": "work-a" },
    { "match": "~/work-b", "name": "work-b" } ] }
```

(`~/work-a` includes every repo and worktree under it.) The config is JSON because the
collector uses only the standard library, and `tomllib` needs Python 3.11.

bix:

`config/bix.json`: the whole suite is one project, and the tile's repo label is the
subproject (bix-ai, health, …):

```json
{ "machine": { "id": "bix", "label": "bix" },
  "site": { "name": "bix.computer", "url": "https://bix.computer/" },
  "spoolDir": "~/jobs/spool",
  "projects": [ { "match": "~/apps", "id": "bix", "name": "bix", "repo": "first-dir" } ] }
```

`"repo": "first-dir"` takes the first path segment under the match as the tile's repo label,
which suits a monorepo of independent projects; otherwise (`"git"`, the default) it is the nearest directory with a `.git` entry.
Other settings, with defaults: `claudeDir` (`~/.claude`), `stalledMinutes` (10),
`doneHideMinutes` (60), `deadHideMinutes` (60), `jobMaxAgeHours` (168), `pollSeconds` (2),
`heartbeatSeconds` (30). Sessions started at a project's root need no special handling. Adding a project is
one more rule.

### Status: one enum, many sources

The tile's shape and colour come from one derived status, so the UI never reads raw
fields:

| Derived status | From | Meaning |
|---|---|---|
| `needs-you` | job `state: blocked`, or session `needs_input` / `waiting` / `blocked`, or a hook `Notification` | Waiting on a decision or permission. The most important tile. |
| `working` | job `working`, or session `busy` / `running`, or session `idle` but its transcript written > 10 s after that status (the session file lags mid-turn on 2.1.327) | Making progress. |
| `stalled` | `working` but no transcript write for > N minutes | Possibly hung. |
| `idle` | session `idle`, no job need | Finished its turn, nothing asked. |
| `done` | job `done` (PR merge from `gh-pr-status-cache.json`: not built yet) | Finished. Shown for a while, then hidden. |
| `dead` | session file present but pid gone (same `pidDomain`) | Crashed or killed without cleanup. |

The enum is the contract between the collector and the UI. How each status looks is in §8.

## 3. Roles

The system has three roles. They are described independently of language and service
manager, because §4 runs them in different shapes on different machines.

```
 ~/.claude/{sessions,jobs,projects}        Claude Code hooks (optional)
            │ read                                │ append one JSON line
            ▼                                     ▼
 ┌──────────────── collector ────────────────┐   spool dir
 │ adapters → normalise → project mapper →   │◀──┘   (2 s / 30 s poll)
 │ status deriver → store                    │
 └───────────────┬───────────────────────────┘
                 │ snapshot  { machine, projects, tasks }
                 ▼
 ┌──────────────── server ───────────────────┐
 │ GET /api/state (snapshot)                 │
 │ GET /api/events (SSE push)                │
 │ GET /  (static UI)                        │
 └───────────────┬───────────────────────────┘
                 ▼
             browser UI
```

- **Collector.** Reads Claude Code's files and the hook spool; produces the snapshot.
  - *Adapters* each read one source and return partial `Task` records; the normaliser
    merges them by `sessionId`. A new source (cloud sessions, another machine) is a new
    adapter, not a change to the core.
  - *One poll, cheap.* Every 2 s the collector stats `sessions/`, `jobs/*/state.json` and
    the hook spool, re-reads only files whose mtime or size changed, reads only the bytes
    appended to each live transcript, and checks liveness. No filesystem-watch library.
    It hands on a snapshot when it changes, and at least every 30 s as a heartbeat.
  - *Liveness* on Linux compares `/proc/<pid>/stat` field 22 with `procStart` and skips
    files from another `pidDomain`. On macOS it only checks the pid exists: Claude Code's
    `procStart` format there is unverified.
  - *Old jobs.* `jobs/<id>/state.json` files stay for months (one from May still says
    `working`), so a job with no live session shows only while `updatedAt` is within
    `jobMaxAgeHours`.
  - *Store* is in memory, rebuilt from disk on start. A small SQLite or JSONL history can
    come later if we want "what did I do this week".
  - It needs **no network access at all**.
- **Server.** Holds the latest snapshot, answers `/api/state`, pushes changes over
  Server-Sent Events (simpler than WebSockets; the browser reconnects by itself), and
  serves the static UI. It never reads `~/.claude`.
- **UI.** Static HTML, CSS and a little script; reads `/api/state` once, then follows
  `/api/events`.

The snapshot is the only thing that crosses from collector to server. It holds `Task`
fields only: no transcript text, no `suggestedReply`, no environment.

### Hooks, as a second channel

Claude Code hooks are the documented interface, so they are the stable fallback if the
internal files change. User-level hooks in `~/.claude/settings.json` (`SessionStart`,
`UserPromptSubmit`, `Notification`, `PostToolUse`, `Stop`, `SessionEnd`) give interactive
sessions a real "waiting for permission" status that the session file lacks. They only
apply to sessions started after they are installed, and they belong in user-level settings
so every workspace reports the same way. Each runs
`python3 -I <jobs>/hook.py --spool <spoolDir>`.

`hook.py` writes one small JSON file per event (event, `sessionId`, time, and for
`Notification` its `notification_type` and message) into a spool directory, instead of
calling the collector over HTTP. That keeps one mechanism on every machine, works when the
collector is down (it catches up on start), and means the collector never listens on a
port. The collector deletes each file as it reads it. A hook event counts only when it is
newer than the session file's own status change. `PostToolUse` clears a permission prompt
once it is approved. `idle_prompt` and `auth_success` notifications don't count as
needs-you. The `notification_type` names are from the hooks documentation and haven't yet
been seen on this machine.

## 4. Deployment shapes

The same code runs in one of two shapes, chosen by config.

| | **Single process** (work laptop) | **Split** (bix) |
|---|---|---|
| Processes | Collector and server in one process | Collector on the host; server in a container |
| Snapshot hand-off | In memory | Collector writes it atomically (temp file + rename) to a snapshot directory the server mounts read-only and watches |
| Listens on | `127.0.0.1:<port>` only | Server on `127.0.0.1:<port>`, published through the tunnel behind Cloudflare Access |
| Started by | launchd (`~/Library/LaunchAgents`) running uvicorn from the virtualenv | systemd unit running `python3 -I` for the collector; the compose stack for the server |
| Who can see it | That machine's user | Matt, through Access |

Why split on bix: the bix threat model treats anything on the tunnel as internet-facing,
and no single compromise may reach the host as `matt`. Reading `~/.claude` needs the host
user, and containers must never mount `~/.claude`. So the part that reads `~/.claude` has
no listener and no network, and the part that listens can only see the snapshot.

**Nothing machine-specific in code.** Paths (`~/.claude`, project roots, spool, snapshot
directory), the port, the shape and the machine label come from a per-machine config file
with sensible defaults. A machine with no `~/work-b` simply shows no work-b tiles.

**Language and frameworks.** Python throughout, matching the rest of the bix suite.

- **Collector:** Python 3 standard library only, no packages, run with `python3 -I`. It
  runs on the system Python of any machine (3.12 on bix; macOS's `python3`), so it needs
  no virtualenv and nothing to install. Keep to features from Python 3.9 so an older
  system Python still works.
- **Server:** FastAPI + uvicorn, pinned to the versions the other bix services use
  (`fastapi==0.115.6`, `uvicorn==0.34.0`). `/api/events` is a `StreamingResponse` with
  `text/event-stream`. In the split shape it runs in a `python:3.12-slim` image; in the
  single-process shape it runs from a small virtualenv, and the collector runs inside it as
  a background task.
- **UI:** static HTML, CSS and plain JavaScript served by the server. No build step, no
  framework.
- **Tests:** pytest. The collector's adapters are tested against fixture copies of real
  (scrubbed) `~/.claude` files, one set per Claude Code version we've seen.

## 5. One instance per machine

Each instance covers only the machine it runs on. Nothing is shared between machines in
this version: jobs.bix.computer shows bix's tasks, and the work laptop's dashboard stays on
its own `127.0.0.1`.

Keep the door open for linking machines later. Every `Task` carries a `machineId`, and the
API returns `{ machine, projects, tasks }`, so a later version can merge several instances'
`/api/state` into one view without changing the format. If that happens, instances
exchange task metadata only (name, status, project, `needs`), never transcript content:
transcripts hold employer code and data that must not leave a work machine. In
particular, a work instance never pushes to jobs.bix.computer.

## 6. Out of scope

- **Claude Code cloud and remote sessions** (claude.ai/code, Remote Control): no local
  file exists for these. Possible later via `ListAgents` from a scheduled `claude -p` job.
- **claude.ai chat Projects**: not running tasks.
- **Sessions run by other users.** On bix that includes the agent-runner sandbox
  (`bixagent`), whose files the collector cannot and should not read.
- **Acting on tasks** (replying, approving, stopping) from the dashboard. It is read-only.
  Any action would make the server a control surface and is a security decision to take
  separately (on bix: JWT verification and an `access` entry for the action path).
- Folders that match no project rule.

## 7. Risks

- **Internal file formats change.** Pin parsing to the fields we use, tolerate missing
  ones, log the Claude Code `version` from each session file, and show a "source format
  changed" warning instead of wrong tiles. Hooks are the fallback.
- **Exposure.** The API carries task names and `needs` lines. Single-process instances
  bind to `127.0.0.1` only. A tunnelled instance sits behind an identity check
  (Cloudflare Access on bix). Transcript text reaches the API only as the redacted context
  slices of §9, and only for listed tasks; nothing else from a transcript leaves the host.
- **Hostile text.** `name`, `detail` and `needs` are written by a model, which may have
  read untrusted input. The UI inserts them as text, never as HTML, and the server sends a
  strict CSP (no inline script, `default-src 'self'` plus the shared stylesheet origin).
- **Secrets leaking through the snapshot.** Adapters read an allow-list of fields (§1);
  a test asserts that the snapshot never contains keys outside the `Task` model.
- **Cost of reading transcripts.** Some are many MB. Read the tail by byte offset, cache
  the offset per file, never parse the whole file on a poll.
- **pid reuse and stale files.** Compare `procStart` and `pidDomain` as well as checking
  the pid exists.

## 8. Styling

Every instance uses the bix design system, including the work one: it is a personal tool,
and one look means one stylesheet to maintain. The rules below are the ones every bix
project follows (from `bix/CLAUDE.md`), plus what they mean for this page.

### Shared rules

- **Stylesheet.** Link `https://bix.computer/bix.css` first and style with its semantic
  tokens, always with a literal fallback so the page renders if the file can't load
  (offline, or a machine that can't reach bix.computer):
  `background: var(--bix-bg, #1e1e2e)`. Tokens in use here: `--bix-bg`, `--bix-bg-sunken`,
  `--bix-surface`, `--bix-border`, `--bix-border-strong`, `--bix-text`, `--bix-text-soft`,
  `--bix-text-muted`, `--bix-text-faint`, `--bix-accent`, `--bix-on-accent`, `--bix-ok`,
  `--bix-warn`, `--bix-danger`, `--bix-sans`, `--bix-mono`.
- **Palette.** Catppuccin only: Mocha by default, Latte for light mode via
  `[data-theme="light"]` (and `prefers-color-scheme`). No colours outside the palette.
- **One accent:** peach (`--bix-accent`), used sparingly: the title's full stop, links,
  focus rings, a primary button if one ever exists. Text on a peach fill uses
  `--bix-on-accent`, never white.
- **Type.** Helvetica Neue stack (`--bix-sans`). Monospace (`--bix-mono`) only for code,
  paths and numbers: branches, PIDs, costs, token counts, durations. Titles are weight
  300, tight tracking, ending in a peach full stop: `Jobs<i class="bix-dot">.</i>`.
- **Chrome.** A small grey breadcrumb top-left (`.bix-site`): `bix.computer / jobs` on
  bix, `<machine label> / jobs` elsewhere. No logo, no back link. Small uppercase labels
  (`.bix-label`), hairline dividers (`--bix-border`), corners 6–12px.
- **Favicon.** `<link rel="icon" type="image/svg+xml" href="https://bix.computer/favicon.svg">`.
  No per-project favicon file.
- **Never:** logos, emojis or icon glyphs standing in for words, cream backgrounds,
  italic accent words, "01 / 02" labels, pill buttons, badges floated above a title,
  centred heroes, grids of identical icon-on-top cards, coloured left/top-border accents,
  glassmorphism, glows, heavy shadows, gradients, stat banner rows.
- **Colourblind-safe status.** Matt is colourblind: status is never red/green alone. Every
  status has its own shape, so the page reads correctly in greyscale.
- Layout works at phone width: 16px side gutters, no horizontal scroll.

### This page

- **Layout (workbench, phase 1, built 2026-11-18).** Breadcrumb, then the title, then two
  columns from 900 px: the list on the left, the selected task's viewport on the right. Below
  900 px it's one column that shows either the list or the viewport (`All tasks` goes back).
  The page is one screen: both columns scroll inside themselves (`100dvh`).
  - The list is one section per project: a `.bix-label` heading (`BIX`, `WORK-A`, `WORK-B`)
    with a count in monospace, then its rows. Within a project, rows sort by status
    (`needs-you` first, then `stalled`, `working`, `idle`, `dead`, `done`) and then by last
    activity. Empty projects are hidden. A row is a link to `#task=<id>`, so a selection is
    shareable and survives a reload.
  - The viewport shows the task's **Goal** (job name, else the session's ai-title, else its
    first prompt), **Now** (the job's detail, else the latest assistant text), **Blocked** when
    it applies (`on you`, `on a usage limit`, or `on something else`, each with its mark and
    text label, then the reason), and **Context** (the last turns, read-only). **Progress**
    is the plan from the session's status card (below), when it has one: "N of M steps done",
    then each step with a shape and the word "done" or "to do". **Changes** waits for captures
    (workbench plan, phase 2's capture service).
- **Status cards (built 2026-11-18).** The session reports through one MCP tool,
  `report_status` (`status_mcp.py`, registered at user level as `jobs-status`). It takes the
  goal, the plan as `steps: [{text, done}]`, `now`, and `blocked: null | {on, why}` (`on` is
  `you`, `limit`, `ci`, `deploy`, `task` or `other`). The server finds its session as its
  parent process (`sessions/<ppid>.json`), so the model never passes a session id, and it
  writes `cards/<sessionId>.json` atomically. The collector reads each live session's card
  (`cardsDir`) and checks every field: a card wins over guesses for goal, now and blocked, a
  `blocked.on: you` card makes the task `needs-you`, and a card older than two hours is a
  guess again. Up to 20 steps; text is redacted and cut like the rest.
- **Tiles** are text-led, not icon cards: a `--bix-bg-sunken` panel with a hairline
  `--bix-border` and 8px radius. First line: the status mark, then the task name. Second line, muted: repo
  and branch (monospace), "N min ago" and cost. A `needs-you` tile adds the `needs` line in
  `--bix-text`; other tiles show `detail` in `--bix-text-muted`, one line, truncated.
  Status never changes the tile's border colour.
- **Status marks** (agreed 2026-11-18; may change later): one 12px mark before the name, using the
  suite's existing convention that a green dot means fine and a yellow "!" ring means it
  wants attention.

  | Status | Mark | Colour |
  |---|---|---|
  | `needs-you` | ring with "!" | `--bix-warn` |
  | `stalled` | hollow square | `--bix-warn` |
  | `working` | filled dot | `--bix-ok` |
  | `idle` | hollow dot | `--bix-text-muted` |
  | `dead` | cross | `--bix-danger` |
  | `done` | short dash; tile text dimmed | `--bix-text-faint` |

  Each mark carries a text label for screen readers (`aria-label="needs you"`). No
  pulsing or animation.
- **Source-format warning** (§7): a single line under the title in `--bix-warn` with the
  same "!" ring, naming the Claude Code version that changed.
- **Connection state.** When the SSE stream drops, a muted "reconnecting…" line under the
  title, and tiles keep their last values; never a blank page.

## 9. On bix (jobs.bix.computer)

How the split shape (§4) fits the bix suite's rules. Built 2026-11-18; `jobs/CLAUDE.md`
has the commands and gotchas.

- **Collector:** a host systemd unit `jobs-collector`, `User=matt`, hardened like
  `health-relay`: `TemporaryFileSystem=/home`, then read-only binds of only
  `~/.claude/sessions`, `~/.claude/jobs`, `~/.claude/projects`, `collector.py` and
  `config/bix.json`, read-write binds of the snapshot and spool directories, plus
  `PrivateNetwork=yes`, `ExecStart=/usr/bin/python3 -I …/collector.py`,
  `ProtectSystem=strict`, `NoNewPrivileges`, an empty capability set and a syscall filter.
  Installed by copy with `install-host.sh`.
- **Snapshot directory:** `~/jobs/snapshot/`, outside the apps tree (ai-router mounts that),
  holding only `state.json`. Mounted read-only into the container. The host never executes
  anything from it.
- **Server:** a compose service `jobs` built like rss and fetch: `python:3.12-slim` with a
  `test` stage (`docker build --target test` runs pytest), `<<: *hardened`, UID 1000,
  `read_only`, its own network (it needs no peers), port `127.0.0.1:3018`. uvicorn sets
  `FORWARDED_ALLOW_IPS` to its own network's `.1`. It does not mount `~/.claude`.
- **Access:** tunnel hostname `jobs.bix.computer` → `http://localhost:3018`, Cloudflare
  Access in front, `access = ["jobs.bix.computer"]`. The app also checks the Access JWT
  on every route but `/healthz`, as fetch and remote do, so it fails closed if the Access
  app is missing or the tunnel route is added first. A laptop instance runs with
  `LOCAL_ONLY=1` instead: no JWT, and only `localhost`/`127.0.0.1` Host headers.
- **Project rule:** `~/apps` → `bix`, subproject as the repo label (§2).
- **Logs:** one event per line, `<UTC timestamp> <LEVEL> <component> <message>`, problems
  as `WARNING`/`ERROR` only. Source-format changes log `WARNING`. No task names or `needs`
  text in logs.
- **Registry entry** in `bix-infra/services.toml`, gated:

  ```toml
  [[service]]
  id      = "jobs"
  name    = "Jobs"
  home    = "gated"
  sidebar = "services"
  url     = "https://jobs.bix.computer/"
  caption = "Every running Claude task on bix, and which ones need me."
  access  = ["jobs.bix.computer"]
  logs    = [
    { kind = "app",     dir = "jobs" },
    { kind = "docker",  container = "apps-jobs-1", dir = "jobs", ignore = '…' },
    { kind = "journal", unit = "jobs-collector", dir = "jobs-collector" },
  ]
  ```

  `deploy = "jobs"` follows once `jobs/` is a git repo (the nightly rebuild pulls one).
  Then the rest of the adding-a-service checklist in `bix-infra/CLAUDE.md` (homepage
  regen, metrics `gen` entry, port row).
- **Nothing new to install.** The collector uses the host's `/usr/bin/python3` (3.12), as
  `health-relay` does; the server's dependencies live only in its image.

- **Context slices (workbench viewport, built 2026-11-18).** The collector writes
  `~/jobs/context/<sessionId>.json` (`contextDir` in the config) whenever a transcript
  changes: the last six user and assistant turns that carry text. Tool calls and tool results
  never enter a slice. Each turn is cut to 600 characters and passed through best-effort
  secret redaction first (key=value secrets, `sk-`/`gh*_`/`AKIA`/`xox*` tokens, and long
  mixed letter-and-digit runs). Slices of tasks no longer shown are deleted on the next
  snapshot. The directory is mounted read-only into `jobs` (`JOBS_CONTEXT`), and
  `/api/tasks/<id>/context` answers only for ids in the current snapshot whose ids are file
  names. Transcript text reaches the web only through this route, behind Access
  (workbench plan decision 2). The same redaction runs on goal and now.

## 9a. The workbench broker (phase 3, built 2026-11-19)

Plan: `bix-ai/PLAN-workbench.md` §4. The broker is `broker/broker.py`, a host daemon
running as matt, so the sessions it starts are matt's sessions. It is the only thing that
starts, resumes or stops Claude Code for the web.

- **Two unix sockets, nothing on TCP.** `~/jobs/broker/broker.sock` takes the web actions
  (`start`, `send`, `events`, `approve`, `stop`, `release`, `take_over`). `approval.sock`
  takes only `approval_request`, from `approval.py`. Each refuses the other's actions. Only
  the web socket is ever mounted into a container. A container holding a ticket therefore
  can't approve its own tool calls.
- **Access checked by the broker.** Every web action carries the visitor's Access JWT
  (`cf-access-jwt-assertion`, forwarded by the jobs route). The broker verifies signature,
  audience, issuer and expiry itself, as `auth.py` does. Anything else is a 403. The
  broker won't start until `aud` is set in `config/broker.json`.
- **One driver per session.** The web driver is one `claude -p` stream-json process per
  session, with `--resume` (or `--session-id` for a new one). A terminal holding the session
  is refused, until `take_over`. `take_over` only sends SIGTERM when the session file says
  `idle` and the transcript has been quiet for 10 s, since the session file can lag mid-turn.
  The conversation stays on disk, and the next `send` resumes it (tested live, see the PR).
- **Permissions.** Each driver gets `--permission-prompt-tool mcp__jobs-approval__approve`
  and `--permission-prompts host`. A tool call that needs approval goes to `approval.py`,
  which blocks on the broker until the visitor allows or denies. A timeout, an unreachable
  broker, or an unclear reply is a deny.
- **Event numbers outlive the process.** The broker keeps each session's recent events in a
  ring, numbered across drivers, so a client's cursor survives a stop and a resume.
- **Send is idempotent.** A client id seen before is accepted without being delivered twice.
  A send while the session is answering is refused (409).
- **Working directory from disk.** The client never names a directory. The broker takes the
  session's cwd from its session file or from its transcript, and checks it against the
  allowed roots.
- **Shown to the web:** text, the names of tools and whether they failed. Tool inputs and
  outputs stay on the host, except approval summaries (the command or path, redacted). Text
  deltas are redacted one at a time, so a secret split across two deltas can slip through
  until the full message arrives: a known, narrow gap.
- **Audit.** One line per action: time, action, session, client id (truncated), outcome.
  Never the message text.
- **Probe.** `broker/probe.py` checks the front door of a running broker (socket modes,
  refusals on both sockets, no TCP listener), like `agent-sandbox-check.py`.
- **Deploy.** `sudo bash install-broker.sh` installs `jobs-broker.service` and starts it
  once `aud` is set. The Access app for the workbench origin is created in the dashboard.

## 10. Build order

1. Collector reading `sessions/` and `jobs/`, project mapper from `cwd`, snapshot, and the
   single-process server with `/api/state`. A plain JSON page already answers "what's
   running and what needs me".
2. SSE push, liveness and staleness, git branch and worktree mapping.
3. Transcript tail: last activity, cost.
4. UI: tiles grouped by project, styled per §8.
5. Hooks spool, service units (launchd; on bix the systemd unit, container, tunnel and
   Access), usage limits panel.
6. Later, if wanted: instances on different machines talking to each other; cloud
   sessions.

## 11. Hold-up metrics

Agreed 2026-11-18. jobs.bix tracks how often tasks get held up, what holds them, and how
each hold was released. It covers every driver (web, terminal, Claude app, background),
because a status change looks the same to the collector whoever drives the session.

### Episodes

A **hold** starts when a task enters `needs-you`, a blocked-on-something-else state, or
`stalled`. It ends when the task goes back to `working`, or reaches `done`, `idle` after a
reply, or ends. The collector writes one line per finished hold:

```
{ id, sessionId, projectId, kind, engine, model, driver,
  startedAt, endedAt, heldMs,
  category, reason, release, releasedBy }
```

- **category / reason:**
  - `you`: `question` · `permission` · `review` · `sudo`
  - `limit`: `usage-limit` · `rate-limit`
  - `external`: `ci` · `deploy` · `other-task` · `network`
  - `stalled`: `unknown`

  The reason comes from, in order: a status card's `blocked.on` (workbench plan, phase
  2), a permission prompt (hook `Notification` or the broker's prompt tool), the job's
  `needs` text (for example "rate limited" → `limit/rate-limit`), else `you/question` for
  `needs-you` and `stalled/unknown` for `stalled`.
- **release / releasedBy:** how the hold ended, and whether you or something else ended it.
  - By you: `replied-web` · `replied-terminal` · `replied-app` · `approved` · `denied`.
  - Automatic: `limit-reset` · `external-cleared` · `resumed-by-itself`.
  - Neither: `abandoned` (the session ended or was killed while held) · `expired` (the
    job aged out unanswered).
- **Where release signals come from:**
  - The broker writes a spool event when you reply or approve from the web.
  - The `UserPromptSubmit` hook in a terminal or Claude app session gives
    `replied-terminal` / `replied-app`, decided by that session's driver.
  - A status change with no user event in between counts as automatic. A session that
    disappears while held counts as `abandoned`.

No text is stored: no `needs` line, no reply. Ids and categories are enough to count, and
names are joined from the live snapshot only when the page is shown.

### Storage

The collector is the only writer. It appends to `holds-YYYY-MM.jsonl` under the snapshot's
sibling directory (`~/jobs/history/` on bix). JSONL, not SQLite, because the server
reads it through a read-only mount, and SQLite's WAL needs write access to read. A hold
still open when the collector restarts is recovered from the next snapshot (the task is
still held). If it can't be recovered, it closes as `abandoned` with a `WARNING`.

### What's shown

- **jobs.bix `/holds`:** for the last 7 and 30 days:
  - the number of holds and the total time held;
  - median and longest time to release per category;
  - the split of release methods (you vs automatic, and web / terminal / app);
  - the share of holds on you that were permission prompts.

  Tables first, plus one bar chart per question. Categories carry the same status marks
  as the tiles, so colour is never the only signal.
- **The workbench viewport:** this task's own holds so far.
- **The metrics dashboard:** `gen["jobs"]` gets weekly holds by category and median
  time to release, for the nightly view.

### Accuracy

- Without hooks, interactive sessions can't show permission prompts, so those holds are
  missed or land under `stalled`.
- Without status cards, `external` reasons are rare, and `limit` is recognised only from
  `needs` text.

The page says which signals are missing, rather than looking confident.


# jobs

Jobs (jobs.bix.computer, port 3018): every running Claude Code task on this
machine, grouped by project, one tile each with a status shape. `DESIGN.md` is
the design: data sources, status rules, the two deployment shapes and the
styling. The same code runs on the work laptop as a single process, so nothing
bix-specific goes in code: it lives in `config/bix.json`, compose and the unit.

## Commands

- Tests: `docker build --target test .` (the Dockerfile's test stage gates the
  image, so `docker compose build jobs` from `bix-infra/` also runs them).
- Deploy the server: from `bix-infra/`, `docker compose build jobs && docker compose up -d jobs`.
- Deploy the collector: `sudo bash install-host.sh` (copies the unit and
  restarts it). Edits to `collector.py` or `config/bix.json` need
  `sudo systemctl restart jobs-collector`.
- One snapshot, without consuming hook events:
  `python3 -I collector.py --config config/bix.json --once`.
- Laptop (single process): a venv with `requirements.txt`, then
  `JOBS_CONFIG=<config.json> LOCAL_ONLY=1 uvicorn web:main_app --factory --host 127.0.0.1 --port 3018`.

## Modules

| File | Does |
|------|------|
| `collector.py` | Stdlib only, Python 3.9 syntax, one file (`python3 -I` can't import siblings). Reads `sessions/`, `jobs/*/state.json`, transcript tails (`gitBranch`, `cost-state`, the viewport's goal/now/turns) and the hook spool; maps cwd → project; derives the status; writes `state.json` atomically and the per-task context slices (`contextDir`). Copies fields by name into `TASK_KEYS`, never whole records. Redaction for viewport text is `redact()` |
| `hook.py` | Claude Code hook → one JSON file per event in the spool. Keeps event, session id, time, and for Notification its type and message only. Exit 1 on failure (non-blocking) |
| `web.py` | FastAPI: `/` + `/static/app.{js,css}`, `/api/state`, `/api/tasks/<id>/context` (a listed task's turn slice), `/api/events` (SSE, full view on change, keepalive 15 s), `/healthz` (503 when the snapshot is older than `STALE_SECONDS`). Follows the snapshot file (`JOBS_SNAPSHOT`) or runs the collector in a thread (`JOBS_CONFIG`). Context slices come from `JOBS_CONTEXT` |
| `auth.py` | Access JWT on every route but `/healthz`; unset `ACCESS_AUD` refuses all. `LOCAL_ONLY=1` (laptop) skips it and only accepts Host `localhost`/`127.0.0.1` |
| `static/` | No build step. All server text goes in via `textContent` (a test checks there's no `innerHTML`) |
| `systemd/jobs-collector.service` | The hardened host unit; see Gotchas |

## Gotchas

- **The container never sees `~/.claude`.** Only the host unit reads it, with
  an empty `/home`, read-only binds of `sessions`, `jobs`, `projects` and no
  network. Keep it that way (suite rule: never mount the host `~/.claude`).
- **Those binds pin directory inodes.** If Claude Code ever recreates one of
  the three directories, restart `jobs-collector`.
- **Names stay out of logs.** `logs/` is readable by ai-router and the agent
  sandbox: log file names and versions, never task names or `needs` text. The
  snapshot (`~/jobs/snapshot`, outside the apps tree) is the only place they are.
- **Old job files never go away.** `jobs/<id>/state.json` from months ago
  still says `working`/`blocked`, so a job without a live session is shown only
  while its `updatedAt` is within `jobMaxAgeHours` (7 days).
- **Session `status` lags.** On 2.1.327 a session can say `idle` mid-turn, so
  an `idle` session whose transcript was written >10 s after its status change
  counts as working. Hooks give a better signal once installed.
- **Context slices are the only transcript text on the web.** Written only when a
  transcript changes, deleted when a task leaves the snapshot, and served only for
  listed ids. The collector's `contextDir` and the container's `JOBS_CONTEXT` mount
  both need `sudo bash install-host.sh` (creates `~/jobs/context`) before the
  compose `jobs` service can start with the mount.
- **`--once` doesn't drain the spool**, so it's safe while the unit runs.
- **Cost appears late on a fresh start.** `cost-state` is read only from the
  last 256 KB of a transcript and from what's appended after.
- Snapshot format is versioned (`SNAPSHOT_VERSION`); the server refuses a
  snapshot of another version and keeps the last good one.

## Deployment

Port 3018: jobs (jobs.bix.computer). Container `jobs` on its own network
`jobs` (172.30.18.0/24, egress only for Access's signing keys), Access in front
plus an in-app JWT check (`ACCESS_AUD` in compose). Host unit
`jobs-collector` (journal → `logs/jobs-collector/`) writes
`/home/matt/jobs/snapshot/state.json`; hooks write `/home/matt/jobs/spool`.

---
name: worktree-orchestrator
description: Orchestrate parallel Claude Code sessions across git worktrees with multi-server support, cross-server port references, and secrets management. Trigger this skill whenever the user wants to spin up parallel worktrees, manage multiple feature branches simultaneously, run dev servers (frontend, backend, or both) for different branches, spawn a worktree for a GitHub issue, check status of running sessions, view server logs, clean up finished worktrees, or says things like "spawn a worktree", "start a parallel session", "work on issue X in a new worktree", "show my active worktrees", "kill that session", "show me the logs", "clean up worktrees", or "set up the orchestrator". Also trigger when the user mentions working on multiple features in parallel or references ".orchestrator" config.
---

# Worktree Orchestrator

Manage parallel git worktree sessions - each with its own branch, dev servers, and working directory. Supports multi-server setups where servers need to know each other's ports (e.g. a Flutter frontend that needs the backend URL).

## Prerequisites

- Git repository with a remote
- Python 3.9+ (stdlib only, no pip installs)

## Quick Reference

| Command | What it does |
|---------|-------------|
| `init` | Creates `.orchestrator.toml`, `.orchestrator/.secrets`, updates `.gitignore` |
| `spawn <n>` | Creates worktree + branch, starts all servers, opens Claude in a new terminal |
| `status` | Shows all sessions with per-server health (UP/DOWN) |
| `logs <session> [server]` | Shows server logs. Omit server name to see all. |
| `kill <session> [--remove]` | Stops all servers. `--remove` also deletes the worktree. |
| `restart <session>` | Stops all servers and starts them again with fresh ports |
| `cleanup [--force]` | Removes all stopped sessions and their worktrees |

## Key Design Decisions

**Ports are auto-assigned by the OS.** No port ranges, no manual allocation, no collisions across projects.

**Servers can reference each other's ports.** Use `{backend.port}` and `{frontend.port}` in start_command and env overrides. All ports are allocated before any server starts.

**Worktrees are project-scoped.** Created at `../worktrees/<project-name>/<session>`. Different projects never interfere.

**Spawn opens Claude automatically.** After starting servers, `spawn` opens a new terminal window with Claude Code running in the worktree directory. The orchestration terminal stays free for `status`, `logs`, `kill`, etc. Use `--no-claude` to skip this.

**Two files, clean separation:**
- `.orchestrator.toml` - run config (commands, port wiring, non-secret env). Safe to commit.
- `.orchestrator/.secrets` - secrets (DB creds, API keys). Gitignored automatically.

Secrets are loaded into every server's environment. Per-server `[servers.X.env]` overrides are applied on top with port substitution.

## First: Check If Already Configured

Before doing anything, check whether the orchestrator is already set up in this project:

```bash
ls -la .orchestrator.toml .orchestrator/ 2>/dev/null
```

- If `.orchestrator.toml` exists, the project is already configured. Skip init and go straight to the command the user asked for (spawn, status, kill, etc.).
- If it does not exist, run the setup flow below.

## Setup Flow

Only run this when `.orchestrator.toml` does not exist yet.

1. Locate the script bundled with this skill:
   ```bash
   ORCH="<path-to-this-skill>/scripts/orchestrator.py"
   ```

2. Run init from the repo root:
   ```bash
   python "$ORCH" init
   ```

3. This creates two things to configure:

**First, add secrets to `.orchestrator/.secrets`** (this file is gitignored):
```
DATABASE_URL=postgres://user:pass@localhost:5432/mydb
GOOGLE_CLIENT_ID=your-client-id
GOOGLE_CLIENT_SECRET=your-client-secret
```

> **Never put secrets in `.orchestrator.toml`** — it's meant to be committed. Secrets go in `.orchestrator/.secrets` only.

**Then, configure servers in `.orchestrator.toml`**. Two typical patterns:

Simple project (portfolio, landing page):
```toml
[servers.web]
start_command = "npm run dev -- --port {port}"
```

Frontend + backend with shared ports (commands must run non-interactively):
```toml
[servers.backend]
start_command = "dart run bin/server.dart"
directory = "server"

[servers.backend.env]
PORT = "{backend.port}"
FRONTEND_URL = "http://localhost:{frontend.port}"
ALLOWED_ORIGIN = "http://localhost:{frontend.port}"
DEV_MODE = "true"

[servers.frontend]
start_command = "flutter run -d web-server --web-port={frontend.port} --dart-define=API_BASE_URL=http://localhost:{backend.port}"
```

> **Note:** Commands must run non-interactively (no TTY). For Flutter, use `-d web-server` instead of `-d chrome`.

## Port Placeholder Reference

In `start_command` and `[servers.X.env]` values:

| Placeholder | Resolves to |
|-------------|-------------|
| `{port}` | The current server's own auto-assigned port |
| `{backend.port}` | The server named "backend"'s port |
| `{frontend.port}` | The server named "frontend"'s port |
| `{<n>.port}` | Any server's port, by its section name |

## Environment Resolution Order

For each server, the process environment is built as:

1. **Inherit** the current shell environment
2. **Load** `.orchestrator/.secrets` (shared across all servers)
3. **Apply** `[servers.<n>.env]` overrides with port substitution

Later steps override earlier ones. Secrets provide the base (DB, API keys), env overrides wire up the dynamic ports.

## Spawning, Logs, Status, Kill, Cleanup

```bash
python "$ORCH" spawn 42              # create session "42" + open Claude in new terminal
python "$ORCH" spawn 42 --no-claude  # create session without opening Claude
python "$ORCH" status                # show all sessions
python "$ORCH" logs 42               # all server logs
python "$ORCH" logs 42 backend       # just backend
python "$ORCH" kill 42               # stop servers
python "$ORCH" kill 42 --remove      # stop + delete worktree
python "$ORCH" restart 42            # stop + restart with fresh ports
python "$ORCH" cleanup --force       # remove all stopped sessions
```

## Config Reference

Read `references/config-schema.md` for the full `.orchestrator.toml` format.

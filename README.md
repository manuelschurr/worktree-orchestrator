# Worktree Orchestrator

A [Claude Code skill](https://docs.anthropic.com/en/docs/claude-code/skills) for managing parallel git worktree sessions — each with its own branch, dev servers, and working directory.

Supports multi-server setups where servers need to know each other's ports (e.g. a Flutter frontend that needs the backend URL).

## Features

- **Auto-assigned ports** — no port ranges, no manual allocation, no collisions
- **Cross-server port references** — use `{backend.port}` in frontend config and vice versa
- **Secrets management** — `.orchestrator/.secrets` (gitignored) loaded into every server's environment
- **Session lifecycle** — spawn, status, logs, kill, restart, cleanup
- **Cross-platform** — Windows, macOS, Linux
- **Zero dependencies** — Python 3.9+ stdlib only

## Prerequisites

- Git repository with a remote
- Python 3.9+
- [Claude Code](https://claude.ai/claude-code) with skills support

## Installation

**Per-project** — available only in one project:

```bash
cd your-project/.claude/skills
git clone https://github.com/manuelschurr/worktree-orchestrator.git
```

Or as a git submodule (so collaborators get it too):

```bash
cd your-project
git submodule add https://github.com/manuelschurr/worktree-orchestrator.git .claude/skills/worktree-orchestrator
```

**Global** — available in all your projects:

```bash
cd ~/.claude/skills
git clone https://github.com/manuelschurr/worktree-orchestrator.git
```

## Quick Start

1. Ask Claude Code to set up the orchestrator (or run `python .claude/skills/worktree-orchestrator/scripts/orchestrator.py init`)
2. Add secrets to `.orchestrator/.secrets`
3. Configure servers in `.orchestrator.toml`
4. Ask Claude Code to "spawn a worktree for issue 42"

## Commands

| Command | What it does |
|---------|-------------|
| `init` | Creates `.orchestrator.toml`, `.orchestrator/.secrets`, updates `.gitignore` |
| `spawn <n>` | Creates worktree + branch, starts all servers, registers session |
| `status` | Shows all sessions with per-server health (UP/DOWN) |
| `logs <session> [server]` | Shows server logs. Omit server name to see all. |
| `kill <session> [--remove]` | Stops all servers. `--remove` also deletes the worktree. |
| `restart <session>` | Stops all servers and starts them again with fresh ports |
| `cleanup [--force]` | Removes all stopped sessions and their worktrees |

## Example Config

**`.orchestrator.toml`** (safe to commit):

```toml
[project]
remote = "origin"
base_branch = "main"
branch_prefix = "feature/issue-"

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

**`.orchestrator/.secrets`** (gitignored):

```
DATABASE_URL=postgres://user:pass@localhost:5432/mydb
GOOGLE_CLIENT_ID=your-client-id
GOOGLE_CLIENT_SECRET=your-client-secret
```

## Port Placeholders

Use these in `start_command` and `[servers.X.env]` values:

| Placeholder | Resolves to |
|-------------|-------------|
| `{port}` | The current server's own auto-assigned port |
| `{backend.port}` | The server named "backend"'s port |
| `{frontend.port}` | The server named "frontend"'s port |
| `{<name>.port}` | Any server's port, by its section name |

## How It Works

1. **`init`** creates config files and gitignore entries
2. **`spawn`** creates a git worktree + branch, allocates ports for all servers, loads secrets, starts each server with the resolved environment, and registers the session
3. **`status`** checks if each server's PID is still alive
4. **`restart`** kills all servers and starts them fresh with new ports on the same worktree
5. **`kill --remove`** stops servers, waits for file locks to release, removes the worktree and logs

Worktrees are created at `../worktrees/<project-name>/<session>` so different projects never interfere.

## Environment Resolution Order

For each server, the process environment is built as:

1. Inherit the current shell environment
2. Load `.orchestrator/.secrets` (shared across all servers)
3. Apply `[servers.<name>.env]` overrides with port substitution

See [references/config-schema.md](references/config-schema.md) for the full config format.

## License

[MIT](LICENSE)

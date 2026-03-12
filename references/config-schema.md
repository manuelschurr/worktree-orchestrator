# Config Schema

The orchestrator uses two config files, both created by `init`:

## `.orchestrator/.secrets`

Flat dotenv file for secrets. Loaded into every server's environment.
Lives inside `.orchestrator/` which is gitignored.

```
DATABASE_URL=postgres://user:pass@localhost:5432/mydb
GOOGLE_CLIENT_ID=your-client-id
GOOGLE_CLIENT_SECRET=your-client-secret
```

Format: `KEY=value`, one per line. Lines starting with `#` are comments.
Quotes around values are optional and stripped if present.

## `.orchestrator.toml`

Run config. Lives in the repo root. Safe to commit — **never put secrets here** (use `.orchestrator/.secrets` instead).

### Full Example (frontend + backend)

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
GOOGLE_AUTH_REDIRECT_URI = "http://localhost:{backend.port}/api/auth/google/callback"
DEV_MODE = "true"

[servers.frontend]
start_command = "flutter run -d web-server --web-port={frontend.port} --dart-define=API_BASE_URL=http://localhost:{backend.port} --dart-define=DEV_MODE=true"
```

### Minimal Example (single server)

```toml
[project]
base_branch = "main"

[servers.web]
start_command = "npm run dev -- --port {port}"
```

### `[project]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `remote` | string | `"origin"` | Git remote to fetch from |
| `base_branch` | string | auto-detected | Branch to base new worktrees on |
| `branch_prefix` | string | `"feature/issue-"` | Prefix for branch names |

### `[servers.<n>]`

Each section defines one server process. The name (e.g. `backend`, `frontend`, `web`) is used in status output, log filenames, and port placeholders.

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `start_command` | string | yes | Shell command to start the server |
| `directory` | string | no | Subdirectory to run from, relative to worktree root |

### `[servers.<n>.env]`

Key-value pairs that set/override environment variables for this server. Applied after secrets are loaded. All values support port placeholders.

### Port Placeholders

| Placeholder | Description |
|-------------|-------------|
| `{port}` | This server's own auto-assigned port |
| `{<n>.port}` | Named server's port (e.g. `{backend.port}`, `{frontend.port}`) |

### Common start_command examples

| Stack | Command |
|-------|---------|
| Dart Shelf | `"dart run bin/server.dart"` (port via env) |
| Flutter web | `"flutter run -d web-server --web-port={frontend.port}"` |
| Node/npm | `"npm run dev -- --port {port}"` |
| Next.js | `"next dev -p {port}"` |
| Python Django | `"python manage.py runserver 0.0.0.0:{port}"` |
| Python FastAPI | `"uvicorn main:app --port {port}"` |
| Rails | `"rails server -p {port}"` |
| Go | `"go run . --port {port}"` |

## Environment Resolution Order

1. Current shell environment (inherited)
2. `.orchestrator/.secrets` (shared across all servers)
3. `[servers.<n>.env]` overrides (per-server, with port substitution)

## File Layout

```
project/
  .orchestrator.toml        # run config (committable)
  .orchestrator/             # gitignored
    .secrets                 # secrets (dotenv)
    sessions.json            # session state
    logs/
      42/
        backend.log
        frontend.log
```

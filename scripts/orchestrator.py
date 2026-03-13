#!/usr/bin/env python3
"""
Worktree Orchestrator - manage parallel git worktree sessions.

Cross-platform (Windows, macOS, Linux). Python 3.9+ stdlib only.
"""

import argparse
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Fix stdout/stderr encoding on Windows cp1252 consoles (Unicode spinners crash print())
# ---------------------------------------------------------------------------
if hasattr(sys.stdout, "reconfigure") and sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure") and sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# TOML parser (stdlib-only, supports nested tables)
# ---------------------------------------------------------------------------

def parse_toml(text: str) -> dict:
    try:
        import tomllib
        return tomllib.loads(text)
    except ImportError:
        pass

    result = {}
    current_path = []
    current_array_key = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # [[array.of.tables]]
        if line.startswith("[[") and line.endswith("]]"):
            section = line[2:-2].strip()
            parts = section.split(".")
            current_array_key = parts[-1]
            parent = result
            for part in parts[:-1]:
                if part not in parent:
                    parent[part] = {}
                parent = parent[part]
            if current_array_key not in parent:
                parent[current_array_key] = []
            parent[current_array_key].append({})
            current_path = parts
            continue

        # [table]
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            current_path = section.split(".")
            current_array_key = None
            d = result
            for part in current_path:
                if part not in d:
                    d[part] = {}
                elif isinstance(d[part], list):
                    pass
                d = d[part] if not isinstance(d[part], list) else d[part][-1]
            continue

        if "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            # Strip inline comments
            if "#" in val:
                in_str = False
                for i, ch in enumerate(val):
                    if ch == '"':
                        in_str = not in_str
                    elif ch == "#" and not in_str:
                        val = val[:i].strip()
                        break
            if val.startswith('"') and val.endswith('"'):
                parsed = val[1:-1]
            elif val.isdigit():
                parsed = int(val)
            elif val.lower() in ("true", "false"):
                parsed = val.lower() == "true"
            else:
                parsed = val

            # Navigate to the correct target dict
            d = result
            for part in current_path:
                if isinstance(d.get(part), list):
                    d = d[part][-1]
                else:
                    d = d[part]
            d[key] = parsed

    return result


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IS_WINDOWS = platform.system() == "Windows"
CONFIG_FILENAME = ".orchestrator.toml"
STATE_DIR = ".orchestrator"
SESSIONS_FILE = "sessions.json"
SECRETS_FILE = ".secrets"
LOGS_DIR = "logs"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("Error: not inside a git repository.", file=sys.stderr)
        sys.exit(1)
    return Path(result.stdout.strip())


def project_name(repo_root: Path) -> str:
    return repo_root.name


def load_config(repo_root: Path) -> dict:
    config_path = repo_root / CONFIG_FILENAME
    if not config_path.exists():
        print(f"Error: {CONFIG_FILENAME} not found. Run 'init' first.", file=sys.stderr)
        sys.exit(1)
    raw = parse_toml(config_path.read_text(encoding="utf-8"))

    project = raw.get("project", {})
    servers_raw = raw.get("servers", {})

    servers = []
    for name, cfg in servers_raw.items():
        if not isinstance(cfg, dict):
            continue
        cmd = cfg.get("start_command", "")
        if not cmd:
            continue
        servers.append({
            "name": name,
            "start_command": cmd,
            "directory": cfg.get("directory", ""),
            "env": {k: v for k, v in cfg.get("env", {}).items() if isinstance(v, str)},
        })

    return {
        "remote": project.get("remote", "origin"),
        "base_branch": project.get("base_branch", "main"),
        "branch_prefix": project.get("branch_prefix", "feature/issue-"),
        "servers": servers,
    }


def orch_dir(repo_root: Path) -> Path:
    d = repo_root / STATE_DIR
    d.mkdir(exist_ok=True)
    return d


def session_logs_dir(repo_root: Path, session_name: str) -> Path:
    d = orch_dir(repo_root) / LOGS_DIR / session_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_sessions(repo_root: Path) -> dict:
    path = orch_dir(repo_root) / SESSIONS_FILE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_sessions(repo_root: Path, sessions: dict):
    path = orch_dir(repo_root) / SESSIONS_FILE
    path.write_text(json.dumps(sessions, indent=2), encoding="utf-8")


def parse_dotenv(path: Path) -> dict:
    """Parse a dotenv file into a dict."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        eq = line.find("=")
        if eq > 0:
            key = line[:eq].strip()
            val = line[eq + 1:].strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            env[key] = val
    return env


def load_secrets(repo_root: Path) -> dict:
    """Load secrets from .orchestrator/.secrets (dotenv format)."""
    return parse_dotenv(orch_dir(repo_root) / SECRETS_FILE)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def is_process_alive(pid) -> bool:
    if pid is None:
        return False
    pid = int(pid)
    try:
        if IS_WINDOWS:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True
            )
            return str(pid) in result.stdout
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False


def kill_process(pid):
    if pid is None:
        return
    pid = int(pid)
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                time.sleep(0.3)
                try:
                    os.kill(pid, 0)
                except OSError:
                    return
            os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def detect_base_branch(remote: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{remote}/main"],
        capture_output=True, text=True
    )
    return "main" if result.returncode == 0 else "master"


def ensure_gitignore(repo_root: Path):
    """Add .orchestrator/ to .gitignore if not already present."""
    gitignore = repo_root / ".gitignore"
    marker = STATE_DIR + "/"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if marker in existing:
        return
    addition = "\n# Worktree orchestrator state and secrets\n" + marker + "\n"
    if existing:
        if not existing.endswith("\n"):
            existing += "\n"
        gitignore.write_text(existing + addition, encoding="utf-8")
    else:
        gitignore.write_text(addition.lstrip("\n"), encoding="utf-8")
    print(f"Added {marker} to .gitignore")


def worktree_base_dir(repo_root: Path) -> Path:
    return (repo_root.parent / "worktrees" / project_name(repo_root)).resolve()


def substitute_vars(text: str, port_map: dict, current_server: str = "") -> str:
    """Replace port placeholders in a string.

    Supports:
      {port}             - current server's own port
      {servername.port}  - named server's port
    """
    for srv_name, port in port_map.items():
        text = text.replace(f"{{{srv_name}.port}}", str(port))
    if current_server and current_server in port_map:
        text = text.replace("{port}", str(port_map[current_server]))
    return text


def open_terminal_with_claude(worktree_path: Path, session_name: str):
    """Open a new terminal window running claude in the worktree directory."""
    wt_str = str(worktree_path)

    # Strip CLAUDECODE env var so the new terminal isn't detected as a nested session
    clean_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    try:
        if IS_WINDOWS:
            # Try Windows Terminal first, fall back to cmd
            wt_check = subprocess.run(
                ["where", "wt"], capture_output=True, text=True
            )
            if wt_check.returncode == 0:
                subprocess.Popen(
                    ["wt", "-w", "new", "-d", wt_str,
                     "--title", f"claude [{session_name}]",
                     "cmd", "/k", "claude"],
                    env=clean_env,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                subprocess.Popen(
                    f'start "claude [{session_name}]" cmd /k "cd /d {wt_str} && claude"',
                    shell=True,
                    env=clean_env,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                )
        elif platform.system() == "Darwin":
            # macOS: use AppleScript to open Terminal.app — unset CLAUDECODE inline
            script = (
                f'tell application "Terminal"\n'
                f'  do script "unset CLAUDECODE && cd {wt_str} && claude"\n'
                f'  activate\n'
                f'end tell'
            )
            subprocess.Popen(["osascript", "-e", script])
        else:
            # Linux: try common terminal emulators
            for term_cmd in [
                ["gnome-terminal", "--", "bash", "-c", f"unset CLAUDECODE && cd {wt_str} && claude; exec bash"],
                ["xfce4-terminal", "-e", f"bash -c 'unset CLAUDECODE && cd {wt_str} && claude; exec bash'"],
                ["konsole", "-e", "bash", "-c", f"unset CLAUDECODE && cd {wt_str} && claude; exec bash"],
                ["xterm", "-e", f"bash -c 'unset CLAUDECODE && cd {wt_str} && claude; exec bash'"],
            ]:
                try:
                    subprocess.Popen(term_cmd, env=clean_env)
                    break
                except FileNotFoundError:
                    continue
            else:
                print(f"  Could not detect terminal emulator. Run manually:")
                print(f"    cd {wt_str} && claude")
                return

        print(f"  Opened new terminal with claude in {wt_str}")
    except Exception as e:
        print(f"  Could not open terminal: {e}")
        print(f"  Run manually: cd {wt_str} && claude")


def _rmtree_robust(path: Path, retries: int = 3, delay: float = 1.0) -> bool:
    """Remove a directory tree with retry logic for Windows file locks.

    Returns True if the directory was fully removed, False otherwise.
    """
    import shutil

    def _onerror(func, fpath, exc_info):
        try:
            os.chmod(fpath, 0o777)
            func(fpath)
        except OSError:
            pass

    for attempt in range(retries):
        try:
            shutil.rmtree(path, onerror=_onerror)
            if not path.exists():
                return True
        except OSError:
            pass
        if attempt < retries - 1:
            time.sleep(delay)
    print(f"Warning: could not fully remove {path} after {retries} attempts.", file=sys.stderr)
    return False


def is_valid_worktree(repo_root: Path, wt_path: Path) -> bool:
    """Check if a directory is a valid git worktree."""
    if not (wt_path / ".git").exists():
        return False
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True, text=True, cwd=repo_root
    )
    return str(wt_path.resolve()) in result.stdout


SECRET_PATTERNS = ["DATABASE_URL=", "_SECRET=", "_KEY=", "_PASSWORD=", "_TOKEN="]


def validate_no_secrets_in_config(repo_root: Path):
    """Warn if .orchestrator.toml appears to contain secret values."""
    config_path = repo_root / CONFIG_FILENAME
    if not config_path.exists():
        return
    content = config_path.read_text(encoding="utf-8")
    found = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pattern in SECRET_PATTERNS:
            if pattern in stripped.upper():
                # Check it's not just a placeholder reference like {backend.port}
                eq_idx = stripped.find("=")
                if eq_idx > 0:
                    val = stripped[eq_idx + 1:].strip().strip('"').strip("'")
                    if val and not val.startswith("{") and not val.startswith("#"):
                        found.append(stripped)
                break
    if found:
        print("=" * 60, file=sys.stderr)
        print("WARNING: Possible secrets in .orchestrator.toml!", file=sys.stderr)
        print("Secrets should go in .orchestrator/.secrets instead.", file=sys.stderr)
        print("", file=sys.stderr)
        for line in found:
            print(f"  {line}", file=sys.stderr)
        print("", file=sys.stderr)
        print("Move these to .orchestrator/.secrets and remove", file=sys.stderr)
        print("them from .orchestrator.toml.", file=sys.stderr)
        print("=" * 60, file=sys.stderr)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    repo_root = find_repo_root()
    config_path = repo_root / CONFIG_FILENAME

    if config_path.exists() and not args.force:
        print(f"{CONFIG_FILENAME} already exists. Use --force to overwrite.")
        return

    base = detect_base_branch("origin")

    template = textwrap.dedent(f"""\
    # WARNING: Do NOT put secrets (DATABASE_URL, API keys, passwords) here.
    # Secrets go in .orchestrator/.secrets — see below.

    [project]
    remote = "origin"
    base_branch = "{base}"
    branch_prefix = "feature/issue-"

    # -------------------------------------------------------------------
    # Servers
    # -------------------------------------------------------------------
    # Define one [servers.<name>] section per process your project needs.
    # Ports are auto-assigned. Use these placeholders in start_command and env values:
    #   {{port}}              - this server's own port
    #   {{backend.port}}      - the "backend" server's port (use any server name)
    #   {{frontend.port}}     - the "frontend" server's port
    #
    # Secrets (DATABASE_URL, API keys) go in .orchestrator/.secrets
    # and are loaded into every server's environment automatically.
    #
    # Commands must run non-interactively (no TTY). For Flutter, use
    # -d web-server instead of -d chrome.
    #
    # Optional fields:
    #   directory = "server"   # subdirectory to run from (default: repo root)
    #
    # Use [servers.<name>.env] to set/override env vars with port substitution.

    # -- Example: Dart Shelf backend --
    # [servers.backend]
    # start_command = "dart run bin/server.dart"
    # directory = "server"
    #
    # [servers.backend.env]
    # PORT = "{{backend.port}}"
    # FRONTEND_URL = "http://localhost:{{frontend.port}}"
    # ALLOWED_ORIGIN = "http://localhost:{{frontend.port}}"
    # DEV_MODE = "true"

    # -- Example: Flutter frontend that needs the backend port --
    # [servers.frontend]
    # start_command = "flutter run -d web-server --web-port={{frontend.port}} --dart-define=API_BASE_URL=http://localhost:{{backend.port}} --dart-define=DEV_MODE=true"

    # -- Example: simple static site --
    # [servers.web]
    # start_command = "npm run dev -- --port {{port}}"
    """)

    config_path.write_text(template, encoding="utf-8")

    # Create .orchestrator/ dir and secrets file
    od = orch_dir(repo_root)
    secrets_path = od / SECRETS_FILE
    if not secrets_path.exists():
        secrets_template = textwrap.dedent("""\
        # Secrets - loaded into every server's environment.
        # This file is inside .orchestrator/ which is gitignored.
        #
        # Format: KEY=value (one per line, no quotes needed)
        #
        # DATABASE_URL=postgres://user:pass@localhost:5432/mydb
        # GOOGLE_CLIENT_ID=your-client-id
        # GOOGLE_CLIENT_SECRET=your-client-secret
        """)
        secrets_path.write_text(secrets_template, encoding="utf-8")
        print(f"Created {secrets_path}")

    ensure_gitignore(repo_root)

    wt_dir = worktree_base_dir(repo_root)
    print(f"Created {config_path}")
    print(f"  -> Edit [servers.*] sections for your project")
    print(f"  -> Add secrets to {secrets_path}")
    print(f"  -> Detected base branch: {base}")
    print(f"  -> Worktrees will go to: {wt_dir}/<session>")


def cmd_spawn(args):
    repo_root = find_repo_root()
    validate_no_secrets_in_config(repo_root)
    config = load_config(repo_root)
    sessions = load_sessions(repo_root)
    name = args.name

    if name in sessions and sessions[name]["status"] == "running":
        print(f"Error: session '{name}' is already running. Kill it first.", file=sys.stderr)
        sys.exit(1)

    branch = f"{config['branch_prefix']}{name}"
    wt_base = worktree_base_dir(repo_root)
    wt_path = wt_base / name

    # Fetch (non-fatal)
    print(f"Fetching from {config['remote']}...")
    fetch = subprocess.run(
        ["git", "fetch", config["remote"]], cwd=repo_root, capture_output=True, text=True
    )
    if fetch.returncode != 0:
        print(f"Warning: could not fetch from {config['remote']}. Using local state.")

    # Create branch if needed
    check = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        capture_output=True, text=True, cwd=repo_root
    )
    if check.returncode != 0:
        base_ref = f"{config['remote']}/{config['base_branch']}"
        verify = subprocess.run(
            ["git", "rev-parse", "--verify", base_ref],
            capture_output=True, text=True, cwd=repo_root
        )
        if verify.returncode != 0:
            base_ref = config["base_branch"]
        print(f"Creating branch {branch} from {base_ref}...")
        subprocess.run(["git", "branch", branch, base_ref], cwd=repo_root, check=True)

    # Create worktree
    wt_base.mkdir(parents=True, exist_ok=True)
    if wt_path.exists():
        if is_valid_worktree(repo_root, wt_path):
            print(f"Worktree at {wt_path} is valid. Reusing.")
        else:
            print(f"Warning: {wt_path} exists but is not a valid worktree. Removing and recreating...")
            _rmtree_robust(wt_path)
            subprocess.run(["git", "worktree", "prune"], cwd=repo_root)
            subprocess.run(
                ["git", "worktree", "add", str(wt_path), branch],
                cwd=repo_root, check=True
            )
    else:
        print(f"Creating worktree at {wt_path}...")
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), branch],
            cwd=repo_root, check=True
        )

    # Phase 1: Allocate ALL ports upfront
    port_map = {}
    for srv_cfg in config["servers"]:
        port_map[srv_cfg["name"]] = find_free_port()

    # Phase 2: Load secrets once (shared across all servers)
    secrets = load_secrets(repo_root)

    # Phase 3: Start each server
    server_records = []
    for srv_cfg in config["servers"]:
        srv_name = srv_cfg["name"]
        port = port_map[srv_name]

        # Working directory
        cwd = wt_path / srv_cfg["directory"] if srv_cfg.get("directory") else wt_path

        # Build environment: inherit -> secrets -> per-server env overrides
        proc_env = os.environ.copy()
        proc_env.update(secrets)
        for key, val in srv_cfg.get("env", {}).items():
            proc_env[key] = substitute_vars(val, port_map, srv_name)

        # Substitute ports in start_command
        cmd = substitute_vars(srv_cfg["start_command"], port_map, srv_name)

        log_file = session_logs_dir(repo_root, name) / f"{srv_name}.log"

        print(f"Starting {srv_name} on port {port}: {cmd}")
        log_handle = open(log_file, "w", encoding="utf-8")

        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=str(cwd),
            env=proc_env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS else {})
        )

        server_records.append({
            "name": srv_name,
            "port": port,
            "pid": proc.pid,
            "command": cmd,
            "directory": srv_cfg.get("directory", ""),
        })
        print(f"  {srv_name} started (PID {proc.pid}, port {port})")

    if not server_records:
        print("No servers configured - worktree created without servers.")

    # Register session
    sessions[name] = {
        "name": name,
        "branch": branch,
        "worktree": str(wt_path),
        "servers": server_records,
        "ports": port_map,
        "status": "running",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_sessions(repo_root, sessions)

    print()
    print(f"Session '{name}' is ready:")
    print(f"  Branch:    {branch}")
    print(f"  Worktree:  {wt_path}")
    for srv in server_records:
        print(f"  {srv['name']:12s} port {srv['port']}  (PID {srv['pid']})")
    print()
    # Open a new terminal with claude in the worktree
    if not args.no_claude:
        open_terminal_with_claude(wt_path, name)
    else:
        print(f"Open {wt_path} in your editor to start working.")


def cmd_status(args):
    repo_root = find_repo_root()
    sessions = load_sessions(repo_root)

    if not sessions:
        print("No sessions.")
        return

    for name, s in sessions.items():
        status = s.get("status", "unknown")
        servers = s.get("servers", [])

        if status == "running" and servers:
            all_dead = all(not is_process_alive(srv.get("pid")) for srv in servers)
            if all_dead:
                status = "dead"
                s["status"] = "dead"

        print(f"[{name}]  branch={s.get('branch')}  status={status}")
        print(f"  worktree: {s.get('worktree')}")
        for srv in servers:
            alive = is_process_alive(srv.get("pid"))
            marker = "UP" if alive else "DOWN"
            print(f"  {srv['name']:12s}  port {srv['port']}  PID {srv['pid']}  [{marker}]")
        if not servers:
            print("  (no servers)")
        print()

    save_sessions(repo_root, sessions)


def cmd_logs(args):
    repo_root = find_repo_root()
    name = args.name
    log_dir = orch_dir(repo_root) / LOGS_DIR / name

    if not log_dir.exists():
        print(f"No logs found for session '{name}'.", file=sys.stderr)
        sys.exit(1)

    log_files = sorted(log_dir.glob("*.log"))

    if args.server:
        log_files = [f for f in log_files if f.stem == args.server]
        if not log_files:
            print(f"No logs for server '{args.server}' in session '{name}'.", file=sys.stderr)
            sys.exit(1)

    n = args.lines
    for log_file in log_files:
        srv_name = log_file.stem
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > n:
            lines = lines[-n:]
        print(f"--- {srv_name} (last {min(n, len(lines))} lines) ---")
        for line in lines:
            print(line)
        print()


def cmd_kill(args):
    repo_root = find_repo_root()
    sessions = load_sessions(repo_root)
    name = args.name

    if name not in sessions:
        print(f"Error: no session named '{name}'.", file=sys.stderr)
        sys.exit(1)

    s = sessions[name]

    # Phase 1: Kill all processes
    for srv in s.get("servers", []):
        pid = srv.get("pid")
        if pid and is_process_alive(pid):
            print(f"Stopping {srv['name']} (PID {pid})...")
            kill_process(pid)
        else:
            print(f"{srv['name']} already stopped.")

    # Phase 2: Wait for file locks to release on Windows
    if IS_WINDOWS and args.remove:
        time.sleep(1.5)

    # Phase 3: Remove worktree, logs, and registry entry
    if args.remove:
        wt = Path(s["worktree"])
        if wt.exists():
            print(f"Removing worktree {wt}...")
            result = subprocess.run(
                ["git", "worktree", "remove", str(wt), "--force"],
                cwd=repo_root, capture_output=True, text=True
            )
            if result.returncode != 0:
                print(f"git worktree remove failed, falling back to manual removal...")
                _rmtree_robust(wt)
                subprocess.run(["git", "worktree", "prune"], cwd=repo_root)

        if wt.exists():
            # Directory is still locked — don't remove from registry
            s["status"] = "stopped"
            save_sessions(repo_root, sessions)
            print(f"Error: could not remove worktree directory {wt}.", file=sys.stderr)
            print(f"The directory is likely locked by another process (editor, terminal, Claude Code).", file=sys.stderr)
            print(f"Close anything using that directory, then retry.", file=sys.stderr)
            sys.exit(1)

        print("Worktree removed.")
        log_dir = orch_dir(repo_root) / LOGS_DIR / name
        if log_dir.exists():
            _rmtree_robust(log_dir)
        del sessions[name]
    else:
        s["status"] = "stopped"

    save_sessions(repo_root, sessions)


def cmd_restart(args):
    repo_root = find_repo_root()
    validate_no_secrets_in_config(repo_root)
    config = load_config(repo_root)
    sessions = load_sessions(repo_root)
    name = args.name

    if name not in sessions:
        print(f"Error: no session named '{name}'.", file=sys.stderr)
        sys.exit(1)

    s = sessions[name]
    wt_path = Path(s["worktree"])

    if not wt_path.exists():
        print(f"Error: worktree {wt_path} no longer exists. Use 'spawn' instead.", file=sys.stderr)
        sys.exit(1)

    # Phase 1: Kill all existing processes
    for srv in s.get("servers", []):
        pid = srv.get("pid")
        if pid and is_process_alive(pid):
            print(f"Stopping {srv['name']} (PID {pid})...")
            kill_process(pid)
        else:
            print(f"{srv['name']} already stopped.")

    if IS_WINDOWS:
        time.sleep(1.5)

    # Phase 2: Allocate new ports and restart servers
    port_map = {}
    for srv_cfg in config["servers"]:
        port_map[srv_cfg["name"]] = find_free_port()

    secrets = load_secrets(repo_root)

    server_records = []
    for srv_cfg in config["servers"]:
        srv_name = srv_cfg["name"]
        port = port_map[srv_name]

        cwd = wt_path / srv_cfg["directory"] if srv_cfg.get("directory") else wt_path

        proc_env = os.environ.copy()
        proc_env.update(secrets)
        for key, val in srv_cfg.get("env", {}).items():
            proc_env[key] = substitute_vars(val, port_map, srv_name)

        cmd = substitute_vars(srv_cfg["start_command"], port_map, srv_name)

        log_file = session_logs_dir(repo_root, name) / f"{srv_name}.log"

        print(f"Starting {srv_name} on port {port}: {cmd}")
        log_handle = open(log_file, "w", encoding="utf-8")

        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=str(cwd),
            env=proc_env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS else {})
        )

        server_records.append({
            "name": srv_name,
            "port": port,
            "pid": proc.pid,
            "command": cmd,
            "directory": srv_cfg.get("directory", ""),
        })
        print(f"  {srv_name} started (PID {proc.pid}, port {port})")

    s["servers"] = server_records
    s["ports"] = port_map
    s["status"] = "running"
    save_sessions(repo_root, sessions)

    print()
    print(f"Session '{name}' restarted:")
    print(f"  Branch:    {s['branch']}")
    print(f"  Worktree:  {wt_path}")
    for srv in server_records:
        print(f"  {srv['name']:12s} port {srv['port']}  (PID {srv['pid']})")


def cmd_cleanup(args):
    repo_root = find_repo_root()
    sessions = load_sessions(repo_root)

    stopped = {n: s for n, s in sessions.items() if s.get("status") in ("stopped", "dead")}

    if not stopped:
        print("Nothing to clean up.")
        return

    print(f"Will remove {len(stopped)} stopped session(s):")
    for name in stopped:
        print(f"  - {name}")

    if not args.force:
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return

    succeeded = []
    failed = []
    for name, s in stopped.items():
        wt = Path(s["worktree"])
        if wt.exists():
            print(f"Removing worktree {wt}...")
            result = subprocess.run(
                ["git", "worktree", "remove", str(wt), "--force"],
                cwd=repo_root, capture_output=True, text=True
            )
            if result.returncode != 0:
                print(f"git worktree remove failed, falling back to manual removal...")
                _rmtree_robust(wt)

        if wt.exists():
            print(f"Warning: could not remove {wt} — directory likely locked by another process.", file=sys.stderr)
            sessions[name]["status"] = "stopped"
            failed.append(name)
        else:
            print(f"Worktree removed: {name}")
            log_dir = orch_dir(repo_root) / LOGS_DIR / name
            if log_dir.exists():
                _rmtree_robust(log_dir)
            succeeded.append(name)

    for name in succeeded:
        del sessions[name]
    save_sessions(repo_root, sessions)

    subprocess.run(["git", "worktree", "prune"], cwd=repo_root, capture_output=True)
    if succeeded:
        print(f"Cleaned up {len(succeeded)} session(s).")
    if failed:
        print(f"Failed to remove {len(failed)} session(s): {', '.join(failed)}", file=sys.stderr)
        print("Close editors/terminals using those directories, then retry.", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="Manage parallel git worktree sessions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Create config, secrets file, and .orchestrator/ dir")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing config")

    p_spawn = sub.add_parser("spawn", help="Create a new worktree session")
    p_spawn.add_argument("name", help="Session name (issue number or slug)")
    p_spawn.add_argument("--no-claude", action="store_true",
                         help="Don't open a new terminal with claude")

    sub.add_parser("status", help="Show all sessions and server health")

    p_logs = sub.add_parser("logs", help="Show server logs")
    p_logs.add_argument("name", help="Session name")
    p_logs.add_argument("server", nargs="?", default=None,
                        help="Server name (omit to show all)")
    p_logs.add_argument("-n", "--lines", type=int, default=50,
                        help="Lines to show (default: 50)")

    p_kill = sub.add_parser("kill", help="Stop all servers in a session")
    p_kill.add_argument("name", help="Session name")
    p_kill.add_argument("--remove", action="store_true",
                        help="Also remove worktree and logs")

    p_restart = sub.add_parser("restart", help="Stop and restart all servers in a session")
    p_restart.add_argument("name", help="Session name")

    p_cleanup = sub.add_parser("cleanup", help="Remove stopped sessions and worktrees")
    p_cleanup.add_argument("--force", action="store_true", help="Skip confirmation")

    args = parser.parse_args()
    commands = {
        "init": cmd_init,
        "spawn": cmd_spawn,
        "status": cmd_status,
        "logs": cmd_logs,
        "kill": cmd_kill,
        "restart": cmd_restart,
        "cleanup": cmd_cleanup,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Worktree Orchestrator - manage parallel git worktree sessions.

Cross-platform (Windows, macOS, Linux). Python 3.9+ stdlib only.
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
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

PROXY_DIR = Path.home() / ".orchestrator"
PROXY_ROUTES_FILE = PROXY_DIR / "routes.json"
ACCESS_FILE = PROXY_DIR / "access.json"
DEFAULT_PROXY_PORT = 1337
DEFAULT_TLD = "localhost"
MAIN_SESSION = "main"  # reserved: served in-place at the apex host <project>.<tld>
CLAUDE_CMD = "claude --dangerously-skip-permissions"

def _tld():    return os.environ.get("ORCH_TLD", DEFAULT_TLD)
def _scheme(): return os.environ.get("ORCH_SCHEME", "http")
def _proxy_port(): return int(os.environ.get("ORCH_PROXY_PORT", str(DEFAULT_PROXY_PORT)))

def _url_port_suffix():
    v = os.environ.get("ORCH_URL_PORT")
    if v is None:
        return f":{_proxy_port()}"
    return "" if v == "" else f":{v}"

def host_for(session, server, project, *, primary=False):
    tld = _tld()
    if session == MAIN_SESSION:
        return f"{project}.{tld}" if primary else f"{project}-{server}.{tld}"
    return f"{session}.{project}.{tld}" if primary else f"{session}-{server}.{project}.{tld}"

def proxy_url(session, server, project, *, primary=False):
    return f"{_scheme()}://{host_for(session, server, project, primary=primary)}{_url_port_suffix()}"


def should_record_access(prev_iso, now_dt, min_seconds=30):
    if not prev_iso:
        return True
    try:
        prev = datetime.fromisoformat(prev_iso)
    except ValueError:
        return True
    return (now_dt - prev).total_seconds() >= min_seconds


def _load_access():
    if not ACCESS_FILE.exists():
        return {}
    try:
        return json.loads(ACCESS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def record_access(host, now_dt):
    data = _load_access()
    if should_record_access(data.get(host), now_dt):
        data[host] = now_dt.isoformat()
        PROXY_DIR.mkdir(parents=True, exist_ok=True)
        ACCESS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
            "setup_command": cfg.get("setup_command", ""),
            "directory": cfg.get("directory", ""),
            "primary": bool(cfg.get("primary", False)),
            "env": {k: v for k, v in cfg.get("env", {}).items() if isinstance(v, str)},
        })

    return {
        "remote": project.get("remote", "origin"),
        "base_branch": project.get("base_branch", "main"),
        "branch_prefix": project.get("branch_prefix", "b"),
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


def deterministic_port(project: str, session: str, server: str,
                       base: int = 10000, range_size: int = 50000) -> int:
    """Derive a stable port from (project, session, server).

    Returns the same port every time for the same inputs, so URLs survive
    restarts.  Falls back to an ephemeral port if the deterministic one is
    already in use by another process.
    """
    key = f"{project}:{session}:{server}"
    port = base + int(hashlib.md5(key.encode()).hexdigest(), 16) % range_size
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return port
        except OSError:
            return find_free_port()


# ---------------------------------------------------------------------------
# Proxy route management
# ---------------------------------------------------------------------------

def is_proxy_running(port: int = DEFAULT_PROXY_PORT) -> bool:
    """Check if something is already listening on the proxy port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def ensure_proxy_running(port: int = DEFAULT_PROXY_PORT):
    """Start the proxy as a detached background process if not already running."""
    if is_proxy_running(port):
        return
    script = Path(__file__).resolve()
    cmd = [sys.executable, str(script), "proxy", "-p", str(port)]
    if IS_WINDOWS:
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
        )
    else:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def load_proxy_routes() -> dict:
    """Load the shared proxy route table (~/.orchestrator/routes.json)."""
    if not PROXY_ROUTES_FILE.exists():
        return {}
    try:
        return json.loads(PROXY_ROUTES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_proxy_routes(routes: dict):
    PROXY_DIR.mkdir(parents=True, exist_ok=True)
    PROXY_ROUTES_FILE.write_text(json.dumps(routes, indent=2), encoding="utf-8")


def register_proxy_routes(project, session, port_map, primary_server=None):
    """Register hostname->port mappings for a session's servers.

    If primary_server is set, the bare `{session}.{project}.{tld}` host points to
    it and that server gets NO `{session}-{server}` host. Otherwise every server
    gets a `{session}-{server}` host and the bare host points to the first server.
    """
    routes = load_proxy_routes()
    servers = list(port_map.keys())
    for srv_name, port in port_map.items():
        if srv_name == primary_server:
            continue  # primary uses the bare host only
        routes[host_for(session, srv_name, project)] = port
    if primary_server and primary_server in port_map:
        routes[host_for(session, primary_server, project, primary=True)] = port_map[primary_server]
    elif servers:
        routes[host_for(session, servers[0], project, primary=True)] = port_map[servers[0]]
    save_proxy_routes(routes)


def unregister_proxy_routes(project: str, session: str):
    """Remove all hostname->port mappings for a session."""
    routes = load_proxy_routes()
    tld = _tld()
    if session == MAIN_SESSION:
        # apex hosts: <project>.<tld> (primary) and <project>-<server>.<tld>
        apex = f"{project}.{tld}"
        prefix, suf = f"{project}-", f".{tld}"
        to_remove = [h for h in routes
                     if h == apex or (h.startswith(prefix) and h.endswith(suf))]
    else:
        suffix = f".{project}.{tld}"
        to_remove = [h for h in routes
                     if h.endswith(suffix)
                     and (h.startswith(f"{session}.") or h.startswith(f"{session}-"))]
    for h in to_remove:
        del routes[h]
    save_proxy_routes(routes)


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


def get_alive_pids(pids) -> set:
    """Batch-check which PIDs are alive. Returns the set of alive PIDs.

    On Windows, calls tasklist once and parses all PIDs from the output,
    avoiding the ~0.5s overhead per individual tasklist call.
    """
    pids = {int(p) for p in pids if p is not None}
    if not pids:
        return set()
    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ["tasklist", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=10,
            )
            alive = set()
            for line in result.stdout.splitlines():
                parts = line.split('"')
                # CSV format: "name","pid","session","session#","mem"
                if len(parts) >= 4:
                    try:
                        p = int(parts[3])
                        if p in pids:
                            alive.add(p)
                    except ValueError:
                        continue
            return alive
        except (OSError, subprocess.TimeoutExpired):
            return set()
    else:
        alive = set()
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.add(pid)
            except (OSError, ProcessLookupError):
                pass
        return alive


def kill_process(pid):
    """Stop a server process and its detached children.

    Servers are launched with start_new_session=True, so the recorded PID is a
    shell wrapper that is its own process-group leader; the real dart/flutter
    process is a child in that group. Killing only the wrapper PID orphans the
    child (RAM leak + stale servers), so when `pid` is its own group leader we
    signal the whole process group. When it is NOT a leader (it shares a group,
    e.g. with the orchestrator itself), we fall back to the single PID — killing
    the shared group would take down unrelated processes.
    """
    if pid is None:
        return
    pid = int(pid)
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True)
        except OSError:
            pass
        return

    try:
        pgid = os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return  # already gone
    group_kill = (pgid == pid)

    def _alive():
        if group_kill:
            try:
                os.killpg(pgid, 0)
                return True
            except (OSError, ProcessLookupError):
                return False
        return is_process_alive(pid)

    def _send(sig):
        if group_kill:
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)

    try:
        _send(signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.3)
            if not _alive():
                return
        _send(signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def parse_ss_listeners(text):
    """Parse `ss -ltnpH` output into {port: set(pids)}.

    Each line looks like:
        LISTEN 0 128 0.0.0.0:50022 0.0.0.0:* users:(("dart:server.dar",pid=9302,fd=10))
    The 4th column is the local address:port; PIDs come from the pid=N fields.
    """
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]  # e.g. 0.0.0.0:50022, *:443, [::]:22, 127.0.0.1:1337
        if ":" not in local:
            continue
        try:
            port = int(local.rsplit(":", 1)[1])
        except ValueError:
            continue
        pids = {int(m) for m in re.findall(r"pid=(\d+)", line)}
        result.setdefault(port, set()).update(pids)
    return result


def listening_ports_with_pids():
    """Current TCP listeners as {port: set(pids)} via `ss` (Linux). {} elsewhere
    or if `ss` is unavailable — callers treat an empty result as 'unknown'."""
    if IS_WINDOWS:
        return {}
    try:
        r = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True,
                           timeout=5)
        return parse_ss_listeners(r.stdout) if r.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired):
        return {}


def verify_servers_stopped(servers):
    """After killing a session, confirm none of its recorded ports are still
    listening. Returns [(name, port, set(pids))] for each straggler — an orphan
    the wrapper-PID kill missed. Best-effort: returns [] when listeners can't be
    read (so absence of evidence never blocks a kill)."""
    listeners = listening_ports_with_pids()
    if not listeners:
        return []
    stragglers = []
    for srv in servers:
        port = srv.get("port")
        if port is None:
            continue
        pids = listeners.get(int(port))
        if pids:
            stragglers.append((srv.get("name"), int(port), set(pids)))
    return stragglers


def reap_stragglers(servers):
    """Verification step run after a kill: every recorded server port must be
    free. Reap any orphan still bound to one and report what was found."""
    stragglers = verify_servers_stopped(servers)
    for name, port, pids in stragglers:
        print(f"WARNING: {name} still listening on :{port} after kill "
              f"(orphan PID(s) {sorted(pids)}); reaping.")
        for pid in pids:
            kill_process(pid)
    if not stragglers:
        return
    still = verify_servers_stopped(servers)
    if still:
        for name, port, pids in still:
            print(f"ERROR: :{port} ({name}) still bound by {sorted(pids)} after "
                  f"reap — manual cleanup needed.", file=sys.stderr)
    else:
        print("Verified: all server ports are free.")


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


def is_repo_root_worktree(worktree, repo_root):
    """True if a session's worktree IS the repo root — the in-place `main` session,
    whose 'worktree' must never be git-removed."""
    return os.path.normpath(str(worktree)) == os.path.normpath(str(repo_root))


def substitute_vars(text: str, port_map: dict, current_server: str = "",
                    project: str = "", session: str = "",
                    primary_server: str = None) -> str:
    """Replace port and URL placeholders in a string.

    Supports:
      {port}                  - current server's own port
      {servername.port}       - named server's port
      {url}                   - current server's proxy URL
      {servername.url}        - named server's proxy URL
    """
    for srv_name, port in port_map.items():
        text = text.replace(f"{{{srv_name}.port}}", str(port))
        if project and session:
            url = proxy_url(session, srv_name, project, primary=(srv_name == primary_server))
            text = text.replace(f"{{{srv_name}.url}}", url)
    if current_server and current_server in port_map:
        text = text.replace("{port}", str(port_map[current_server]))
        if project and session:
            text = text.replace("{url}",
                proxy_url(session, current_server, project, primary=(current_server == primary_server)))
    return text


def run_setup_command(setup_cmd: str, cwd: Path, env: dict,
                      log_handle, srv_name: str) -> bool:
    """Run a server's setup_command before its start_command.

    Runs synchronously in the server's working directory and environment.
    Output is captured into the already-open server log handle, framed by
    `=== setup ===` markers. Returns True on success (exit 0, or no setup
    command configured), False if the command exited non-zero.
    """
    if not setup_cmd:
        return True
    print(f"  Running setup for {srv_name}: {setup_cmd}")
    log_handle.write(f"=== setup: {setup_cmd} ===\n")
    log_handle.flush()
    result = subprocess.run(
        setup_cmd,
        shell=True,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    log_handle.write(f"=== setup exited {result.returncode} ===\n")
    log_handle.flush()
    if result.returncode != 0:
        log_path = getattr(log_handle, "name", "the server log")
        print(f"  ERROR: setup for {srv_name} failed (exit {result.returncode}). "
              f"Server not started.", file=sys.stderr)
        print(f"  See {log_path}", file=sys.stderr)
        return False
    return True


def open_terminal_with_claude(worktree_path: Path, session_name: str):
    """Open a new terminal window running claude in the worktree directory."""
    wt_str = str(worktree_path)
    claude_cmd = CLAUDE_CMD

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
                     "cmd", "/k", claude_cmd],
                    env=clean_env,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                subprocess.Popen(
                    f'start "claude [{session_name}]" cmd /k "cd /d {wt_str} && {claude_cmd}"',
                    shell=True,
                    env=clean_env,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                )
        elif platform.system() == "Darwin":
            # macOS: use AppleScript to open Terminal.app — unset CLAUDECODE inline
            script = (
                f'tell application "Terminal"\n'
                f'  do script "unset CLAUDECODE && cd {wt_str} && {claude_cmd}"\n'
                f'  activate\n'
                f'end tell'
            )
            subprocess.Popen(["osascript", "-e", script])
        else:
            # Linux: try common terminal emulators
            for term_cmd in [
                ["gnome-terminal", "--", "bash", "-c", f"unset CLAUDECODE && cd {wt_str} && {claude_cmd}; exec bash"],
                ["xfce4-terminal", "-e", f"bash -c 'unset CLAUDECODE && cd {wt_str} && {claude_cmd}; exec bash'"],
                ["konsole", "-e", "bash", "-c", f"unset CLAUDECODE && cd {wt_str} && {claude_cmd}; exec bash"],
                ["xterm", "-e", f"bash -c 'unset CLAUDECODE && cd {wt_str} && {claude_cmd}; exec bash'"],
            ]:
                try:
                    subprocess.Popen(term_cmd, env=clean_env)
                    break
                except FileNotFoundError:
                    continue
            else:
                print(f"  Could not detect terminal emulator. Run manually:")
                print(f"    cd {wt_str} && {claude_cmd}")
                return

        print(f"  Opened new terminal with claude in {wt_str}")
    except Exception as e:
        print(f"  Could not open terminal: {e}")
        print(f"  Run manually: cd {wt_str} && {claude_cmd}")


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


def panes_to_create(worktree_paths, existing_pane_paths):
    """Worktree paths that have no tmux pane yet (additive reconcile).

    Compared by normalized path so trailing slashes / '.' segments don't cause a
    duplicate pane. Input order is preserved."""
    existing = {os.path.normpath(p) for p in existing_pane_paths}
    return [p for p in worktree_paths if os.path.normpath(p) not in existing]


def worktree_paths_under(porcelain_text, base_dir):
    """Session worktree paths from `git worktree list --porcelain` that live under
    base_dir. `git worktree list` is the source of truth (sessions.json can be out of
    sync); the main repo isn't under the worktrees base, so it's naturally excluded."""
    base = os.path.normpath(str(base_dir))
    out = []
    for line in porcelain_text.splitlines():
        if line.startswith("worktree "):
            p = line[len("worktree "):].strip()
            np = os.path.normpath(p)
            if np == base or np.startswith(base + os.sep):
                out.append(p)
    return out


def grid_pane_command():
    """Command a grid pane runs: plain `claude`. NOT --dangerously-skip-permissions —
    claude refuses that as root (and the VPS runs as root); plain claude works and
    honors the user's default permission mode. tmux execs this directly (no shell)."""
    return "claude"


def reorder_swaps(current, desired):
    """Selection-sort swaps to turn `current` (items by visual pane position) into
    `desired`. Returns [(i, j)] position pairs; applying `swap-pane` between the panes
    at positions i and j, in order, leaves panes reading in `desired` order. Used so a
    re-added pane doesn't leave the grid out of worktree order."""
    cur = list(current)
    swaps = []
    for k in range(len(desired)):
        if cur[k] == desired[k]:
            continue
        j = cur.index(desired[k], k + 1)
        swaps.append((k, j))
        cur[k], cur[j] = cur[j], cur[k]
    return swaps


def _order_grid_panes(session):
    """Reorder panes so the tiled grid reads in worktree order (top-left → bottom).
    swap-pane preserves each pane's running process, so no Claude is disturbed."""
    r = _tmux("list-panes", "-t", session, "-F",
              "#{pane_id}\t#{pane_top}\t#{pane_left}\t#{pane_current_path}")
    if r.returncode != 0:
        return
    rows = [ln.split("\t") for ln in r.stdout.splitlines() if ln.strip()]
    rows.sort(key=lambda x: (int(x[1]), int(x[2])))   # visual order: top, then left
    pane_ids = [x[0] for x in rows]
    current = [x[3] for x in rows]

    def _key(path):
        base = os.path.basename(os.path.normpath(path))
        return (0, int(base)) if base.isdigit() else (1, base)

    desired = sorted(current, key=_key)
    for i, j in reorder_swaps(current, desired):
        _tmux("swap-pane", "-s", pane_ids[j], "-t", pane_ids[i])
        pane_ids[i], pane_ids[j] = pane_ids[j], pane_ids[i]


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
    branch_prefix = "b"

    # -------------------------------------------------------------------
    # Servers
    # -------------------------------------------------------------------
    # Define one [servers.<name>] section per process your project needs.
    # Ports are auto-assigned. Use these placeholders in start_command and env values:
    #   {{port}}              - this server's own port
    #   {{url}}               - this server's proxy URL (e.g. http://b1-web.myapp.localhost:1337)
    #   {{backend.port}}      - the "backend" server's port (use any server name)
    #   {{backend.url}}       - the "backend" server's proxy URL
    #   {{frontend.port}}     - the "frontend" server's port
    #   {{frontend.url}}      - the "frontend" server's proxy URL
    #
    # NOTE: Use {{*.url}} for CORS, API base URLs, etc. (browsers resolve .localhost fine).
    # Use http://localhost:{{*.port}} for OAuth redirect URIs (Google rejects .localhost subdomains).
    #
    # Secrets (DATABASE_URL, API keys) go in .orchestrator/.secrets
    # and are loaded into every server's environment automatically.
    #
    # Commands must run non-interactively (no TTY). For Flutter, use
    # -d web-server instead of -d chrome.
    #
    # Optional fields:
    #   directory = "server"          # subdirectory to run from (default: repo root)
    #   setup_command = "npm install" # runs before start_command on every spawn/restart
    #
    # Use [servers.<name>.env] to set/override env vars with port substitution.

    # -- Example: Dart Shelf backend --
    # [servers.backend]
    # setup_command = "dart pub get"
    # start_command = "dart run bin/server.dart"
    # directory = "server"
    #
    # [servers.backend.env]
    # PORT = "{{backend.port}}"
    # FRONTEND_URL = "{{frontend.url}}"
    # ALLOWED_ORIGIN = "{{frontend.url}}"
    # DEV_MODE = "true"

    # -- Example: Flutter frontend that needs the backend port --
    # [servers.frontend]
    # setup_command = "flutter pub get"
    # start_command = "flutter run -d web-server --web-port={{frontend.port}} --dart-define=API_BASE_URL={{backend.url}} --dart-define=DEV_MODE=true"

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

    if name == MAIN_SESSION:
        wt_path = repo_root
        branch = config["base_branch"]  # main runs on the base branch, in place
        print(f"Serving '{name}' in place at {wt_path} (no worktree).")
    else:
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

        # Resolve base_ref once (prefer remote, fall back to local)
        base_ref = f"{config['remote']}/{config['base_branch']}"
        verify_base = subprocess.run(
            ["git", "rev-parse", "--verify", base_ref],
            capture_output=True, text=True, cwd=repo_root
        )
        base_ref_available = verify_base.returncode == 0
        if not base_ref_available:
            base_ref = config["base_branch"]

        # Create branch if needed, or fast-forward an existing branch to base_ref
        # so the new worktree starts on current main rather than a stale commit
        # left over from a previous session on this slot.
        check = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            capture_output=True, text=True, cwd=repo_root
        )
        if check.returncode != 0:
            print(f"Creating branch {branch} from {base_ref}...")
            subprocess.run(["git", "branch", branch, base_ref], cwd=repo_root, check=True)
        elif base_ref_available:
            # Only fast-forward when the existing branch is strictly an ancestor
            # of base_ref — never discard local commits. Skip silently if the
            # branch is already at base_ref (is-ancestor returns 0 for equal refs).
            is_ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", branch, base_ref],
                cwd=repo_root, capture_output=True,
            )
            if is_ancestor.returncode == 0:
                ff = subprocess.run(
                    ["git", "branch", "-f", branch, base_ref],
                    cwd=repo_root, capture_output=True, text=True,
                )
                if ff.returncode == 0:
                    print(f"Fast-forwarded {branch} to {base_ref}.")
                else:
                    # Branch is likely checked out in another worktree.
                    print(f"Warning: could not fast-forward {branch} to {base_ref}: "
                          f"{ff.stderr.strip() or 'unknown error'}")
            else:
                print(f"Branch {branch} has commits not on {base_ref}; leaving as-is.")

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

    # Phase 1: Allocate ALL ports upfront (deterministic from project+session+server)
    proj = project_name(repo_root)
    port_map = {}
    for srv_cfg in config["servers"]:
        port_map[srv_cfg["name"]] = deterministic_port(proj, name, srv_cfg["name"])

    # Phase 2: Load secrets once (shared across all servers)
    secrets = load_secrets(repo_root)
    primary_server = next((s["name"] for s in config["servers"] if s.get("primary")), None)

    # Phase 3: Start each server
    server_records = []
    setup_failed = False
    for srv_cfg in config["servers"]:
        srv_name = srv_cfg["name"]
        port = port_map[srv_name]

        # Working directory
        cwd = wt_path / srv_cfg["directory"] if srv_cfg.get("directory") else wt_path

        # Build environment: inherit -> secrets -> per-server env overrides
        proc_env = os.environ.copy()
        proc_env.update(secrets)
        for key, val in srv_cfg.get("env", {}).items():
            proc_env[key] = substitute_vars(val, port_map, srv_name, proj, name,
                                            primary_server=primary_server)

        # Substitute ports in start_command
        cmd = substitute_vars(srv_cfg["start_command"], port_map, srv_name, proj, name,
                              primary_server=primary_server)

        log_file = session_logs_dir(repo_root, name) / f"{srv_name}.log"
        log_handle = open(log_file, "w", encoding="utf-8")

        # Run the setup step (e.g. `dart pub get`) before starting the server.
        setup_cmd = substitute_vars(srv_cfg.get("setup_command", ""),
                                    port_map, srv_name, proj, name,
                                    primary_server=primary_server)
        if not run_setup_command(setup_cmd, cwd, proc_env, log_handle, srv_name):
            log_handle.close()
            setup_failed = True
            server_records.append({
                "name": srv_name,
                "port": port,
                "pid": None,
                "command": cmd,
                "directory": srv_cfg.get("directory", ""),
            })
            continue

        print(f"Starting {srv_name} on port {port}: {cmd}")

        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=str(cwd),
            env=proc_env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS
               else {"start_new_session": True})
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
    now_iso = datetime.now(timezone.utc).isoformat()
    sessions[name] = {
        "name": name,
        "branch": branch,
        "worktree": str(wt_path),
        "servers": server_records,
        "ports": port_map,
        "status": "running",
        "created_at": now_iso,
        "started_at": now_iso,
    }
    save_sessions(repo_root, sessions)
    register_proxy_routes(proj, name, port_map, primary_server=primary_server)
    ensure_proxy_running()

    print()
    print(f"Session '{name}' is ready:")
    print(f"  Branch:    {branch}")
    print(f"  Worktree:  {wt_path}")
    for srv in server_records:
        is_primary = (srv["name"] == primary_server)
        if srv["pid"] is None:
            print(f"  {srv['name']:12s} setup failed - see logs ({srv['name']}.log)")
        else:
            print(f"  {srv['name']:12s} {proxy_url(name, srv['name'], proj, primary=is_primary)}  (PID {srv['pid']})")
    print()
    # Open a new terminal with claude in the worktree
    if not args.no_claude:
        open_terminal_with_claude(wt_path, name)
    else:
        print(f"Open {wt_path} in your editor to start working.")

    if setup_failed:
        print()
        print("Warning: one or more servers failed their setup step and were "
              "not started. Fix the cause and run 'restart'.", file=sys.stderr)
        sys.exit(1)


def parse_free_mb(text):
    mem = swap = None
    for line in text.splitlines():
        p = line.split()
        if p and p[0] == "Mem:":
            mem = p
        elif p and p[0] == "Swap:":
            swap = p
    if not mem:
        return None
    return {
        "total_mb": int(mem[1]), "used_mb": int(mem[2]),
        "available_mb": int(mem[6]) if len(mem) > 6 else int(mem[3]),
        "swap_total_mb": int(swap[1]) if swap else 0,
        "swap_used_mb": int(swap[2]) if swap else 0,
    }

def read_system_memory():
    if IS_WINDOWS:
        return None
    try:
        r = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=5)
        return parse_free_mb(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None

def _parse_ps_tree(text):
    """Parse `ps -eo pid=,ppid=,rss=` output into {pid: (ppid, rss_kb)}."""
    tree = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            tree[int(parts[0])] = (int(parts[1]), int(parts[2]))
        except ValueError:
            continue
    return tree


def sum_descendant_rss_mb(pid, ps_output):
    """Total RSS (MB) of `pid` plus all its descendants, from a
    `ps -eo pid=,ppid=,rss=` dump. Returns None if `pid` isn't present.

    The recorded server PID is a shell wrapper whose dart/flutter child actually
    holds the memory, so the wrapper's own RSS (~2 MB) is meaningless — we sum the
    whole subtree to get the real footprint."""
    tree = _parse_ps_tree(ps_output)
    if pid not in tree:
        return None
    children = {}
    for p, (ppid, _rss) in tree.items():
        children.setdefault(ppid, []).append(p)
    total, stack, seen = 0, [pid], set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        total += tree.get(cur, (0, 0))[1]
        stack.extend(children.get(cur, []))
    return round(total / 1024)


def process_rss_mb(pid):
    if pid is None or IS_WINDOWS:
        return None
    try:
        r = subprocess.run(["ps", "-eo", "pid=,ppid=,rss="],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        return sum_descendant_rss_mb(int(pid), r.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def build_status(repo_root):
    proj = project_name(repo_root)
    config = load_config(repo_root)
    primary_server = next((s["name"] for s in config["servers"] if s.get("primary")), None)
    sessions = load_sessions(repo_root)
    access = _load_access()
    out = {"memory": read_system_memory(), "sessions": {}}
    for name, s in sessions.items():
        servers = []
        for srv in s.get("servers", []):
            is_primary = (srv["name"] == primary_server)
            servers.append({
                "name": srv["name"], "port": srv.get("port"), "pid": srv.get("pid"),
                "up": is_process_alive(srv.get("pid")),
                "primary": is_primary,
                "url": proxy_url(name, srv["name"], proj, primary=is_primary),
                "rss_mb": process_rss_mb(srv.get("pid")),
                "last_access": access.get(host_for(name, srv["name"], proj, primary=is_primary)),
            })
        out["sessions"][name] = {
            "branch": s.get("branch"), "status": s.get("status"),
            "worktree": s.get("worktree"), "servers": servers,
        }
    return out


def cmd_status(args):
    repo_root = find_repo_root()
    if getattr(args, "json", False):
        print(json.dumps(build_status(repo_root), indent=2))
        return
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
        if n > 0 and len(lines) > n:
            lines = lines[-n:]
            header = f"--- {srv_name} (last {len(lines)} lines) ---"
        else:
            header = f"--- {srv_name} ({len(lines)} lines) ---"
        print(header)
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

    # Phase 1b: verify only the servers that should be running are — reap any
    # orphan still bound to a killed server's port.
    reap_stragglers(s.get("servers", []))

    # Phase 2: Wait for file locks to release on Windows
    if IS_WINDOWS and args.remove:
        time.sleep(1.5)

    # Phase 3: Remove worktree, logs, and registry entry
    if args.remove:
        wt = Path(s["worktree"])
        if is_repo_root_worktree(wt, repo_root):
            print(f"'{name}' runs in place ({wt}); stopping servers, leaving the checkout.")
        elif wt.exists():
            print(f"Removing worktree {wt}...")
            result = subprocess.run(
                ["git", "worktree", "remove", str(wt), "--force"],
                cwd=repo_root, capture_output=True, text=True
            )
            if result.returncode != 0:
                print(f"git worktree remove failed, falling back to manual removal...")
                _rmtree_robust(wt)
                subprocess.run(["git", "worktree", "prune"], cwd=repo_root)

        if wt.exists() and not is_repo_root_worktree(wt, repo_root):
            # Directory is still locked — don't remove from registry
            s["status"] = "stopped"
            save_sessions(repo_root, sessions)
            print(f"Error: could not remove worktree directory {wt}.", file=sys.stderr)
            print(f"The directory is likely locked by another process (editor, terminal, Claude Code).", file=sys.stderr)
            print(f"Close anything using that directory, then retry.", file=sys.stderr)
            sys.exit(1)

        if not is_repo_root_worktree(wt, repo_root):
            print("Worktree removed.")
        log_dir = orch_dir(repo_root) / LOGS_DIR / name
        if log_dir.exists():
            _rmtree_robust(log_dir)
        del sessions[name]
    else:
        s["status"] = "stopped"

    save_sessions(repo_root, sessions)
    unregister_proxy_routes(project_name(repo_root), name)


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

    # Verify the old servers are actually gone before relaunching — reap any
    # orphan still holding a port so restarts don't leak detached processes.
    reap_stragglers(s.get("servers", []))

    if IS_WINDOWS:
        time.sleep(1.5)

    # Phase 2: Allocate ports (deterministic — same as original spawn)
    proj = project_name(repo_root)
    port_map = {}
    for srv_cfg in config["servers"]:
        port_map[srv_cfg["name"]] = deterministic_port(proj, name, srv_cfg["name"])

    secrets = load_secrets(repo_root)
    primary_server = next((sc["name"] for sc in config["servers"] if sc.get("primary")), None)

    server_records = []
    setup_failed = False
    for srv_cfg in config["servers"]:
        srv_name = srv_cfg["name"]
        port = port_map[srv_name]

        cwd = wt_path / srv_cfg["directory"] if srv_cfg.get("directory") else wt_path

        proc_env = os.environ.copy()
        proc_env.update(secrets)
        for key, val in srv_cfg.get("env", {}).items():
            proc_env[key] = substitute_vars(val, port_map, srv_name, proj, name,
                                            primary_server=primary_server)

        cmd = substitute_vars(srv_cfg["start_command"], port_map, srv_name, proj, name,
                              primary_server=primary_server)

        log_file = session_logs_dir(repo_root, name) / f"{srv_name}.log"
        log_handle = open(log_file, "w", encoding="utf-8")

        # Run the setup step (e.g. `dart pub get`) before starting the server.
        setup_cmd = substitute_vars(srv_cfg.get("setup_command", ""),
                                    port_map, srv_name, proj, name,
                                    primary_server=primary_server)
        if not run_setup_command(setup_cmd, cwd, proc_env, log_handle, srv_name):
            log_handle.close()
            setup_failed = True
            server_records.append({
                "name": srv_name,
                "port": port,
                "pid": None,
                "command": cmd,
                "directory": srv_cfg.get("directory", ""),
            })
            continue

        print(f"Starting {srv_name} on port {port}: {cmd}")

        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=str(cwd),
            env=proc_env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS
               else {"start_new_session": True})
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
    s["started_at"] = datetime.now(timezone.utc).isoformat()
    save_sessions(repo_root, sessions)
    register_proxy_routes(proj, name, port_map, primary_server=primary_server)
    ensure_proxy_running()

    print()
    print(f"Session '{name}' restarted:")
    print(f"  Branch:    {s['branch']}")
    print(f"  Worktree:  {wt_path}")
    for srv in server_records:
        is_primary = (srv["name"] == primary_server)
        if srv["pid"] is None:
            print(f"  {srv['name']:12s} setup failed - see logs ({srv['name']}.log)")
        else:
            print(f"  {srv['name']:12s} {proxy_url(name, srv['name'], proj, primary=is_primary)}  (PID {srv['pid']})")

    if setup_failed:
        print()
        print("Warning: one or more servers failed their setup step and were "
              "not started. Fix the cause and run 'restart' again.", file=sys.stderr)
        sys.exit(1)


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

    proj = project_name(repo_root)
    for name in succeeded:
        del sessions[name]
        unregister_proxy_routes(proj, name)
    save_sessions(repo_root, sessions)

    subprocess.run(["git", "worktree", "prune"], cwd=repo_root, capture_output=True)
    if succeeded:
        print(f"Cleaned up {len(succeeded)} session(s).")
    if failed:
        print(f"Failed to remove {len(failed)} session(s): {', '.join(failed)}", file=sys.stderr)
        print("Close editors/terminals using those directories, then retry.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Reverse proxy
# ---------------------------------------------------------------------------

async def _proxy_connection(reader, writer, routes):
    """Handle one proxied connection: read headers, route by Host, pipe."""
    try:
        # Read until end of HTTP headers
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await asyncio.wait_for(reader.read(8192), timeout=30)
            if not chunk:
                writer.close()
                return
            buf += chunk

        header_end = buf.index(b"\r\n\r\n")
        header_bytes = buf[:header_end]
        rest = buf[header_end:]  # \r\n\r\n + any body bytes

        # Extract Host header
        host = None
        for line in header_bytes.split(b"\r\n"):
            if line.lower().startswith(b"host:"):
                host = line.split(b":", 1)[1].strip().decode("latin-1")
                if ":" in host:
                    host = host.rsplit(":", 1)[0]
                break

        if not host or host not in routes:
            body = f"No route for host: {host}\n".encode()
            resp = (f"HTTP/1.1 404 Not Found\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"Content-Type: text/plain\r\n\r\n").encode() + body
            writer.write(resp)
            await writer.drain()
            writer.close()
            return

        record_access(host, datetime.now(timezone.utc))
        target_port = routes[host]

        # Connect to upstream server (localhost resolves to IPv4 or IPv6)
        try:
            be_reader, be_writer = await asyncio.open_connection(
                "localhost", target_port)
        except (ConnectionRefusedError, OSError):
            body = f"Server on port {target_port} is not responding.\n".encode()
            resp = (f"HTTP/1.1 502 Bad Gateway\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"Content-Type: text/plain\r\n\r\n").encode() + body
            writer.write(resp)
            await writer.drain()
            writer.close()
            return

        # Rewrite Host header so backends accept the request, keep original
        # as X-Forwarded-Host.
        lines = header_bytes.split(b"\r\n")
        new_lines = []
        forwarded_added = False
        for line in lines:
            if line.lower().startswith(b"host:"):
                new_lines.append(f"Host: localhost:{target_port}".encode("latin-1"))
                new_lines.append(f"X-Forwarded-Host: {host}".encode("latin-1"))
                forwarded_added = True
            else:
                new_lines.append(line)
        be_writer.write(b"\r\n".join(new_lines) + rest)
        await be_writer.drain()

        # Bidirectional pipe (handles WebSocket, SSE, chunked, etc.)
        async def pipe(src, dst):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.CancelledError, OSError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(pipe(reader, be_writer), pipe(be_reader, writer))
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def cmd_proxy(args):
    """Run the reverse proxy daemon."""
    port = getattr(args, "port", DEFAULT_PROXY_PORT)
    routes = {}
    routes_mtime = 0.0

    def reload_routes():
        nonlocal routes, routes_mtime
        try:
            st = PROXY_ROUTES_FILE.stat()
            if st.st_mtime != routes_mtime:
                routes.clear()
                routes.update(load_proxy_routes())
                routes_mtime = st.st_mtime
                print(f"Routes reloaded ({len(routes)} entries)")
                for h in sorted(routes):
                    print(f"  http://{h}:{port} -> 127.0.0.1:{routes[h]}")
        except FileNotFoundError:
            if routes:
                routes.clear()
                print("Routes file removed — no routes active")

    async def handle(reader, writer):
        reload_routes()
        await _proxy_connection(reader, writer, routes)

    async def run():
        reload_routes()
        try:
            server = await asyncio.start_server(handle, "127.0.0.1", port)
        except OSError as e:
            print(f"Error: cannot bind to port {port}: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Proxy listening on http://127.0.0.1:{port}")
        print(f"Routes file: {PROXY_ROUTES_FILE}")
        print()
        async with server:
            await server.serve_forever()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nProxy stopped.")


def _tmux(*tmux_args):
    return subprocess.run(["tmux", *tmux_args], capture_output=True, text=True)

def tmux_session_exists(name):
    return _tmux("has-session", "-t", name).returncode == 0

def tmux_pane_paths(name):
    r = _tmux("list-panes", "-t", name, "-F", "#{pane_current_path}")
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]

def cmd_grid(args):
    repo_root = find_repo_root()
    proj = project_name(repo_root)
    # Source of truth is `git worktree list`, not sessions.json (which can be out of
    # sync with worktrees created/removed outside the orchestrator).
    wt = subprocess.run(["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
                        capture_output=True, text=True)
    worktrees = [p for p in worktree_paths_under(wt.stdout, worktree_base_dir(repo_root))
                 if Path(p).exists()]
    if not worktrees:
        print("No worktrees yet. Spawn one first.")
        return

    try:
        exists = tmux_session_exists(proj)
    except FileNotFoundError:
        print("Error: tmux is not installed.", file=sys.stderr)
        sys.exit(1)

    pane_cmd = grid_pane_command()
    created = False
    if not exists:
        _tmux("new-session", "-d", "-s", proj, "-c", worktrees[0], pane_cmd)
        created = True

    existing = tmux_pane_paths(proj)
    if created and worktrees[0] not in existing:
        existing.append(worktrees[0])  # the just-created pane may not report its path yet
    to_add = panes_to_create(worktrees, existing)
    for path in to_add:
        _tmux("split-window", "-t", proj, "-c", path, pane_cmd)
    _tmux("select-layout", "-t", proj, "tiled")
    _order_grid_panes(proj)

    print(f"Grid '{proj}': {len(worktrees)} worktree(s); {len(to_add)} pane(s) added"
          + (" (new session)" if created else ""))
    # Never auto-attach/switch — the caller (often an agent inside tmux) would hijack
    # the user's client. Print the command and let the user run it.
    if os.environ.get("TMUX"):
        print(f"Attach: tmux switch-client -t {proj}")
    else:
        print(f"Attach: tmux attach -t {proj}")


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

    p_status = sub.add_parser("status", help="Show all sessions and server health")
    p_status.add_argument("--json", action="store_true", help="Output status as JSON")

    p_logs = sub.add_parser("logs", help="Show server logs")
    p_logs.add_argument("name", help="Session name")
    p_logs.add_argument("server", nargs="?", default=None,
                        help="Server name (omit to show all)")
    p_logs.add_argument("-n", "--lines", type=int, default=50,
                        help="Lines to show (default: 50; 0 = all)")

    p_kill = sub.add_parser("kill", help="Stop all servers in a session")
    p_kill.add_argument("name", help="Session name")
    p_kill.add_argument("--remove", action="store_true",
                        help="Also remove worktree and logs")

    p_restart = sub.add_parser("restart", help="Stop and restart all servers in a session")
    p_restart.add_argument("name", help="Session name")

    p_cleanup = sub.add_parser("cleanup", help="Remove stopped sessions and worktrees")
    p_cleanup.add_argument("--force", action="store_true", help="Skip confirmation")

    p_proxy = sub.add_parser("proxy", help="Run the reverse proxy daemon")
    p_proxy.add_argument("-p", "--port", type=int, default=DEFAULT_PROXY_PORT,
                         help=f"Port to listen on (default: {DEFAULT_PROXY_PORT})")

    sub.add_parser("grid",
                   help="Bring up/reattach a project's tiled tmux grid (one claude pane per worktree)")

    args = parser.parse_args()
    commands = {
        "init": cmd_init,
        "spawn": cmd_spawn,
        "status": cmd_status,
        "logs": cmd_logs,
        "kill": cmd_kill,
        "restart": cmd_restart,
        "cleanup": cmd_cleanup,
        "proxy": cmd_proxy,
        "grid": cmd_grid,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()

"""Async command server — runs ON each Dev Box.

Accepts commands via HTTP and executes them asynchronously.
  POST /exec     → submit command, returns {jobId}
  GET /jobs/{id} → poll job status + results
  GET /health    → health check (includes version)
  POST /update   → trigger graceful restart for update

This eliminates devtunnel 504 timeouts for long-running commands.
"""

import json
import os
import secrets
import subprocess
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote

from agent_runtime import __version__ as _VERSION
from agent_runtime.persistent_shell import (
    available_shells as _available_persistent_shells,
    normalize_shell_kind,
    persistent_shell_supported,
    preferred_shell_kind,
    shared_manager as _shared_persistent_shell_manager,
)

# Exit code that signals the wrapper/CLI to pull updates and restart
RESTART_EXIT_CODE = 42


def _terminal_available() -> bool:
    """Check if terminal extras are installed (lazy import)."""
    try:
        from agent_runtime.terminal import is_available
        return is_available()
    except ImportError:
        return False


def _terminal_session_count() -> int:
    """Get active terminal session count (0 if terminal not available)."""
    try:
        from agent_runtime.terminal import active_session_count
        return active_session_count()
    except ImportError:
        return 0


def _exit_for_restart() -> None:
    """Exit the process for restart. Separate function so tests can patch it."""
    os._exit(RESTART_EXIT_CODE)


# In-memory job store (keyed by job ID)
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# Max jobs to keep in memory
MAX_JOBS = 100

# Headless persistent shell sessions (separate from browser terminal PTYs)
_persistent_shells = _shared_persistent_shell_manager()

# API key for authenticating dashboard requests (set on startup)
_api_key: str = ""

# Path to persist API key across restarts
_API_KEY_FILE = os.path.join(
    os.environ.get("LOCALAPPDATA", os.environ.get("HOME", ".")),
    "devpilot-agent", "api_key"
)


def _init_api_key() -> str:
    """Initialize the API key: load from disk, env, or generate new.

    Priority: env var > persisted file > generate new.
    Always persists the active key to disk so it survives restarts.
    """
    key = os.environ.get("agent_runtime_API_KEY", "")
    if not key:
        # Try loading from persisted file
        try:
            with open(_API_KEY_FILE, "r") as f:
                key = f.read().strip()
        except (FileNotFoundError, OSError):
            pass
    if not key:
        key = secrets.token_urlsafe(32)
    os.environ["agent_runtime_API_KEY"] = key
    # Persist to disk for next restart
    _persist_api_key(key)
    return key


def _persist_api_key(key: str) -> None:
    """Save the API key to disk so it survives restarts."""
    try:
        os.makedirs(os.path.dirname(_API_KEY_FILE), exist_ok=True)
        with open(_API_KEY_FILE, "w") as f:
            f.write(key)
    except OSError as e:
        print(f"[auth] WARNING: Could not persist API key: {e}")


def _normalize_env(raw_env: object) -> dict[str, str] | None:
    """Validate and normalize an optional env payload."""
    if raw_env is None:
        return None
    if not isinstance(raw_env, dict):
        raise ValueError("env must be an object of string key/value pairs")
    env: dict[str, str] = {}
    for key, value in raw_env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("env must be an object of string key/value pairs")
        env[key] = value
    return env


def _merged_env(env: dict[str, str] | None) -> dict[str, str]:
    """Return the process environment with an optional overlay."""
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return merged


def _job_process_alive(job: dict) -> bool:
    """Return whether the job still has an active running process."""
    thread = job.get("_thread")
    if thread is not None and thread.is_alive():
        return True
    session_key = job.get("_persistentSessionKey")
    if isinstance(session_key, str):
        info = _persistent_shells.get_session_info(session_key)
        return bool(info and info.get("activeCommand"))
    proc = job.get("_process")
    return proc is not None and proc.poll() is None


def _reader_thread(stream, chunks: list, job_id: str) -> None:
    """Read lines from a stream, updating the job's activity timestamp."""
    try:
        for line in stream:
            chunks.append(line)
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id]["lastOutputAt"] = time.time()
    except (ValueError, OSError):
        pass  # Stream closed
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _run_command(
    job_id: str,
    command: str,
    workdir: str,
    timeout: int,
    env: dict[str, str] | None = None,
) -> None:
    """Execute a command in a background thread and store the result.

    Uses Popen with reader threads so we can track output activity
    and expose the process handle for nudge/health checks.
    """
    start = time.time()
    proc = None
    try:
        with _jobs_lock:
            _jobs[job_id]["status"] = "running"
            _jobs[job_id]["lastOutputAt"] = start

        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            cwd=workdir or None,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_merged_env(env),
        )

        with _jobs_lock:
            _jobs[job_id]["_process"] = proc

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        t_out = threading.Thread(
            target=_reader_thread, args=(proc.stdout, stdout_chunks, job_id),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_reader_thread, args=(proc.stderr, stderr_chunks, job_id),
            daemon=True,
        )
        t_out.start()
        t_err.start()

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            t_out.join(timeout=5)
            t_err.join(timeout=5)
            with _jobs_lock:
                _jobs[job_id].update({
                    "status": "timeout",
                    "exitCode": -1,
                    "stdout": "".join(stdout_chunks),
                    "stderr": "".join(stderr_chunks) or "Command timed out",
                    "durationMs": int(timeout * 1000),
                    "timedOut": True,
                })
                _jobs[job_id].pop("_process", None)
            return

        # Process exited — wait for reader threads to drain
        t_out.join(timeout=10)
        t_err.join(timeout=10)

        status = "completed" if proc.returncode == 0 else "failed"
        with _jobs_lock:
            _jobs[job_id].update({
                "status": status,
                "exitCode": proc.returncode,
                "stdout": "".join(stdout_chunks),
                "stderr": "".join(stderr_chunks),
                "durationMs": int((time.time() - start) * 1000),
                "timedOut": False,
            })
            _jobs[job_id].pop("_process", None)
    except Exception as e:
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        with _jobs_lock:
            _jobs[job_id].update({
                "status": "failed",
                "exitCode": 1,
                "stdout": "",
                "stderr": str(e),
                "durationMs": int((time.time() - start) * 1000),
                "timedOut": False,
            })
            _jobs[job_id].pop("_process", None)


def _run_persistent_shell_command(
    job_id: str,
    session_key: str,
    shell: str,
    command: str,
    workdir: str,
    timeout: int,
    env: dict[str, str] | None = None,
    bootstrap_command: str = "",
    bootstrap_current_command: bool = False,
) -> None:
    """Execute a command inside a named persistent shell session."""
    start = time.time()

    with _jobs_lock:
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["lastOutputAt"] = start

    def on_output() -> None:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job:
                job["lastOutputAt"] = time.time()

    try:
        result = _persistent_shells.run_command(
            session_key,
            command,
            shell=shell,
            timeout=timeout,
            workdir=workdir,
            env=env,
            on_output=on_output,
            bootstrap_command=bootstrap_command,
            bootstrap_current_command=bootstrap_current_command,
        )
        status = "timeout" if result.timed_out else (
            "completed" if result.exit_code == 0 else "failed"
        )
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job:
                job.update({
                    "status": status,
                    "exitCode": result.exit_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "durationMs": result.duration_ms,
                    "timedOut": result.timed_out,
                })
    except Exception as e:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job:
                job.update({
                    "status": "failed",
                    "exitCode": 1,
                    "stdout": "",
                    "stderr": str(e),
                    "durationMs": int((time.time() - start) * 1000),
                    "timedOut": False,
                })


def _prune_old_jobs() -> None:
    """Remove oldest jobs if we exceed MAX_JOBS."""
    with _jobs_lock:
        if len(_jobs) > MAX_JOBS:
            sorted_ids = sorted(
                _jobs.keys(), key=lambda k: _jobs[k].get("submittedAt", 0)
            )
            for jid in sorted_ids[: len(_jobs) - MAX_JOBS]:
                if _jobs[jid].get("status") not in ("pending", "running"):
                    del _jobs[jid]


def _acp_available() -> bool:
    """Check if the ACP client module is available."""
    try:
        from agent_runtime.acp_client import run_acp_session_sync  # noqa: F401
        return True
    except ImportError:
        return False


def _run_acp_command(job_id: str, agent: str, prompt: str, workdir: str,
                     timeout: int, session_id: str | None,
                     model: str | None, effort: str | None,
                     env: dict[str, str] | None = None,
                     persistent_session_key: str | None = None,
                     persistent_shell: str | None = None) -> None:
    """Run an ACP session in a dedicated thread with its own event loop."""
    import shlex
    from agent_runtime.acp_client import (
        run_acp_session_sync, HeadlessApprovePolicy, ACPEvent,
    )

    with _jobs_lock:
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["startedAt"] = time.time()

    def on_event(event: ACPEvent) -> None:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job:
                job["lastOutputAt"] = time.time()

    try:
        agent_cmd = shlex.split(agent, posix=(os.name != "nt"))
        effective_workdir = workdir or os.getcwd()
        policy = HeadlessApprovePolicy(effective_workdir)
        result = run_acp_session_sync(
            agent_cmd=agent_cmd,
            prompt=prompt or None,
            workdir=effective_workdir,
            timeout=timeout,
            session_id=session_id,
            model=model,
            effort=effort,
            env=env,
            persistent_session_key=persistent_session_key,
            persistent_shell=persistent_shell,
            permission_policy=policy,
            on_event=on_event,
        )

        with _jobs_lock:
            job = _jobs.get(job_id)
            if job:
                # Only "end_turn" and "no_prompt" are success; everything else is failure
                if result.stop_reason in ("end_turn", "no_prompt"):
                    job["status"] = "completed"
                    job["exitCode"] = 0
                elif result.stop_reason == "timeout":
                    job["status"] = "timeout"
                    job["exitCode"] = 1
                else:
                    job["status"] = "failed"
                    job["exitCode"] = 1
                job["stdout"] = result.output_text
                job["stderr"] = result.stderr
                job["durationMs"] = int((time.time() - job.get("startedAt", time.time())) * 1000)
                job["acpSessionId"] = result.session_id
                job["stopReason"] = result.stop_reason
                job["events"] = [
                    {"type": e.type, "timestamp": e.timestamp, "data": e.data}
                    for e in result.events[-500:]  # Cap events in response
                ]
                if result.error:
                    job["acpError"] = result.error

    except Exception as e:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job:
                job["status"] = "failed"
                job["stderr"] = str(e)
                job["exitCode"] = 1
                job["durationMs"] = int((time.time() - job.get("startedAt", time.time())) * 1000)
                job["acpError"] = str(e)


class CommandHandler(BaseHTTPRequestHandler):
    """HTTP handler for the async command server."""

    def _check_api_key(self) -> bool:
        """Validate X-API-Key header. Returns True if valid, sends 401 if not."""
        key = self.headers.get("X-API-Key", "")
        if not key or not secrets.compare_digest(key, _api_key):
            self._json_response(401, {"error": "invalid or missing API key"})
            return False
        return True

    def _is_api_key_valid(self) -> bool:
        """Check if the provided API key matches, without sending a 401."""
        key = self.headers.get("X-API-Key", "")
        return bool(key) and secrets.compare_digest(key, _api_key)

    def do_GET(self) -> None:
        if self.path == "/health":
            # Minimal response for unauthenticated probes (load balancers, monitoring)
            if not self._is_api_key_valid():
                self._json_response(200, {"status": "ok", "authenticated": False})
                return
            execution_modes = ["shell"]
            if _acp_available():
                execution_modes.append("acp")
            if persistent_shell_supported():
                execution_modes.append("persistent_shell")
            self._json_response(
                200,
                {
                    "status": "ok",
                    "version": _VERSION,
                    "hostname": os.environ.get("COMPUTERNAME", "unknown"),
                    "jobs": len(_jobs),
                    "cwd": os.getcwd(),
                    "authenticated": True,
                    "terminal_supported": _terminal_available(),
                    "terminal_sessions": _terminal_session_count(),
                    "acp_supported": _acp_available(),
                    "execution_modes": execution_modes,
                    "persistent_shell_supported": persistent_shell_supported(),
                    "persistent_shell_shells": _available_persistent_shells(),
                    "persistent_shell_default_shell": preferred_shell_kind(),
                    "persistent_shell_sessions": _persistent_shells.active_session_count(),
                    "persistent_shell_max_sessions": _persistent_shells.max_sessions,
                    "persistent_shell_idle_timeout_seconds": _persistent_shells.idle_timeout,
                },
            )
            return

        # All other GET endpoints require authentication
        if not self._check_api_key():
            return

        if self.path == "/sessions":
            self._json_response(200, _persistent_shells.list_sessions())

        elif self.path.startswith("/sessions/"):
            session_key = unquote(self.path.split("/sessions/", 1)[1])
            info = _persistent_shells.get_session_info(session_key)
            if info is None:
                self._json_response(404, {"error": "session not found"})
            else:
                self._json_response(200, info)

        elif self.path.startswith("/jobs/"):
            job_id = self.path.split("/jobs/", 1)[1]
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job is not None:
                    resp = {k: v for k, v in job.items() if not k.startswith("_")}
                    resp["processAlive"] = _job_process_alive(job)
            if job is None:
                self._json_response(404, {"error": "job not found"})
            else:
                self._json_response(200, resp)

        elif self.path == "/jobs":
            with _jobs_lock:
                jobs_list = []
                for j in _jobs.values():
                    sanitized = {k: v for k, v in j.items() if not k.startswith("_")}
                    sanitized["processAlive"] = _job_process_alive(j)
                    jobs_list.append(sanitized)
            jobs_list.sort(key=lambda j: j.get("submittedAt", 0), reverse=True)
            self._json_response(200, jobs_list[:20])

        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._check_api_key():
            return

        if self.path == "/update":
            self._handle_update()
            return

        if self.path.startswith("/jobs/") and self.path.endswith("/nudge"):
            self._handle_nudge()
            return

        if self.path != "/exec":
            self._json_response(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length > 0 else {}

        mode = body.get("mode", "shell")
        if mode not in ("shell", "acp", "persistent_shell"):
            self._json_response(400, {"error": f"unsupported mode: {mode}"})
            return
        try:
            env = _normalize_env(body.get("env"))
        except ValueError as e:
            self._json_response(400, {"error": str(e)})
            return

        # ── ACP mode ────────────────────────────────────────────────────
        if mode == "acp":
            if not _acp_available():
                self._json_response(400, {"error": "ACP not available (acp_client module not found)"})
                return
            prompt = body.get("prompt", "")
            agent = body.get("agent", "copilot --acp --stdio")
            workdir = body.get("workdir", "")
            timeout = body.get("timeout", 300)
            acp_session_id = body.get("acp_session_id")
            acp_model = body.get("model")
            acp_effort = body.get("effort")
            persistent_session_key = body.get("persistent_session_key")
            persistent_shell = body.get("persistent_shell")

            if persistent_session_key is not None and not isinstance(persistent_session_key, str):
                self._json_response(400, {"error": "persistent_session_key must be a string"})
                return
            if persistent_shell is not None and not isinstance(persistent_shell, str):
                self._json_response(400, {"error": "persistent_shell must be a string"})
                return
            if persistent_session_key:
                if not persistent_shell:
                    self._json_response(400, {"error": "persistent_shell is required"})
                    return
                try:
                    persistent_shell = normalize_shell_kind(persistent_shell)
                except (ValueError, RuntimeError) as e:
                    self._json_response(400, {"error": str(e)})
                    return

            job_id = uuid.uuid4().hex[:12]
            with _jobs_lock:
                _jobs[job_id] = {
                    "jobId": job_id,
                    "status": "pending",
                    "command": f"[acp] {agent}",
                    "mode": "acp",
                    "submittedAt": time.time(),
                }
                if persistent_session_key:
                    _jobs[job_id]["_persistentSessionKey"] = persistent_session_key
                    _jobs[job_id]["persistentShell"] = persistent_shell

            thread = threading.Thread(
                target=_run_acp_command,
                args=(job_id, agent, prompt, workdir, timeout,
                      acp_session_id, acp_model, acp_effort, env,
                      persistent_session_key or None, persistent_shell or None),
                daemon=True,
            )
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id]["_thread"] = thread
            thread.start()
            _prune_old_jobs()
            self._json_response(202, {"jobId": job_id, "status": "pending"})
            return

        if mode == "persistent_shell":
            if not persistent_shell_supported():
                self._json_response(400, {"error": "persistent shell is not available"})
                return
            command = body.get("command", "")
            session_key = body.get("sessionKey", "")
            shell = body.get("shell", "")
            workdir = body.get("workdir", "")
            timeout = body.get("timeout", 300)
            bootstrap_command = body.get("bootstrapCommand", "")
            bootstrap_current_command = body.get("bootstrapCurrentCommand", False)

            if not command:
                self._json_response(400, {"error": "command is required"})
                return
            if not isinstance(session_key, str) or not session_key:
                self._json_response(400, {"error": "sessionKey is required"})
                return
            if not isinstance(shell, str) or not shell:
                self._json_response(400, {"error": "shell is required"})
                return
            if bootstrap_command and not isinstance(bootstrap_command, str):
                self._json_response(400, {"error": "bootstrapCommand must be a string"})
                return
            if not isinstance(bootstrap_current_command, bool):
                self._json_response(
                    400,
                    {"error": "bootstrapCurrentCommand must be a boolean"},
                )
                return
            try:
                shell_kind = normalize_shell_kind(shell)
            except (ValueError, RuntimeError) as e:
                self._json_response(400, {"error": str(e)})
                return

            job_id = uuid.uuid4().hex[:12]
            with _jobs_lock:
                _jobs[job_id] = {
                    "jobId": job_id,
                    "status": "pending",
                    "command": command,
                    "mode": "persistent_shell",
                    "sessionKey": session_key,
                    "shell": shell_kind,
                    "submittedAt": time.time(),
                    "lastOutputAt": time.time(),
                    "_persistentSessionKey": session_key,
                }

            thread = threading.Thread(
                target=_run_persistent_shell_command,
                args=(
                    job_id,
                    session_key,
                    shell_kind,
                    command,
                    workdir,
                    timeout,
                    env,
                    bootstrap_command,
                    bootstrap_current_command,
                ),
                daemon=True,
            )
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id]["_thread"] = thread
            thread.start()
            _prune_old_jobs()
            self._json_response(202, {"jobId": job_id, "status": "pending"})
            return

        # ── Shell mode (default) ────────────────────────────────────────
        command = body.get("command", "")
        workdir = body.get("workdir", "")
        timeout = body.get("timeout", 300)

        if not command:
            self._json_response(400, {"error": "command is required"})
            return

        job_id = uuid.uuid4().hex[:12]
        with _jobs_lock:
            _jobs[job_id] = {
                "jobId": job_id,
                "status": "pending",
                "command": command,
                "submittedAt": time.time(),
            }

        thread = threading.Thread(
            target=_run_command,
            args=(job_id, command, workdir, timeout, env),
            daemon=True,
        )
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id]["_thread"] = thread
        thread.start()

        _prune_old_jobs()
        self._json_response(202, {"jobId": job_id, "status": "pending"})

    def _handle_nudge(self) -> None:
        """Write to a running job's stdin to nudge a stalled process."""
        # Extract job ID: /jobs/{id}/nudge
        parts = self.path.split("/")
        job_id = parts[2] if len(parts) >= 4 else ""

        with _jobs_lock:
            job = _jobs.get(job_id)
        if job is None:
            self._json_response(404, {"error": "job not found"})
            return

        session_key = job.get("_persistentSessionKey")
        if isinstance(session_key, str):
            nudged = _persistent_shells.nudge(session_key)
            self._json_response(200, {
                "nudged": nudged,
                "reason": "" if nudged else "persistent session not running",
                "processAlive": _job_process_alive(job),
            })
            return

        proc = job.get("_process")
        if proc is None or proc.poll() is not None:
            self._json_response(200, {
                "nudged": False,
                "reason": "process not running",
                "processAlive": False,
            })
            return

        try:
            proc.stdin.write("\n")
            proc.stdin.flush()
            self._json_response(200, {"nudged": True, "processAlive": True})
        except (OSError, ValueError) as e:
            self._json_response(200, {
                "nudged": False,
                "reason": str(e),
                "processAlive": proc.poll() is None,
            })

    def do_DELETE(self) -> None:
        if not self._check_api_key():
            return

        if not self.path.startswith("/sessions/"):
            self._json_response(404, {"error": "not found"})
            return

        session_key = unquote(self.path.split("/sessions/", 1)[1])
        info = _persistent_shells.get_session_info(session_key)
        if info is None:
            self._json_response(404, {"error": "session not found"})
            return

        _persistent_shells.close_session(session_key)
        self._json_response(200, {"deleted": True, "sessionKey": session_key})

    def _handle_update(self) -> None:
        """Trigger a graceful restart for update.

        Rejects if any jobs are currently running. On success, responds
        with 200 then exits with RESTART_EXIT_CODE so the wrapper can
        pull updates and restart the server.
        """
        with _jobs_lock:
            running = [
                j for j in _jobs.values()
                if j.get("status") in ("pending", "running")
            ]

        if running:
            self._json_response(409, {
                "error": "cannot update while jobs are running",
                "running_jobs": len(running),
            })
            return

        # Also block if terminal sessions are active
        terminal_count = _terminal_session_count()
        if terminal_count > 0:
            self._json_response(409, {
                "error": "cannot update while terminal sessions are active",
                "terminal_sessions": terminal_count,
            })
            return

        persistent_sessions = _persistent_shells.active_session_count()
        if persistent_sessions > 0:
            self._json_response(409, {
                "error": "cannot update while persistent shell sessions are active",
                "persistent_sessions": persistent_sessions,
            })
            return

        self._json_response(200, {
            "status": "updating",
            "version": _VERSION,
            "message": "Server will restart for update",
        })

        def _shutdown() -> None:
            time.sleep(1)
            _exit_for_restart()

        threading.Thread(target=_shutdown, daemon=True).start()

    def _json_response(self, status: int, data: object) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {args[0] if args else ''}")


def start_server(port: int = 8585) -> None:
    """Start the command server."""
    global _api_key
    _api_key = _init_api_key()
    print(f"DevPilot Agent v{_VERSION}")
    print(f"Listening on 0.0.0.0:{port}")
    print(
        "Endpoints: POST /exec, POST /update, "
        "GET /jobs/{id}, GET /health, GET /sessions, GET/DELETE /sessions/{key}"
    )
    print(f"API key: configured ({len(_api_key)} chars)")

    # Start terminal WebSocket server if extras are installed (fix C4: after API key init)
    _start_terminal_if_available(port + 1, _api_key)

    server = HTTPServer(("0.0.0.0", port), CommandHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


def _start_terminal_if_available(terminal_port: int, api_key: str) -> None:
    """Start the terminal WebSocket server in a background thread if extras installed."""
    try:
        from agent_runtime.terminal import is_available, start_terminal_server
        if not is_available():
            print("[terminal] Extras not installed (pip install devpilot-agent[terminal])")
            return

        import asyncio

        def _run() -> None:
            asyncio.run(start_terminal_server(port=terminal_port, api_key=api_key))

        threading.Thread(target=_run, daemon=True).start()
    except ImportError:
        pass

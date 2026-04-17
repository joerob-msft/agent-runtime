"""Headless PowerShell-family session manager for persistent workflow state.

This module provides the shared long-lived shell sessions used by the public
`/exec` persistent-shell HTTP mode and by headless ACP terminal execution.
It preserves shell-local state such as functions, variables, environment, and
current location across multiple commands within the same named session.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence, TextIO

logger = logging.getLogger("devpilot.persistent_shell")

_DEFAULT_MAX_SESSIONS_ENV = "DEVPILOT_PERSISTENT_SHELL_MAX_SESSIONS"
_DEFAULT_IDLE_TIMEOUT_ENV = "DEVPILOT_PERSISTENT_SHELL_IDLE_TIMEOUT_SECONDS"

_SHELL_ALIASES = {
    "pwsh": "pwsh",
    "pwsh.exe": "pwsh",
    "powershell": "powershell",
    "powershell.exe": "powershell",
    "windows-powershell": "powershell",
}

_SHELL_CANDIDATES = {
    "pwsh": ("pwsh", "pwsh.exe"),
    "powershell": ("powershell.exe", "powershell"),
}


def available_shells() -> dict[str, bool]:
    """Return shell availability for the supported PowerShell-family hosts."""
    return {
        kind: _resolve_shell_executable(kind) is not None
        for kind in ("pwsh", "powershell")
    }


def persistent_shell_supported() -> bool:
    """Return True when at least one supported shell host is available."""
    return any(available_shells().values())


def preferred_shell_kind() -> Optional[str]:
    """Return the preferred shell kind for persistent sessions."""
    shells = available_shells()
    if shells["pwsh"]:
        return "pwsh"
    if shells["powershell"]:
        return "powershell"
    return None


def normalize_shell_kind(shell: Optional[str]) -> str:
    """Normalize a user-provided shell name to a canonical kind."""
    if not shell:
        preferred = preferred_shell_kind()
        if preferred:
            return preferred
        raise RuntimeError("no supported PowerShell shell is available")
    canonical = _SHELL_ALIASES.get(shell.lower())
    if canonical is None:
        raise ValueError(f"unsupported shell: {shell}")
    return canonical


def shell_command_for(shell: str) -> tuple[str, ...]:
    """Return the process command line for a canonical shell kind."""
    shell_kind = normalize_shell_kind(shell)
    executable = _resolve_shell_executable(shell_kind)
    if executable is None:
        raise RuntimeError(f"shell is not available: {shell_kind}")
    args = [executable, "-NoLogo", "-NoProfile", "-NoExit"]
    if os.name == "nt":
        args.extend(["-ExecutionPolicy", "Bypass"])
    args.extend(["-Command", "-"])
    return tuple(args)


def _resolve_shell_executable(shell_kind: str) -> Optional[str]:
    """Resolve the executable for a canonical shell kind."""
    canonical = _SHELL_ALIASES.get(shell_kind.lower(), shell_kind.lower())
    for candidate in _SHELL_CANDIDATES.get(canonical, ()):
        executable = shutil.which(candidate)
        if executable:
            return executable
    return None


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """Read a bounded integer environment variable."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default
    return max(value, minimum)


def _env_float(name: str, default: float) -> float:
    """Read a floating-point environment variable."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default


@dataclass
class PersistentCommandResult:
    """Result of one command executed inside a persistent shell session."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False


@dataclass
class _ActiveCommand:
    """In-flight command state shared with the reader threads."""

    marker: str
    started_at: float
    done: threading.Event = field(default_factory=threading.Event)
    stdout_chunks: list[str] = field(default_factory=list)
    stderr_chunks: list[str] = field(default_factory=list)
    exit_code: int = -1
    on_output: Optional[Callable[[], None]] = None
    on_chunk: Optional[Callable[[str, bool], None]] = None


class PersistentShellSession:
    """One long-lived PowerShell process that executes commands sequentially."""

    STARTUP_LINES: tuple[str, ...] = (
        "$ProgressPreference = 'SilentlyContinue'",
        "function global:prompt { '' }",
    )

    def __init__(self, shell: str) -> None:
        self.shell_kind = normalize_shell_kind(shell)
        self.shell_command = shell_command_for(self.shell_kind)
        self._proc: Optional[subprocess.Popen[str]] = None
        self._proc_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._active: Optional[_ActiveCommand] = None
        self._seed_workdir = ""
        self._seed_env: dict[str, str] = {}
        self._bootstrap_command = ""
        self._seed_applied = False
        self._bootstrap_applied = True
        self.created_at = 0.0
        self.last_used_at = 0.0
        self.last_output_at = 0.0

    @staticmethod
    def _ps_literal(value: str) -> str:
        """Return a single-quoted PowerShell string literal."""
        return "'" + value.replace("'", "''") + "'"

    @staticmethod
    def _normalize_env(env: Optional[dict[str, str]]) -> dict[str, str]:
        """Validate and normalize optional environment overlays."""
        normalized: dict[str, str] = {}
        if not env:
            return normalized
        for key, value in env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("env must be an object of string key/value pairs")
            normalized[key] = value
        return normalized

    def is_alive(self) -> bool:
        """Return True when the underlying PowerShell process is alive."""
        with self._proc_lock:
            proc = self._proc
        return proc is not None and proc.poll() is None

    def has_active_command(self) -> bool:
        """Return True when a command is currently running."""
        with self._state_lock:
            return self._active is not None and not self._active.done.is_set()

    def is_idle_expired(self, now: float, idle_timeout: float) -> bool:
        """Return True when the session is idle and past the idle timeout."""
        if idle_timeout <= 0:
            return False
        if self.has_active_command():
            return False
        return (now - self.last_used_at) > idle_timeout

    def close(self) -> None:
        """Terminate the shell process and drop any active command state."""
        proc = self._detach_process()
        self._terminate_process(proc)

    def reset(self) -> None:
        """Reset the shell process but keep the session definition."""
        self.close()

    def remember_seed(self, *, workdir: str = "", env: Optional[dict[str, str]] = None) -> None:
        """Record the initial workdir/env that should be replayed on process start."""
        normalized_env = self._normalize_env(env)
        with self._proc_lock:
            if workdir:
                self._seed_workdir = workdir
            if normalized_env:
                self._seed_env = normalized_env

    def set_bootstrap_command(
        self,
        command: str,
        *,
        current_command: bool = False,
    ) -> None:
        """Record a bootstrap command that should replay after process resets."""
        with self._proc_lock:
            self._bootstrap_command = command
            self._bootstrap_applied = current_command or not bool(command)

    def _detach_process(self) -> Optional[subprocess.Popen[str]]:
        """Detach and clear the current process reference."""
        with self._proc_lock:
            proc = self._proc
            self._proc = None
            self._seed_applied = False
            self._bootstrap_applied = not bool(self._bootstrap_command)
        with self._state_lock:
            self._active = None
        return proc

    @staticmethod
    def _terminate_process(proc: Optional[subprocess.Popen[str]]) -> None:
        """Terminate one detached shell process."""
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        except OSError:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def run_command(
        self,
        command: str,
        *,
        timeout: int = 300,
        on_output: Optional[Callable[[], None]] = None,
        on_chunk: Optional[Callable[[str, bool], None]] = None,
        bootstrap_command: str = "",
        bootstrap_current_command: bool = False,
    ) -> PersistentCommandResult:
        """Run one command inside the persistent shell.

        Commands are serialized per session. If a command times out, the entire
        process is reset to restore a known-good state before the next call.
        """
        start = time.time()
        marker = f"__DEVPILOT_DONE__{uuid.uuid4().hex}"

        with self._command_lock:
            if bootstrap_command:
                self.set_bootstrap_command(
                    bootstrap_command,
                    current_command=bootstrap_current_command,
                )
            self._ensure_process()
            run_seed = False
            run_bootstrap = False
            with self._proc_lock:
                proc = self._proc
                if proc is None or proc.stdin is None:
                    raise RuntimeError("Persistent shell failed to start")
                run_seed = not self._seed_applied
                run_bootstrap = (
                    bool(self._bootstrap_command)
                    and not self._bootstrap_applied
                    and not bootstrap_current_command
                )

            active = _ActiveCommand(
                marker=marker,
                started_at=start,
                on_output=on_output,
                on_chunk=on_chunk,
            )
            with self._state_lock:
                self._active = active
            self.last_used_at = start
            self.last_output_at = start

            try:
                with self._proc_lock:
                    proc = self._proc
                    if proc is None or proc.stdin is None:
                        raise RuntimeError("Persistent shell failed to start")
                    proc.stdin.write(
                        self._build_script(
                            marker=marker,
                            command=command,
                            run_seed=run_seed,
                            run_bootstrap=run_bootstrap,
                        )
                    )
                    proc.stdin.flush()
                    if run_seed:
                        self._seed_applied = True
                    if run_bootstrap or bootstrap_current_command:
                        self._bootstrap_applied = True
            except OSError as exc:
                self.close()
                return PersistentCommandResult(
                    exit_code=1,
                    stdout="",
                    stderr=str(exc),
                    duration_ms=int((time.time() - start) * 1000),
                    timed_out=False,
                )

            deadline = start + timeout
            while time.time() < deadline:
                if active.done.wait(timeout=0.1):
                    break
                if not self.is_alive():
                    break

            duration_ms = int((time.time() - start) * 1000)
            stdout = "".join(active.stdout_chunks)
            stderr = "".join(active.stderr_chunks)

            with self._state_lock:
                if self._active is active:
                    self._active = None

            if active.done.is_set():
                return PersistentCommandResult(
                    exit_code=active.exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    duration_ms=duration_ms,
                    timed_out=False,
                )

            if self.is_alive():
                self.close()
                return PersistentCommandResult(
                    exit_code=-1,
                    stdout=stdout,
                    stderr=stderr or "Command timed out",
                    duration_ms=duration_ms,
                    timed_out=True,
                )

            self.close()
            return PersistentCommandResult(
                exit_code=1,
                stdout=stdout,
                stderr=stderr or "Persistent shell exited unexpectedly",
                duration_ms=duration_ms,
                timed_out=False,
            )

    def nudge(self) -> bool:
        """Send a newline to the active shell session."""
        with self._proc_lock:
            proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return False
        if not self.has_active_command():
            return False
        try:
            proc.stdin.write("\n")
            proc.stdin.flush()
            self.last_used_at = time.time()
            return True
        except OSError:
            return False

    def info(self, session_key: str) -> dict[str, object]:
        """Return health information for this persistent session."""
        with self._state_lock:
            active = self._active
            active_command = active is not None and not active.done.is_set()
        process_alive = self.is_alive()
        if active_command:
            status = "running"
        elif process_alive:
            status = "idle"
        else:
            status = "stopped"
        return {
            "sessionKey": session_key,
            "shell": self.shell_kind,
            "status": status,
            "createdAt": self.created_at,
            "lastUsedAt": self.last_used_at,
            "lastOutputAt": self.last_output_at or None,
            "processAlive": process_alive,
            "activeCommand": active_command,
        }

    def _ensure_process(self) -> None:
        """Start a PowerShell process if the current one is not alive."""
        if self.is_alive():
            return
        self._terminate_process(self._detach_process())
        proc = subprocess.Popen(
            list(self.shell_command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        with self._proc_lock:
            self._proc = proc
            self.created_at = time.time()
            self.last_used_at = self.created_at
            self._seed_applied = False
            self._bootstrap_applied = not bool(self._bootstrap_command)

        assert proc.stdout is not None
        assert proc.stderr is not None
        threading.Thread(
            target=self._reader_loop,
            args=(proc.stdout, True),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._reader_loop,
            args=(proc.stderr, False),
            daemon=True,
        ).start()

        if proc.stdin is not None:
            proc.stdin.write("\n".join(self.STARTUP_LINES) + "\n")
            proc.stdin.flush()

    def _reader_loop(self, stream: TextIO, is_stdout: bool) -> None:
        """Continuously drain one process stream."""
        try:
            for line in iter(stream.readline, ""):
                self._handle_line(line, is_stdout=is_stdout)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _handle_line(self, line: str, *, is_stdout: bool) -> None:
        """Route one output line to the current active command."""
        callback = None
        chunk_callback = None
        with self._state_lock:
            active = self._active
            if active is None or active.done.is_set():
                return
            stripped = line.rstrip("\r\n")
            if is_stdout and stripped.startswith(active.marker + ":"):
                exit_text = stripped[len(active.marker) + 1:].strip()
                try:
                    active.exit_code = int(exit_text)
                except ValueError:
                    active.exit_code = 1
                active.done.set()
                self.last_used_at = time.time()
                self.last_output_at = self.last_used_at
                return
            if is_stdout:
                active.stdout_chunks.append(line)
            else:
                active.stderr_chunks.append(line)
            self.last_used_at = time.time()
            self.last_output_at = self.last_used_at
            callback = active.on_output
            chunk_callback = active.on_chunk
        if chunk_callback is not None:
            try:
                chunk_callback(line, is_stdout)
            except Exception:
                logger.debug("persistent shell on_chunk callback failed", exc_info=True)
        if callback is not None:
            try:
                callback()
            except Exception:
                logger.debug("persistent shell on_output callback failed", exc_info=True)

    def _build_script(
        self,
        *,
        marker: str,
        command: str,
        run_seed: bool,
        run_bootstrap: bool,
    ) -> str:
        """Build one PowerShell script that runs in the current session scope."""
        encoded_command = base64.b64encode(command.encode("utf-8")).decode("ascii")
        lines = [
            "$ProgressPreference = 'SilentlyContinue'",
            "$global:LASTEXITCODE = 0",
            f"$__devpilotMarker = {self._ps_literal(marker)}",
            "$__devpilotExit = 0",
            "$__devpilotContinue = $true",
        ]
        if run_seed:
            for key, value in self._seed_env.items():
                lines.append(
                    "Set-Item -Path ('Env:' + "
                    f"{self._ps_literal(key)}) -Value {self._ps_literal(value)}"
                )
            if self._seed_workdir:
                lines.append(
                    f"Set-Location -LiteralPath {self._ps_literal(self._seed_workdir)}"
                )
        if run_bootstrap and self._bootstrap_command:
            encoded_bootstrap = base64.b64encode(
                self._bootstrap_command.encode("utf-8")
            ).decode("ascii")
            lines.extend(
                [
                    "$__devpilotBootstrap = [Text.Encoding]::UTF8.GetString("
                    f"[Convert]::FromBase64String('{encoded_bootstrap}'))",
                    "try {",
                    "    Invoke-Expression $__devpilotBootstrap | Out-Null",
                    "    if ($LASTEXITCODE -is [int] -and $LASTEXITCODE -ne 0) {",
                    "        $__devpilotExit = [int]$LASTEXITCODE",
                    "        $__devpilotContinue = $false",
                    "    } elseif (-not $?) {",
                    "        $__devpilotExit = 1",
                    "        $__devpilotContinue = $false",
                    "    }",
                    "} catch {",
                    "    $_ | Out-String | Write-Error",
                    "    $__devpilotExit = 1",
                    "    $__devpilotContinue = $false",
                    "}",
                    "if ($__devpilotContinue) {",
                    "    $global:LASTEXITCODE = 0",
                    "}",
                ]
            )
        lines.extend(
            [
                "if ($__devpilotContinue) {",
                "    $__devpilotCommand = [Text.Encoding]::UTF8.GetString(",
                f"        [Convert]::FromBase64String('{encoded_command}'))",
                "    try {",
                "        Invoke-Expression $__devpilotCommand",
                "        if ($LASTEXITCODE -is [int]) {",
                "            $__devpilotExit = [int]$LASTEXITCODE",
                "        } elseif (-not $?) {",
                "            $__devpilotExit = 1",
                "        }",
                "    } catch {",
                "        $_ | Out-String | Write-Error",
                "        $__devpilotExit = 1",
                "    }",
                "}",
                'Write-Output ($__devpilotMarker + ":" + $__devpilotExit)',
            ]
        )
        # PowerShell's stdin-driven interactive parser does not execute a
        # multi-line block (for example try/catch) until it receives a blank
        # line after the block. Without this extra newline the shell keeps
        # waiting for more input and the command appears to hang.
        return "\n".join(lines) + "\n\n"


class PersistentShellManager:
    """Manage a bounded set of reusable PowerShell sessions."""

    def __init__(
        self,
        *,
        max_sessions: int = 0,
        idle_timeout: float = 0.0,
    ) -> None:
        self.max_sessions = max_sessions or _env_int(
            _DEFAULT_MAX_SESSIONS_ENV, 32
        )
        self.idle_timeout = idle_timeout
        self._sessions: dict[str, PersistentShellSession] = {}
        self._lock = threading.Lock()

    def run_command(
        self,
        session_key: str,
        command: str,
        *,
        shell: Optional[str] = None,
        timeout: int = 300,
        workdir: str = "",
        env: Optional[dict[str, str]] = None,
        on_output: Optional[Callable[[], None]] = None,
        on_chunk: Optional[Callable[[str, bool], None]] = None,
        bootstrap_command: str = "",
        bootstrap_current_command: bool = False,
    ) -> PersistentCommandResult:
        """Run a command in the named session, creating it on first use."""
        if not session_key:
            raise ValueError("session_key is required")
        shell_kind = normalize_shell_kind(shell)
        session = self._get_or_create_session(
            session_key,
            shell_kind,
            workdir=workdir,
            env=env,
        )
        return session.run_command(
            command,
            timeout=timeout,
            on_output=on_output,
            on_chunk=on_chunk,
            bootstrap_command=bootstrap_command,
            bootstrap_current_command=bootstrap_current_command,
        )

    def reset_session(self, session_key: str) -> None:
        """Terminate one session's process but keep its bootstrap definition."""
        with self._lock:
            session = self._sessions.get(session_key)
        if session is not None:
            session.reset()

    def close_session(self, session_key: str) -> None:
        """Terminate and remove one session."""
        with self._lock:
            session = self._sessions.pop(session_key, None)
        if session is not None:
            session.close()

    def close_all(self) -> None:
        """Terminate and remove all sessions."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def active_session_count(self) -> int:
        """Return the number of tracked persistent sessions."""
        with self._lock:
            self._prune_locked()
            return len(self._sessions)

    def get_session_info(self, session_key: str) -> Optional[dict[str, object]]:
        """Return one session's health info, if tracked and alive."""
        with self._lock:
            self._prune_locked()
            session = self._sessions.get(session_key)
            if session is None:
                return None
            return session.info(session_key)

    def list_sessions(self) -> list[dict[str, object]]:
        """Return health info for all tracked sessions."""
        with self._lock:
            self._prune_locked()
            return [
                self._sessions[key].info(key)
                for key in sorted(self._sessions)
            ]

    def nudge(self, session_key: str) -> bool:
        """Send a newline to a running command in the named session."""
        with self._lock:
            self._prune_locked()
            session = self._sessions.get(session_key)
        if session is None:
            return False
        return session.nudge()

    def session_keys(self) -> list[str]:
        """Return the currently tracked session keys."""
        with self._lock:
            self._prune_locked()
            return sorted(self._sessions)

    def _get_or_create_session(
        self,
        session_key: str,
        shell_kind: str,
        *,
        workdir: str = "",
        env: Optional[dict[str, str]] = None,
    ) -> PersistentShellSession:
        with self._lock:
            self._prune_locked()
            session = self._sessions.get(session_key)
            if session is not None:
                if session.shell_kind != shell_kind:
                    raise RuntimeError(
                        f"session '{session_key}' already uses shell '{session.shell_kind}'"
                    )
                return session
            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError("maximum persistent shell sessions reached")
            session = PersistentShellSession(shell_kind)
            session.remember_seed(workdir=workdir, env=env)
            self._sessions[session_key] = session
            return session

    def _prune_locked(self) -> None:
        now = time.time()
        expired = [
            key
            for key, session in self._sessions.items()
            if session.is_idle_expired(now, self.idle_timeout)
        ]
        for key in expired:
            session = self._sessions.pop(key)
            session.close()


_SHARED_MANAGER = PersistentShellManager(
    idle_timeout=_env_float(_DEFAULT_IDLE_TIMEOUT_ENV, 0.0)
)


def shared_manager() -> PersistentShellManager:
    """Return the process-wide persistent shell manager."""
    return _SHARED_MANAGER

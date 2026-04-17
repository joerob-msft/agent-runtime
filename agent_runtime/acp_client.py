"""Headless ACP client — runs AI agents via Agent Client Protocol (stdio).

Standalone module with no imports from agent_runtime.server, .cli, or
.terminal. Designed for reuse in any automation system that needs to
drive an ACP-compatible agent (Copilot CLI, Claude Code, Gemini, etc.)
without an interactive editor.

Uses stdlib only: asyncio, json, subprocess, pathlib, time, dataclasses.

Usage:
    result = await run_acp_session(
        agent_cmd=["copilot", "--acp", "--stdio"],
        prompt="Implement the login feature",
        workdir="/path/to/repo",
    )
    print(result.output_text)
    print(result.session_id)
    for event in result.events:
        print(event)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from agent_runtime.persistent_shell import (
    PersistentShellManager,
    normalize_shell_kind,
    shared_manager as _shared_persistent_shell_manager,
)

logger = logging.getLogger("devpilot.acp")

# ACP protocol version (v1 stable)
PROTOCOL_VERSION = 1

# Max accumulated events before oldest are dropped
MAX_EVENTS = 2000
# Max accumulated text chars before truncation
MAX_TEXT_CHARS = 2_000_000


# ── Data types ───────────────────────────────────────────────────────────────


@dataclass
class ACPEvent:
    """A structured event from an ACP session."""

    type: str  # "message", "tool_call", "tool_call_update", "file_read",
               # "file_write", "terminal_run", "permission", "thought"
    timestamp: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)


@dataclass
class ACPResult:
    """Result of a completed ACP session prompt."""

    session_id: str = ""
    output_text: str = ""
    events: list[ACPEvent] = field(default_factory=list)
    stop_reason: str = ""  # "end_turn", "cancelled", "error", "timeout"
    stderr: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    error: str = ""


@dataclass
class _PersistentTerminalJob:
    """Logical ACP terminal backed by a shared persistent shell session."""

    manager: PersistentShellManager
    session_key: str
    shell: str
    command: str
    args: list[str]
    cwd: str
    env: dict[str, str]
    timeout: int
    _chunks: list[str] = field(default_factory=list)
    _read_offset: int = 0
    exit_code: int = -1
    timed_out: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def _ps_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        result = self.manager.run_command(
            self.session_key,
            self._build_command(),
            shell=self.shell,
            timeout=self.timeout,
            workdir=self.cwd,
            env=self.env,
            on_chunk=self._on_chunk,
        )
        with self._lock:
            if not self.done.is_set() and not self._chunks and (result.stdout or result.stderr):
                self._chunks.append(result.stdout + result.stderr)
            if not self.done.is_set():
                self.exit_code = result.exit_code
                self.timed_out = result.timed_out
        self.done.set()

    def _on_chunk(self, chunk: str, _is_stdout: bool) -> None:
        with self._lock:
            self._chunks.append(chunk)

    def _build_command(self) -> str:
        parts = ["&", self._ps_literal(self.command)]
        for arg in self.args:
            parts.append(self._ps_literal(str(arg)))
        return " ".join(parts)

    def read_output(self) -> tuple[str, bool]:
        with self._lock:
            text = "".join(self._chunks)
            output = text[self._read_offset:]
            self._read_offset = len(text)
            is_complete = self.done.is_set()
        return output, is_complete

    def wait_for_exit(self, timeout: float) -> int:
        if self.done.wait(timeout):
            with self._lock:
                return self.exit_code
        self.kill(timed_out=True)
        return -1

    def kill(self, *, timed_out: bool = False) -> None:
        if self.done.is_set():
            return
        self.manager.reset_session(self.session_key)
        with self._lock:
            self.exit_code = -1
            self.timed_out = timed_out
        self.done.set()


# ── Permission policies ─────────────────────────────────────────────────────


class PermissionPolicy:
    """Base permission policy — denies all requests (safe default for OSS)."""

    def decide(self, tool_name: str, options: list[dict],
               params: dict) -> Optional[dict]:
        """Return an option dict to approve, or None to deny."""
        return None


class HeadlessApprovePolicy(PermissionPolicy):
    """Auto-approve permissions scoped to a workdir (for DevPilot headless)."""

    def __init__(self, workdir: str) -> None:
        self.workdir = Path(workdir).resolve()

    # Tools that are safe to approve without a path check
    _SAFE_TOOLS = frozenset({"read_file", "view_file", "list_directory", "grep",
                             "search", "glob", "read_text_file"})

    def decide(self, tool_name: str, options: list[dict],
               params: dict) -> Optional[dict]:
        tool_call = params.get("toolCall", {})
        raw_input = tool_call.get("input", {}) or tool_call.get("rawInput", {})

        # Check multiple common path-like keys
        path = ""
        for key in ("path", "file", "filepath", "filePath", "target", "cwd"):
            path = raw_input.get(key, "")
            if path:
                break

        if path:
            try:
                resolved = Path(path).resolve()
                if not resolved.is_relative_to(self.workdir):
                    logger.warning("Permission denied: %s outside workdir %s",
                                   path, self.workdir)
                    return None
            except (ValueError, OSError):
                return None
            return options[0] if options else None

        # No path found — approve only known safe tools, deny others
        if tool_name in self._SAFE_TOOLS:
            return options[0] if options else None

        logger.warning("Permission denied for unknown pathless tool: %s", tool_name)
        return None


# ── NDJSON transport ─────────────────────────────────────────────────────────


class NDJSONTransport:
    """Buffered NDJSON reader/writer over asyncio streams."""

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._buffer = b""

    async def send(self, msg: dict) -> None:
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()

    async def recv(self, timeout: float = 30.0) -> Optional[dict]:
        """Read one NDJSON message. Returns None on EOF or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            # Check buffer first
            if b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                line = line.strip()
                if line:
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON: %s", line[:200])
                        continue

            # Read more data
            remaining = max(0.1, deadline - time.time())
            try:
                chunk = await asyncio.wait_for(
                    self._reader.read(65536), timeout=min(2.0, remaining)
                )
            except asyncio.TimeoutError:
                continue
            except Exception:
                return None

            if not chunk:
                return None  # EOF
            self._buffer += chunk

        return None  # Timeout


# ── JSON-RPC helpers ─────────────────────────────────────────────────────────


class RPCClient:
    """Minimal JSON-RPC 2.0 client over NDJSON transport."""

    def __init__(self, transport: NDJSONTransport) -> None:
        self._transport = transport
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}

    def _make_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def request(self, method: str, params: dict,
                      timeout: float = 30.0) -> dict:
        """Send a request and wait for the response."""
        msg_id = self._make_id()
        msg = {"jsonrpc": "2.0", "method": method, "id": msg_id, "params": params}
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future
        await self._transport.send(msg)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise

    async def respond(self, msg_id: Any, result: dict) -> None:
        """Send a response to an agent request."""
        msg = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        await self._transport.send(msg)

    async def respond_error(self, msg_id: Any, code: int, message: str) -> None:
        """Send an error response."""
        msg = {"jsonrpc": "2.0", "id": msg_id,
               "error": {"code": code, "message": message}}
        await self._transport.send(msg)

    def resolve(self, msg: dict) -> bool:
        """Try to resolve a pending request future. Returns True if matched."""
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in self._pending:
            future = self._pending.pop(msg_id)
            if "error" in msg:
                future.set_exception(
                    RuntimeError(f"ACP error: {msg['error'].get('message', msg['error'])}")
                )
            else:
                future.set_result(msg.get("result", {}))
            return True
        return False


# ── ACP Session Runner ───────────────────────────────────────────────────────


async def run_acp_session(
    agent_cmd: list[str],
    prompt: Optional[str],
    workdir: str,
    timeout: int = 300,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    mode: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    persistent_session_key: Optional[str] = None,
    persistent_shell: Optional[str] = None,
    persistent_shell_manager: Optional[PersistentShellManager] = None,
    permission_policy: Optional[PermissionPolicy] = None,
    on_event: Optional[Callable[[ACPEvent], None]] = None,
) -> ACPResult:
    """Run an ACP session: spawn agent, send prompt, collect structured results.

    Args:
        agent_cmd: Command to launch the agent (e.g. ["copilot", "--acp", "--stdio"])
        prompt: The prompt text to send (None = just create/load session, no prompt)
        workdir: Working directory for the agent
        timeout: Maximum seconds to wait for the prompt to complete
        session_id: If set, resume this session via session/load instead of session/new
        model: Set model via session/set_config_option (e.g. "claude-sonnet-4.6")
        effort: Set reasoning effort ("low", "medium", "high")
        mode: Set session mode ("agent", "plan", "autopilot")
        persistent_session_key: Reuse this persistent shell session for ACP terminal calls
        persistent_shell: Shell kind for the persistent session ("pwsh" or "powershell")
        persistent_shell_manager: Optional shared manager override for tests/embedding
        permission_policy: Policy for handling permission requests (default: deny all)
        on_event: Callback fired for each event (for progress streaming)

    Returns:
        ACPResult with session_id, accumulated output, structured events, etc.
    """
    policy = permission_policy or PermissionPolicy()
    result = ACPResult()
    events: list[ACPEvent] = []
    text_parts: list[str] = []
    text_len = 0
    replay_complete = False  # Boundary marker for session/load replay dedupe
    terminals: dict[str, asyncio.subprocess.Process] = {}
    terminal_outputs: dict[str, str] = {}
    persistent_terminals: dict[str, _PersistentTerminalJob] = {}
    terminal_counter_ref = [0]  # Shared mutable counter across all message loop phases

    workdir_resolved = Path(workdir).resolve()
    merged_env = os.environ.copy()
    if env:
        for key, value in env.items():
            if isinstance(key, str) and isinstance(value, str):
                merged_env[key] = value
    persistent_manager = (
        persistent_shell_manager
        if persistent_shell_manager is not None
        else (_shared_persistent_shell_manager() if persistent_session_key else None)
    )
    persistent_shell_kind = (
        normalize_shell_kind(persistent_shell) if persistent_session_key else None
    )
    terminal_timeout = max(timeout, 300)

    def _path_ok(path: str) -> bool:
        """Validate path is under workdir (resolve symlinks/junctions).
        Relative paths are resolved against workdir, not cwd."""
        try:
            p = Path(path)
            if not p.is_absolute():
                p = workdir_resolved / p
            return p.resolve().is_relative_to(workdir_resolved)
        except (ValueError, OSError):
            return False

    def _add_event(evt: ACPEvent) -> None:
        events.append(evt)
        if len(events) > MAX_EVENTS:
            events.pop(0)
        if on_event:
            on_event(evt)

    # ── Launch agent subprocess ──────────────────────────────────────────
    proc = await asyncio.create_subprocess_exec(
        *agent_cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        env=merged_env,
    )

    try:
        assert proc.stdin and proc.stdout and proc.stderr
        transport = NDJSONTransport(proc.stdout, proc.stdin)
        rpc = RPCClient(transport)

        # Drain stderr concurrently to prevent pipe deadlock
        stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            assert proc.stderr
            while True:
                try:
                    chunk = await proc.stderr.read(65536)
                    if not chunk:
                        break
                    stderr_chunks.append(chunk)
                    # Cap at 100KB to prevent memory issues
                    if sum(len(c) for c in stderr_chunks) > 100_000:
                        stderr_chunks.pop(0)
                except Exception:
                    break

        stderr_task = asyncio.create_task(_drain_stderr())

        # ── Initialize ───────────────────────────────────────────────────
        init_task = asyncio.create_task(rpc.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {},
            "clientInfo": {
                "name": "devpilot-acp-client",
                "title": "DevPilot Headless ACP Client",
                "version": "1.0.0",
            },
        }, timeout=30))

        # Process messages while waiting for init response
        init_result = await _message_loop(
            transport, rpc, init_task, events=[], text_parts=[],
            text_len_ref=[0], replay_complete_ref=[True],
            policy=policy, workdir=workdir, workdir_resolved=workdir_resolved,
            path_ok=_path_ok, terminals=terminals,
            terminal_outputs=terminal_outputs,
            merged_env=merged_env,
            session_env=env or {},
            persistent_terminals=persistent_terminals,
            persistent_manager=persistent_manager,
            persistent_session_key=persistent_session_key,
            persistent_shell=persistent_shell_kind,
            terminal_timeout=terminal_timeout,
            terminal_counter_ref=terminal_counter_ref,
            add_event=lambda e: None, timeout=30,
        )
        logger.info("ACP initialized: agent=%s", agent_cmd[0])

        # ── Create or load session ───────────────────────────────────────
        session_params = {"cwd": workdir, "mcpServers": []}

        if session_id:
            # Resume existing session — will replay conversation history
            session_params["sessionId"] = session_id
            load_task = asyncio.create_task(
                rpc.request("session/load", session_params, timeout=60)
            )
            replay_complete_ref = [False]
            await _message_loop(
                transport, rpc, load_task, events=events, text_parts=[],
                text_len_ref=[0], replay_complete_ref=replay_complete_ref,
                policy=policy, workdir=workdir,
                workdir_resolved=workdir_resolved,
                path_ok=_path_ok, terminals=terminals,
                terminal_outputs=terminal_outputs,
                merged_env=merged_env,
                session_env=env or {},
                persistent_terminals=persistent_terminals,
                persistent_manager=persistent_manager,
                persistent_session_key=persistent_session_key,
                persistent_shell=persistent_shell_kind,
                terminal_timeout=terminal_timeout,
                terminal_counter_ref=terminal_counter_ref,
                add_event=lambda e: None,  # Don't track replay events
                timeout=60,
            )
            result.session_id = session_id
            replay_complete = True
            logger.info("Session loaded: %s", session_id)
        else:
            # New session
            new_task = asyncio.create_task(
                rpc.request("session/new", session_params, timeout=30)
            )
            new_result = await _message_loop(
                transport, rpc, new_task, events=[], text_parts=[],
                text_len_ref=[0], replay_complete_ref=[True],
                policy=policy, workdir=workdir,
                workdir_resolved=workdir_resolved,
                path_ok=_path_ok, terminals=terminals,
                terminal_outputs=terminal_outputs,
                merged_env=merged_env,
                session_env=env or {},
                persistent_terminals=persistent_terminals,
                persistent_manager=persistent_manager,
                persistent_session_key=persistent_session_key,
                persistent_shell=persistent_shell_kind,
                terminal_timeout=terminal_timeout,
                terminal_counter_ref=terminal_counter_ref,
                add_event=lambda e: None, timeout=30,
            )
            result.session_id = new_result.get("sessionId", "")
            replay_complete = True
            logger.info("Session created: %s", result.session_id)

        if not result.session_id:
            result.error = "No session ID received"
            result.stop_reason = "error"
            return result

        # ── Set config options (model, effort, mode) ─────────────────────
        for option_id, value in [("model", model), ("reasoning_effort", effort),
                                  ("mode", mode)]:
            if value:
                try:
                    config_task = asyncio.create_task(rpc.request(
                        "session/set_config_option", {
                            "sessionId": result.session_id,
                            "configOptionId": option_id,
                            "value": value,
                        }, timeout=10))
                    await _message_loop(
                        transport, rpc, config_task, events=[], text_parts=[],
                        text_len_ref=[0], replay_complete_ref=[True],
                        policy=policy, workdir=workdir,
                        workdir_resolved=workdir_resolved,
                        path_ok=_path_ok, terminals=terminals,
                        terminal_outputs=terminal_outputs,
                        merged_env=merged_env,
                        session_env=env or {},
                        persistent_terminals=persistent_terminals,
                        persistent_manager=persistent_manager,
                        persistent_session_key=persistent_session_key,
                        persistent_shell=persistent_shell_kind,
                        terminal_timeout=terminal_timeout,
                        terminal_counter_ref=terminal_counter_ref,
                        add_event=lambda e: None, timeout=10,
                    )
                    logger.info("Set %s=%s", option_id, value)
                except Exception as e:
                    logger.warning("Failed to set %s=%s: %s", option_id, value, e)

        # ── Send prompt ──────────────────────────────────────────────────
        if prompt is None:
            result.stop_reason = "no_prompt"
            result.events = events
            return result

        prompt_task = asyncio.create_task(rpc.request("session/prompt", {
            "sessionId": result.session_id,
            "prompt": [{"type": "text", "text": prompt}],
        }, timeout=timeout + 30))

        prompt_result = await _message_loop(
            transport, rpc, prompt_task, events=events, text_parts=text_parts,
            text_len_ref=[text_len], replay_complete_ref=[replay_complete],
            policy=policy, workdir=workdir, workdir_resolved=workdir_resolved,
            path_ok=_path_ok, terminals=terminals,
            terminal_outputs=terminal_outputs,
            merged_env=merged_env,
            session_env=env or {},
            persistent_terminals=persistent_terminals,
            persistent_manager=persistent_manager,
            persistent_session_key=persistent_session_key,
            persistent_shell=persistent_shell_kind,
            terminal_timeout=terminal_timeout,
            terminal_counter_ref=terminal_counter_ref,
            add_event=_add_event, timeout=timeout,
        )

        result.stop_reason = prompt_result.get("stopReason", "unknown")
        result.output_text = "".join(text_parts)
        result.events = events
        result.tool_calls = [
            e.data for e in events
            if e.type in ("tool_call", "tool_call_update")
        ]

    except asyncio.TimeoutError:
        result.stop_reason = "timeout"
        result.error = f"ACP session timed out after {timeout}s"
        result.output_text = "".join(text_parts)
        result.events = events
        logger.warning("ACP session timed out after %ds", timeout)
        # Try graceful cancel
        try:
            if result.session_id:
                cancel_msg = {"jsonrpc": "2.0", "method": "session/cancel",
                              "id": 9999, "params": {"sessionId": result.session_id}}
                transport._writer.write(
                    (json.dumps(cancel_msg) + "\n").encode("utf-8"))
                await transport._writer.drain()
                await asyncio.sleep(2)
        except Exception:
            pass

    except Exception as e:
        result.stop_reason = "error"
        result.error = str(e)
        result.output_text = "".join(text_parts)
        result.events = events
        logger.error("ACP session error: %s", e)

    finally:
        # Cancel stderr drain and collect accumulated output
        stderr_task.cancel()
        try:
            await stderr_task
        except (asyncio.CancelledError, Exception):
            pass
        result.stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")[-10000:]

        # Kill all spawned terminals
        for tid, tproc in terminals.items():
            if tproc.returncode is None:
                try:
                    tproc.kill()
                except Exception:
                    pass
        for pjob in persistent_terminals.values():
            if not pjob.done.is_set():
                await asyncio.to_thread(pjob.kill)

        # Kill agent process
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    return result


async def _message_loop(
    transport: NDJSONTransport,
    rpc: RPCClient,
    wait_task: asyncio.Task,
    *,
    events: list[ACPEvent],
    text_parts: list[str],
    text_len_ref: list[int],
    replay_complete_ref: list[bool],
    policy: PermissionPolicy,
    workdir: str,
    workdir_resolved: Path,
    path_ok: Callable[[str], bool],
    terminals: dict[str, asyncio.subprocess.Process],
    terminal_outputs: dict[str, str],
    merged_env: dict[str, str],
    session_env: dict[str, str],
    persistent_terminals: dict[str, _PersistentTerminalJob],
    persistent_manager: Optional[PersistentShellManager],
    persistent_session_key: Optional[str],
    persistent_shell: Optional[str],
    terminal_timeout: int,
    terminal_counter_ref: list[int],
    add_event: Callable[[ACPEvent], None],
    timeout: float,
) -> dict:
    """Read messages from transport, dispatch callbacks, until wait_task completes."""
    deadline = time.time() + timeout

    while not wait_task.done() and time.time() < deadline:
        msg = await transport.recv(timeout=min(2.0, deadline - time.time()))
        if msg is None:
            if wait_task.done():
                break
            continue

        # Check if this resolves a pending RPC response
        if rpc.resolve(msg):
            if wait_task.done():
                break
            continue

        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        # ── Notifications (no id) ────────────────────────────────────────
        if method == "session/update" and msg_id is None:
            update = params.get("update", {})
            utype = update.get("sessionUpdate", "")

            if utype == "agent_message_chunk" and replay_complete_ref[0]:
                content = update.get("content", {})
                if content.get("type") == "text":
                    text = content["text"]
                    if text_len_ref[0] < MAX_TEXT_CHARS:
                        text_parts.append(text)
                        text_len_ref[0] += len(text)
                add_event(ACPEvent(type="message", data={"content": content}))

            elif utype == "tool_call":
                add_event(ACPEvent(type="tool_call", data=update))

            elif utype == "tool_call_update":
                add_event(ACPEvent(type="tool_call_update", data=update))

            elif utype == "agent_thought_chunk" and replay_complete_ref[0]:
                add_event(ACPEvent(type="thought", data=update))

            continue

        # ── Requests from agent (have id) ────────────────────────────────
        if not msg_id:
            continue

        if method == "fs/read_text_file":
            path = params.get("path", "")
            add_event(ACPEvent(type="file_read", data={"path": path}))
            if not _path_ok_for_read(path, workdir_resolved):
                await rpc.respond_error(msg_id, -1,
                                        f"Path outside workdir: {path}")
                continue
            try:
                # Resolve relative paths against workdir (not process cwd)
                p = Path(path)
                if not p.is_absolute():
                    p = workdir_resolved / p
                content = p.read_text(encoding="utf-8", errors="replace")
                await rpc.respond(msg_id, {"content": content})
            except Exception as e:
                await rpc.respond_error(msg_id, -1, str(e))

        elif method == "fs/write_text_file":
            path = params.get("path", "")
            content = params.get("content", "")
            add_event(ACPEvent(type="file_write",
                               data={"path": path, "size": len(content)}))
            if not path_ok(path):
                await rpc.respond_error(msg_id, -1,
                                        f"Path outside workdir: {path}")
                continue
            try:
                # Resolve relative paths against workdir (not process cwd)
                p = Path(path)
                if not p.is_absolute():
                    p = workdir_resolved / p
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                await rpc.respond(msg_id, {})
            except Exception as e:
                await rpc.respond_error(msg_id, -1, str(e))

        elif method == "session/request_permission":
            options = params.get("options", [])
            tool_call = params.get("toolCall", {})
            tool_name = tool_call.get("name", "unknown")
            add_event(ACPEvent(type="permission",
                               data={"tool": tool_name, "options": len(options)}))
            decision = policy.decide(tool_name, options, params)
            if decision:
                await rpc.respond(msg_id, {"option": decision})
            else:
                await rpc.respond(msg_id,
                                  {"outcome": {"outcome": "cancelled"}})

        elif method == "terminal/create":
            command = params.get("command", "")
            args = params.get("args", [])
            cwd = params.get("cwd", workdir)
            add_event(ACPEvent(type="terminal_run",
                               data={"command": command, "args": args, "cwd": cwd}))
            terminal_counter_ref[0] += 1
            tid = f"term-{terminal_counter_ref[0]}"
            try:
                # Validate cwd
                if cwd and not path_ok(cwd):
                    cwd = workdir
                if persistent_session_key and persistent_shell and persistent_manager:
                    pjob = _PersistentTerminalJob(
                        manager=persistent_manager,
                        session_key=persistent_session_key,
                        shell=persistent_shell,
                        command=command,
                        args=[str(arg) for arg in args],
                        cwd=cwd,
                        env=session_env,
                        timeout=terminal_timeout,
                    )
                    persistent_terminals[tid] = pjob
                    terminal_outputs[tid] = ""
                    pjob.start()
                else:
                    tproc = await asyncio.create_subprocess_exec(
                        command, *args,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=cwd,
                        env=merged_env,
                    )
                    terminals[tid] = tproc
                    terminal_outputs[tid] = ""
                await rpc.respond(msg_id, {"terminalId": tid})
            except Exception as e:
                await rpc.respond_error(msg_id, -1, str(e))

        elif method == "terminal/output":
            tid = params.get("terminalId", "")
            pjob = persistent_terminals.get(tid)
            if pjob is not None:
                output, is_complete = pjob.read_output()
                terminal_outputs[tid] = terminal_outputs.get(tid, "") + output
                await rpc.respond(msg_id, {"output": output, "isComplete": is_complete})
                continue
            tproc = terminals.get(tid)
            if tproc and tproc.stdout:
                try:
                    chunk = await asyncio.wait_for(
                        tproc.stdout.read(65536), timeout=5
                    )
                    output = chunk.decode("utf-8", errors="replace")
                    terminal_outputs[tid] = terminal_outputs.get(tid, "") + output
                    is_complete = tproc.returncode is not None
                    await rpc.respond(msg_id,
                                      {"output": output, "isComplete": is_complete})
                except asyncio.TimeoutError:
                    await rpc.respond(msg_id, {"output": "", "isComplete": False})
            else:
                await rpc.respond(msg_id, {"output": "", "isComplete": True})

        elif method == "terminal/wait_for_exit":
            tid = params.get("terminalId", "")
            pjob = persistent_terminals.get(tid)
            if pjob is not None:
                exit_code = await asyncio.to_thread(pjob.wait_for_exit, terminal_timeout)
                await rpc.respond(msg_id, {"exitCode": exit_code})
                continue
            tproc = terminals.get(tid)
            if tproc:
                try:
                    await asyncio.wait_for(tproc.wait(), timeout=300)
                    await rpc.respond(msg_id,
                                      {"exitCode": tproc.returncode or 0})
                except asyncio.TimeoutError:
                    tproc.kill()
                    await rpc.respond(msg_id, {"exitCode": -1})
            else:
                await rpc.respond(msg_id, {"exitCode": 0})

        elif method == "terminal/release":
            tid = params.get("terminalId", "")
            pjob = persistent_terminals.get(tid)
            if pjob is not None and pjob.done.is_set():
                persistent_terminals.pop(tid, None)
            tproc = terminals.pop(tid, None)
            terminal_outputs.pop(tid, None)
            if tproc and tproc.returncode is None:
                tproc.kill()
            await rpc.respond(msg_id, {})

        elif method == "terminal/kill":
            tid = params.get("terminalId", "")
            pjob = persistent_terminals.get(tid)
            if pjob is not None:
                await asyncio.to_thread(pjob.kill)
                await rpc.respond(msg_id, {})
                continue
            tproc = terminals.get(tid)
            if tproc and tproc.returncode is None:
                tproc.kill()
            await rpc.respond(msg_id, {})

        else:
            await rpc.respond_error(msg_id, -32601,
                                    f"Method not found: {method}")

    if wait_task.done():
        return wait_task.result()
    raise asyncio.TimeoutError(f"Message loop timed out after {timeout}s")


def _path_ok_for_read(path: str, workdir: Path) -> bool:
    """For reads, allow workdir + resolve relative paths against workdir."""
    try:
        p = Path(path)
        if not p.is_absolute():
            p = workdir / p
        return p.resolve().is_relative_to(workdir)
    except (ValueError, OSError):
        return False


# ── Sync wrapper for use from threading ──────────────────────────────────────


def run_acp_session_sync(
    agent_cmd: list[str],
    prompt: Optional[str],
    workdir: str,
    timeout: int = 300,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    mode: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    persistent_session_key: Optional[str] = None,
    persistent_shell: Optional[str] = None,
    persistent_shell_manager: Optional[PersistentShellManager] = None,
    permission_policy: Optional[PermissionPolicy] = None,
    on_event: Optional[Callable[[ACPEvent], None]] = None,
) -> ACPResult:
    """Synchronous wrapper — runs the async ACP session in a fresh event loop.

    Intended for use from a job thread in the command server.
    Each call creates and destroys its own event loop.
    """
    return asyncio.run(run_acp_session(
        agent_cmd=agent_cmd,
        prompt=prompt,
        workdir=workdir,
        timeout=timeout,
        session_id=session_id,
        model=model,
        effort=effort,
        mode=mode,
        env=env,
        persistent_session_key=persistent_session_key,
        persistent_shell=persistent_shell,
        persistent_shell_manager=persistent_shell_manager,
        permission_policy=permission_policy,
        on_event=on_event,
    ))

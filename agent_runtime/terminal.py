"""WebSocket terminal server — interactive PTY sessions for browser terminals.

Optional feature: requires ``pip install devpilot-agent[terminal]``
which adds pywinpty + websockets.

Protocol (JSON text frames from client, raw text frames from server):
  Client → Server (text, JSON):
    {"type": "input", "data": "<keystrokes>"}
    {"type": "resize", "cols": 80, "rows": 24}
    {"type": "ping"}
  Server → Client (text, raw PTY output — written directly to xterm.js):
    <raw terminal output>
  Server → Client (text, JSON — only for control messages prefixed with NUL):
    \\x00{"type": "pong"}
    \\x00{"type": "error", "message": "..."}
"""

import asyncio
import json
import logging
import queue
import secrets
import threading
import time

log = logging.getLogger("devpilot.terminal")

# Lazy-loaded optional deps
_PtyProcess = None
_websockets = None

READ_BUFFER = 65536       # 64 KB — critical for TUI apps like Copilot CLI
MAX_SESSIONS = 2          # Max concurrent terminal sessions
IDLE_TIMEOUT = 900        # 15 min no input → terminate
MAX_LIFETIME = 3600       # 60 min hard cap
MAX_MESSAGE_SIZE = 65536  # Max inbound WebSocket message size
SHELL_COMMAND = "powershell.exe -NoLogo -NoProfile"

# Active session tracking
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


def is_available() -> bool:
    """Check if terminal extras (pywinpty, websockets) are installed."""
    try:
        import winpty  # noqa: F401
        import websockets  # noqa: F401
        return True
    except ImportError:
        return False


def _load_deps() -> None:
    """Lazy-load optional dependencies."""
    global _PtyProcess, _websockets
    if _PtyProcess is None:
        from winpty import PtyProcess
        _PtyProcess = PtyProcess
    if _websockets is None:
        import websockets
        _websockets = websockets


def _check_api_key(ws, expected_key: str) -> bool:
    """Validate API key from WebSocket handshake headers."""
    if not expected_key:
        return False  # Reject all if no key configured (safety: never allow empty)
    key = ""
    try:
        for name, value in ws.request.headers.raw_items():
            if name.lower() == "x-api-key":
                key = value
                break
    except AttributeError:
        return False
    return bool(key) and secrets.compare_digest(key, expected_key)


def _send_control(ws_send: asyncio.coroutines, msg: dict) -> asyncio.Task:
    """Send a NUL-prefixed JSON control message (distinguishable from PTY output)."""
    return asyncio.ensure_future(ws_send("\x00" + json.dumps(msg)))


async def terminal_handler(ws, api_key: str) -> None:
    """Handle one WebSocket connection → one PTY session."""
    _load_deps()

    session_id = secrets.token_hex(6)
    start_time = time.time()
    last_activity = time.time()

    # Reserve a session slot under the lock to prevent TOCTOU race
    with _sessions_lock:
        if len(_sessions) >= MAX_SESSIONS:
            await ws.send("\x00" + json.dumps({
                "type": "error",
                "message": f"Max terminal sessions ({MAX_SESSIONS}) reached",
            }))
            await ws.close(1013, "Max sessions reached")
            return
        # Reserve the slot immediately (before spawning PTY)
        _sessions[session_id] = {"pid": 0, "started": start_time}

    # Parse workdir and command from query string for session-aware terminals
    workdir = ""
    command = ""
    try:
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(str(ws.request.path)).query)
        workdir = query.get("workdir", [""])[0]
        command = query.get("command", [""])[0]
    except Exception:
        pass

    # Spawn plain PowerShell — set workdir and run command via stdin after startup
    try:
        pty = _PtyProcess.spawn(SHELL_COMMAND)
    except Exception as e:
        # Release reserved slot on spawn failure
        with _sessions_lock:
            _sessions.pop(session_id, None)
        await ws.send("\x00" + json.dumps({"type": "error", "message": f"Failed to spawn shell: {e}"}))
        await ws.close(1011, "Shell spawn failed")
        return

    log.info("Terminal session %s started (pid %d, workdir=%s)",
             session_id, pty.pid, workdir or "(default)")

    # Write startup commands to stdin (avoids all quoting/escaping issues)
    import asyncio as _asyncio
    await _asyncio.sleep(0.5)  # Let PowerShell finish initializing
    if workdir:
        pty.write(f"cd '{workdir}'\r")
        await _asyncio.sleep(0.3)
        pty.write("cls\r")  # Clear the cd command from view
    if command:
        await _asyncio.sleep(0.3)
        pty.write(f"{command}\r")

    with _sessions_lock:
        _sessions[session_id]["pid"] = pty.pid

    loop = asyncio.get_event_loop()
    output_q: queue.Queue[str | None] = queue.Queue()

    def reader_thread() -> None:
        """Read PTY output as fast as possible into a queue."""
        while pty.isalive():
            try:
                data = pty.read(READ_BUFFER)
                if data:
                    output_q.put(data)
            except EOFError:
                break
            except Exception:
                time.sleep(0.01)
        output_q.put(None)

    reader = threading.Thread(target=reader_thread, daemon=True)
    reader.start()

    async def send_output() -> None:
        """Drain queue → batch → send to WebSocket."""
        while True:
            chunk = await loop.run_in_executor(None, output_q.get)
            if chunk is None:
                break
            # Batch all immediately available data
            while not output_q.empty():
                more = output_q.get_nowait()
                if more is None:
                    await ws.send(chunk)
                    return
                chunk += more
            await ws.send(chunk)

    sender_task = asyncio.create_task(send_output())

    async def watchdog() -> None:
        """Enforce idle timeout and max lifetime."""
        nonlocal last_activity
        while True:
            await asyncio.sleep(30)
            elapsed = time.time() - start_time
            idle = time.time() - last_activity
            if elapsed > MAX_LIFETIME:
                log.info("Terminal %s: max lifetime reached", session_id)
                await ws.close(1000, "Session max lifetime reached")
                return
            if idle > IDLE_TIMEOUT:
                log.info("Terminal %s: idle timeout", session_id)
                await ws.close(1000, "Idle timeout")
                return

    watchdog_task = asyncio.create_task(watchdog())

    try:
        async for raw_msg in ws:
            if len(raw_msg) > MAX_MESSAGE_SIZE:
                continue
            try:
                msg = json.loads(raw_msg)
            except (json.JSONDecodeError, TypeError):
                continue

            msg_type = msg.get("type")
            if msg_type == "input":
                data = msg.get("data", "")
                if data:
                    pty.write(data)
                    last_activity = time.time()
            elif msg_type == "resize":
                cols = min(max(msg.get("cols", 80), 10), 500)
                rows = min(max(msg.get("rows", 24), 2), 200)
                pty.setwinsize(rows, cols)
            elif msg_type == "ping":
                await ws.send("\x00" + json.dumps({"type": "pong"}))
    except Exception:
        pass
    finally:
        sender_task.cancel()
        watchdog_task.cancel()
        if pty.isalive():
            pty.terminate()
        with _sessions_lock:
            _sessions.pop(session_id, None)
        log.info("Terminal session %s closed (%.0fs)",
                 session_id, time.time() - start_time)


def active_session_count() -> int:
    """Return the number of active terminal sessions."""
    with _sessions_lock:
        return len(_sessions)


async def start_terminal_server(port: int = 8586, api_key: str = "") -> None:
    """Start the WebSocket terminal server.

    Args:
        port: Port to listen on.
        api_key: Expected X-API-Key for authentication. Must not be empty.
    """
    _load_deps()

    if not api_key:
        log.error("Terminal server refusing to start without an API key")
        return

    async def _handler(ws) -> None:
        if not _check_api_key(ws, api_key):
            await ws.close(3401, "Invalid API key")
            return
        await terminal_handler(ws, api_key)

    log.info("Terminal server starting on port %d", port)
    print(f"[terminal] Listening on 0.0.0.0:{port}")
    async with _websockets.serve(
        _handler,
        "0.0.0.0",
        port,
        max_size=MAX_MESSAGE_SIZE,
        ping_interval=30,
        ping_timeout=10,
    ):
        await asyncio.Future()  # Run forever

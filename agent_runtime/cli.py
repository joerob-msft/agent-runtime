"""CLI entry point for devpilot-server.

Usage:
    devpilot-server                                          # Start on port 8585
    devpilot-server --port 9090                              # Custom port
    devpilot-server --register https://azure-devpilot.azurewebsites.net # Auto-register with dashboard
    devpilot-server --wrapper                                # Run with auto-update wrapper loop
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from agent_runtime import __version__, PIP_INSTALL_URL
from agent_runtime.server import RESTART_EXIT_CODE, start_server


def _resolve_wheel_url(dashboard_url: str) -> str:
    """Resolve the versioned wheel URL from the dashboard.

    The /agent/agent_runtime.whl endpoint redirects to the real filename
    (e.g. agent_runtime-0.5.2-py3-none-any.whl). pip needs the real
    filename in the URL to recognise it as a wheel.
    """
    base = f"{dashboard_url.rstrip('/')}/agent/agent_runtime.whl"
    try:
        req = urllib.request.Request(base, method="HEAD")
        resp = urllib.request.urlopen(req, timeout=10)
        resolved = resp.url
        return f"devpilot-agent[terminal] @ {resolved}"
    except Exception as e:
        print(f"[wrapper] Could not resolve wheel URL: {e}")
        return PIP_INSTALL_URL


def _derive_terminal_url(tunnel_url: str, main_port: int, terminal_port: int) -> str:
    """Derive the terminal tunnel URL from the main tunnel URL.

    Devtunnel URLs include the port: https://{id}-{port}.{region}.devtunnels.ms
    For localhost, just swap the port.
    """
    import re
    if "localhost" in tunnel_url or "127.0.0.1" in tunnel_url:
        return re.sub(r":\d+", f":{terminal_port}", tunnel_url)
    # Devtunnel URL: replace -{main_port}. with -{terminal_port}.
    pattern = f"-{main_port}."
    replacement = f"-{terminal_port}."
    if pattern in tunnel_url:
        return tunnel_url.replace(pattern, replacement, 1)
    # Fallback: append port+1 info (will need manual config)
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="devpilot-server",
        description="DevPilot Agent — command server for Dev Boxes",
    )
    parser.add_argument(
        "--port", type=int, default=8585, help="Port to listen on (default: 8585)"
    )
    parser.add_argument(
        "--register",
        metavar="URL",
        default="",
        help="Dashboard URL to auto-register with on startup",
    )
    parser.add_argument(
        "--name",
        default=os.environ.get("COMPUTERNAME", "devbox"),
        help="Dev Box name for registration (default: hostname)",
    )
    parser.add_argument(
        "--wrapper",
        action="store_true",
        help="Run with auto-update wrapper (restarts on update, runs pip upgrade)",
    )
    parser.add_argument(
        "--tunnel",
        action="store_true",
        help="Start a devtunnel automatically (Dev Box only; not needed on ADC)",
    )
    parser.add_argument(
        "--tunnel-id",
        metavar="ID",
        default="",
        help="Reuse a persistent devtunnel ID (creates one if not specified)",
    )
    parser.add_argument(
        "--version", action="version", version=f"devpilot-server {__version__}"
    )

    args = parser.parse_args()

    if args.wrapper:
        _run_wrapper(args.port, args.register, args.name, args.tunnel, args.tunnel_id)
    else:
        _run_server(args.port, args.register, args.name, args.tunnel, args.tunnel_id)


def _run_server(port: int, register_url: str, name: str,
                tunnel: bool = False, tunnel_id: str = "") -> None:
    """Start the server, optionally with a devtunnel and dashboard registration."""
    import shutil
    tunnel_proc = None
    tunnel_url = ""

    if tunnel:
        tunnel_proc, tunnel_url = _start_tunnel(port, tunnel_id)
        if tunnel_url:
            os.environ["DEVPILOT_TUNNEL_URL"] = tunnel_url
            print(f"[tunnel] URL: {tunnel_url}")
            # Derive terminal URL (port+1) from the main tunnel URL
            terminal_url = _derive_terminal_url(tunnel_url, port, port + 1)
            if terminal_url:
                os.environ["DEVPILOT_TERMINAL_URL"] = terminal_url
                print(f"[tunnel] Terminal URL: {terminal_url}")

    if register_url:
        _register(register_url, name, port)

    # Background thread to refresh the tunnel token every 20 hours
    # and push it to the dashboard so connectivity isn't lost.
    if tunnel and register_url:
        import threading

        def _token_refresh_loop() -> None:
            effective_tunnel_id = tunnel_id or f"devpilot-{port}"
            devtunnel = shutil.which("devtunnel") or ""
            if not devtunnel:
                return
            while True:
                time.sleep(20 * 3600)  # 20 hours (tokens last 24)
                new_token = _generate_tunnel_token(devtunnel, effective_tunnel_id)
                if new_token:
                    os.environ["DEVPILOT_TUNNEL_TOKEN"] = new_token
                    print(f"[tunnel] Token refreshed (length: {len(new_token)})")
                    _push_token_update(register_url, new_token)
                else:
                    print("[tunnel] WARNING: Token refresh failed")

        threading.Thread(target=_token_refresh_loop, daemon=True).start()

    try:
        start_server(port=port)
    finally:
        if tunnel_proc:
            tunnel_proc.terminate()
            tunnel_proc.wait(timeout=5)


def _run_wrapper(port: int, register_url: str, name: str,
                 tunnel: bool = False, tunnel_id: str = "") -> None:
    """Wrapper loop: start server, on exit code 42 → pip upgrade → restart."""
    print(f"DevPilot Agent Wrapper v{__version__}")
    print(f"  Port: {port}")
    print(f"  Tunnel: {'yes' if tunnel else 'no'}")
    print(f"  Auto-update: enabled (exit code {RESTART_EXIT_CODE} triggers upgrade)")
    print()

    while True:
        print("[wrapper] Starting devpilot-server...")

        # Build the command to run the server without --wrapper to avoid recursion
        cmd = [sys.executable, "-m", "agent_runtime.cli", "--port", str(port)]
        if register_url:
            cmd.extend(["--register", register_url, "--name", name])
        if tunnel:
            cmd.append("--tunnel")
        if tunnel_id:
            cmd.extend(["--tunnel-id", tunnel_id])

        try:
            result = subprocess.run(cmd)
        except KeyboardInterrupt:
            print("\n[wrapper] Shutting down.")
            sys.exit(0)

        if result.returncode == RESTART_EXIT_CODE:
            print()
            print("[wrapper] Update requested. Running pip upgrade...")
            # Prefer dashboard-served wheel; fall back to GitHub
            if register_url:
                install_url = _resolve_wheel_url(register_url)
            else:
                install_url = PIP_INSTALL_URL
            upgrade = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade",
                 install_url, "--quiet"],
            )
            if upgrade.returncode == 0:
                print("[wrapper] Upgrade complete. Restarting in 2 seconds...")
            else:
                print("[wrapper] Upgrade failed. Restarting with current version...")
            time.sleep(2)
        else:
            print(f"[wrapper] Server exited with code {result.returncode}. Stopping.")
            sys.exit(result.returncode)


def _push_token_update(dashboard_url: str, tunnel_token: str) -> None:
    """Push a refreshed tunnel token and current tunnel URL to the dashboard."""
    tunnel_url = os.environ.get("DEVPILOT_TUNNEL_URL", "")
    name = os.environ.get("COMPUTERNAME", "devbox")
    api_key = os.environ.get("agent_runtime_API_KEY", "")
    if not tunnel_url:
        return
    url = f"{dashboard_url.rstrip('/')}/api/register/refresh-token"
    payload = json.dumps({
        "name": name,
        "tunnel_url": tunnel_url,
        "tunnel_token": tunnel_token,
        "api_key": api_key,
        "current_api_key": api_key,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[tunnel] Token pushed to dashboard (HTTP {resp.status})")
    except Exception as e:
        print(f"[tunnel] WARNING: Failed to push token update: {e}")


def _generate_tunnel_token(devtunnel: str, tunnel_id: str) -> str:
    """Generate a devtunnel access token. Returns the JWT or empty string."""
    try:
        token_result = subprocess.run(
            [devtunnel, "token", tunnel_id, "--scopes", "connect"],
            capture_output=True, text=True, timeout=15,
        )
        if token_result.returncode == 0:
            for line in token_result.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("Token:"):
                    value = stripped.split(":", 1)[1].strip()
                    if value.startswith("eyJ"):
                        return value
    except Exception as e:
        print(f"[tunnel] WARNING: Failed to generate tunnel token: {e}")
    return ""


def _start_tunnel(port: int, tunnel_id: str = "") -> tuple[subprocess.Popen, str]:
    """Start a devtunnel and parse the tunnel URL from its output.

    Returns (process, tunnel_url). The tunnel URL is empty if parsing fails.
    This is Dev Box–specific; ADC sandboxes don't need tunnels.

    The tunnel allows anonymous connect (the API key on the command server
    is the security layer). A tunnel access token is also generated for
    the X-Tunnel-Authorization header to bypass the anti-phishing page.
    """
    import re
    import shutil

    devtunnel = shutil.which("devtunnel")
    if not devtunnel:
        print("[tunnel] ERROR: 'devtunnel' not found. Install from https://aka.ms/devtunnels/install")
        print("[tunnel] Continuing without tunnel — server will only be reachable locally.")
        return None, ""

    # Create or reuse a persistent tunnel for stable URLs across restarts.
    # Include hostname so multiple dev boxes don't fight over the same tunnel.
    if not tunnel_id:
        hostname = os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", ""))
        if hostname:
            tunnel_id = f"devpilot-{hostname.lower()}-{port}"
        else:
            tunnel_id = f"devpilot-{port}"
        try:
            subprocess.run(
                [devtunnel, "create", tunnel_id, "--expiration", "30d"],
                capture_output=True, timeout=15,
            )
            subprocess.run(
                [devtunnel, "port", "create", tunnel_id, "-p", str(port)],
                capture_output=True, timeout=15,
            )
            # Also forward the terminal WebSocket port (port + 1)
            subprocess.run(
                [devtunnel, "port", "create", tunnel_id, "-p", str(port + 1)],
                capture_output=True, timeout=15,
            )
        except Exception as e:
            print(f"[tunnel] Warning creating tunnel: {e}")

    # Ensure anonymous access is enabled (idempotent) — the API key on the
    # command server is the real auth layer; anonymous just lets the relay forward.
    try:
        subprocess.run(
            [devtunnel, "access", "create", tunnel_id, "--anonymous"],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass

    # Ensure terminal port is forwarded (idempotent — also covers reused tunnel IDs)
    try:
        subprocess.run(
            [devtunnel, "port", "create", tunnel_id, "-p", str(port + 1)],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass

    # Generate a tunnel access token for the dashboard to connect.
    # Tokens are valid for 24 hours; a background thread refreshes them.
    tunnel_token = _generate_tunnel_token(devtunnel, tunnel_id)
    if tunnel_token:
        os.environ["DEVPILOT_TUNNEL_TOKEN"] = tunnel_token
        print(f"[tunnel] Access token generated (length: {len(tunnel_token)})")
    else:
        print(f"[tunnel] WARNING: Could not generate tunnel access token.")

    print(f"[tunnel] Starting devtunnel '{tunnel_id}' on port {port}...")

    proc = subprocess.Popen(
        [devtunnel, "host", tunnel_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Parse the tunnel URL from devtunnel output (waits up to 15 seconds)
    tunnel_url = ""
    deadline = time.time() + 15
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        print(f"[tunnel] {line.rstrip()}")
        # Look for "Connect via browser: https://xxx.devtunnels.ms"
        match = re.search(r"(https://\S+\.devtunnels\.ms\S*)", line)
        if match:
            url = match.group(1).rstrip(",")
            # Prefer the clean URL without port suffix
            if "inspect" not in url:
                tunnel_url = url
                break

    if not tunnel_url:
        print("[tunnel] WARNING: Could not parse tunnel URL. Registration may fail.")

    # Continue reading tunnel output in a background thread so it doesn't block
    import threading

    def _drain_output():
        for line in proc.stdout:
            pass  # silently consume

    threading.Thread(target=_drain_output, daemon=True).start()

    return proc, tunnel_url


def _register(dashboard_url: str, name: str, port: int) -> None:
    """Register this Dev Box with the dashboard using a challenge code.

    Generates a short code, submits a pending registration, and polls
    until a dashboard user confirms by entering the code.
    """
    import random
    import string
    import threading

    def _generate_code() -> str:
        letters = "".join(random.choices(string.ascii_uppercase, k=3))
        digits = "".join(random.choices(string.digits, k=4))
        return f"{letters}-{digits}"

    def _do_register() -> None:
        # Wait for the server to be ready
        time.sleep(3)

        tunnel_url = os.environ.get("DEVPILOT_TUNNEL_URL", f"http://localhost:{port}")
        terminal_url = os.environ.get("DEVPILOT_TERMINAL_URL", f"http://localhost:{port + 1}")
        api_key = os.environ.get("agent_runtime_API_KEY", "")
        tunnel_token = os.environ.get("DEVPILOT_TUNNEL_TOKEN", "")

        # Try to update an existing devbox first (re-registration after restart)
        refresh_url = f"{dashboard_url.rstrip('/')}/api/register/refresh-token"
        refresh_payload = json.dumps({
            "name": name,
            "tunnel_url": tunnel_url,
            "terminal_url": terminal_url,
            "tunnel_token": tunnel_token,
            "api_key": api_key,
            "current_api_key": api_key,
        }).encode("utf-8")
        refresh_req = urllib.request.Request(
            refresh_url, data=refresh_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(refresh_req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("updated"):
                    print(f"[register] ✅ Re-registered with dashboard (devbox: {data['updated']})")
                    return
        except Exception:
            pass  # Not found — need full registration with challenge code

        challenge_code = _generate_code()

        # Submit pending registration
        url = f"{dashboard_url.rstrip('/')}/api/register"
        payload_data = {
            "name": name,
            "tunnel_url": tunnel_url,
            "terminal_url": terminal_url,
            "challenge_code": challenge_code,
        }
        if api_key:
            payload_data["api_key"] = api_key
        if tunnel_token:
            payload_data["tunnel_token"] = tunnel_token
        payload = json.dumps(payload_data).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        reg_id = ""
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    reg_id = data.get("id", "")
                    break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")[:200] if e.fp else ""
                print(f"[register] Registration failed (HTTP {e.code}): {body}")
                return
            except Exception as e:
                print(f"[register] Attempt {attempt + 1}/3 failed: {e}")
                if attempt < 2:
                    time.sleep(5)

        if not reg_id:
            print("[register] Failed to submit registration.")
            return

        # Display the challenge code prominently
        print()
        print("=" * 52)
        print(f"  REGISTRATION CODE:  {challenge_code}")
        print()
        print(f"  Enter this code on the dashboard to confirm:")
        print(f"  {dashboard_url.rstrip('/')}/devboxes")
        print("=" * 52)
        print()

        # Poll for confirmation (up to 10 minutes)
        status_url = f"{dashboard_url.rstrip('/')}/api/register/{reg_id}/status"
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(5)
            try:
                status_req = urllib.request.Request(status_url)
                with urllib.request.urlopen(status_req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("status") == "confirmed":
                        devbox = data.get("devbox", {})
                        print(f"[register] ✅ Confirmed! Registered as: {devbox.get('name', name)} (id: {devbox.get('id', reg_id)})")
                        return
            except Exception:
                pass  # Keep polling

        print("[register] ⏰ Registration timed out (10 minutes). Run bootstrap again to retry.")

    thread = threading.Thread(target=_do_register, daemon=True)
    thread.start()


if __name__ == "__main__":
    main()

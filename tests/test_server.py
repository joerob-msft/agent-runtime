"""Tests for command server: exec, nudge, activity tracking, auto-update."""

import json
import os
import sys
import threading
import time
from http.server import HTTPServer
from unittest.mock import patch

from agent_runtime.server import (
    CommandHandler,
    RESTART_EXIT_CODE,
    _VERSION,
    _jobs,
    _jobs_lock,
    _run_acp_command,
)
from agent_runtime.acp_client import ACPResult
import agent_runtime.server as _server_module


def _start_test_server(api_key: str = "") -> tuple[HTTPServer, int, threading.Thread]:
    """Start a command server on a random port and return (server, port, thread).

    Sets a test API key so auth works. Pass api_key="" to use auto-generated key.
    """
    if not api_key:
        import secrets as _secrets
        api_key = _secrets.token_urlsafe(16)
    _server_module._api_key = api_key
    server = HTTPServer(("127.0.0.1", 0), CommandHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, thread


def _get(port: int, path: str, api_key: str = "") -> dict:
    import urllib.request
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET",
                                headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_status(port: int, path: str, api_key: str = "") -> tuple[int, dict]:
    """Like _get but returns (status_code, body) and doesn't raise on 4xx."""
    import urllib.request
    import urllib.error
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET",
                                headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        return e.code, json.loads(body_text)


def _post(port: int, path: str, body: dict | None = None,
          api_key: str = "") -> tuple[int, dict]:
    import urllib.request
    import urllib.error
    data = json.dumps(body or {}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        return e.code, json.loads(body_text)


class TestRestartExitCode:
    def test_restart_exit_code_is_42(self):
        assert RESTART_EXIT_CODE == 42


class TestHealthVersion:
    """Test /health endpoint returns expected fields (with valid auth)."""

    def test_health_includes_version(self):
        server, port, _ = _start_test_server()
        try:
            api_key = _server_module._api_key
            data = _get(port, "/health", api_key=api_key)
            assert data["status"] == "ok"
            assert "version" in data
            assert data["version"] == _VERSION
        finally:
            server.shutdown()

    def test_health_includes_all_fields(self):
        server, port, _ = _start_test_server()
        try:
            api_key = _server_module._api_key
            data = _get(port, "/health", api_key=api_key)
            assert "hostname" in data
            assert "jobs" in data
            assert "cwd" in data
            assert "version" in data
        finally:
            server.shutdown()


class TestUpdateEndpoint:
    def _clear_jobs(self):
        with _jobs_lock:
            _jobs.clear()

    def test_update_rejects_when_jobs_running(self):
        server, port, _ = _start_test_server()
        try:
            api_key = _server_module._api_key
            with _jobs_lock:
                _jobs["fake-running"] = {
                    "jobId": "fake-running",
                    "status": "running",
                    "command": "sleep 999",
                    "submittedAt": time.time(),
                }

            status, data = _post(port, "/update", api_key=api_key)
            assert status == 409
            assert "running_jobs" in data
            assert data["running_jobs"] == 1
        finally:
            self._clear_jobs()
            server.shutdown()

    def test_update_rejects_when_jobs_pending(self):
        server, port, _ = _start_test_server()
        try:
            api_key = _server_module._api_key
            with _jobs_lock:
                _jobs["fake-pending"] = {
                    "jobId": "fake-pending",
                    "status": "pending",
                    "command": "echo hello",
                    "submittedAt": time.time(),
                }

            status, data = _post(port, "/update", api_key=api_key)
            assert status == 409
            assert data["running_jobs"] == 1
        finally:
            self._clear_jobs()
            server.shutdown()

    def test_update_succeeds_when_no_active_jobs(self):
        server, port, _ = _start_test_server()
        try:
            api_key = _server_module._api_key
            self._clear_jobs()
            with _jobs_lock:
                _jobs["fake-done"] = {
                    "jobId": "fake-done",
                    "status": "completed",
                    "command": "echo done",
                    "submittedAt": time.time(),
                }

            with patch.object(_server_module, "_exit_for_restart") as mock_exit:
                status, data = _post(port, "/update", api_key=api_key)
                assert status == 200
                assert data["status"] == "updating"
                assert "version" in data

                time.sleep(2)
                mock_exit.assert_called_once()
        finally:
            self._clear_jobs()
            server.shutdown()

    def test_update_succeeds_when_job_store_empty(self):
        server, port, _ = _start_test_server()
        try:
            api_key = _server_module._api_key
            self._clear_jobs()

            with patch.object(_server_module, "_exit_for_restart") as mock_exit:
                status, data = _post(port, "/update", api_key=api_key)
                assert status == 200
                assert data["status"] == "updating"

                time.sleep(2)
                mock_exit.assert_called_once()
        finally:
            self._clear_jobs()
            server.shutdown()


class TestGetEndpointAuth:
    """Test that GET endpoints enforce API key authentication."""

    TEST_KEY = "test-secret-key-12345"

    def _with_key(self, fn):
        """Run fn with _api_key set, then restore."""
        old = _server_module._api_key
        _server_module._api_key = self.TEST_KEY
        try:
            return fn()
        finally:
            _server_module._api_key = old

    def test_health_no_key_returns_minimal(self):
        server, port, _ = _start_test_server()
        try:
            def check():
                data = _get(port, "/health")
                assert data["status"] == "ok"
                assert data["authenticated"] is False
                assert "hostname" not in data
                assert "jobs" not in data
                assert "cwd" not in data
            self._with_key(check)
        finally:
            server.shutdown()

    def test_health_wrong_key_returns_minimal(self):
        server, port, _ = _start_test_server()
        try:
            def check():
                data = _get(port, "/health", api_key="wrong-key")
                assert data["status"] == "ok"
                assert data["authenticated"] is False
                assert "hostname" not in data
            self._with_key(check)
        finally:
            server.shutdown()

    def test_health_valid_key_returns_full(self):
        server, port, _ = _start_test_server()
        try:
            def check():
                data = _get(port, "/health", api_key=self.TEST_KEY)
                assert data["status"] == "ok"
                assert data["authenticated"] is True
                assert "hostname" in data
                assert "jobs" in data
                assert "cwd" in data
                assert "version" in data
            self._with_key(check)
        finally:
            server.shutdown()

    def test_jobs_no_key_returns_401(self):
        server, port, _ = _start_test_server()
        try:
            def check():
                status, data = _get_status(port, "/jobs")
                assert status == 401
                assert "error" in data
            self._with_key(check)
        finally:
            server.shutdown()

    def test_jobs_valid_key_returns_200(self):
        server, port, _ = _start_test_server()
        try:
            def check():
                status, data = _get_status(port, "/jobs", api_key=self.TEST_KEY)
                assert status == 200
            self._with_key(check)
        finally:
            server.shutdown()

    def test_job_by_id_no_key_returns_401(self):
        server, port, _ = _start_test_server()
        try:
            def check():
                status, data = _get_status(port, "/jobs/nonexistent")
                assert status == 401
                assert "error" in data
            self._with_key(check)
        finally:
            server.shutdown()

    def test_job_by_id_valid_key_returns_404_for_missing(self):
        server, port, _ = _start_test_server()
        try:
            def check():
                status, data = _get_status(port, "/jobs/nonexistent",
                                           api_key=self.TEST_KEY)
                assert status == 404
            self._with_key(check)
        finally:
            server.shutdown()


class TestExecAndPoll:
    """Test command execution with Popen-based runner and activity tracking."""

    def _clear_jobs(self):
        with _jobs_lock:
            _jobs.clear()

    def test_exec_echo_completes(self):
        server, port, _ = _start_test_server()
        try:
            api_key = _server_module._api_key
            self._clear_jobs()

            status, data = _post(port, "/exec",
                                 {"command": "echo hello-world", "timeout": 10},
                                 api_key=api_key)
            assert status == 202
            job_id = data["jobId"]
            assert job_id

            # Poll until completed
            for _ in range(20):
                time.sleep(0.5)
                result = _get(port, f"/jobs/{job_id}", api_key=api_key)
                if result["status"] in ("completed", "failed", "timeout"):
                    break

            assert result["status"] == "completed"
            assert "hello-world" in result["stdout"]
            assert result["exitCode"] == 0
            assert result["timedOut"] is False
            assert "durationMs" in result
        finally:
            self._clear_jobs()
            server.shutdown()

    def test_poll_includes_activity_fields(self):
        server, port, _ = _start_test_server()
        try:
            api_key = _server_module._api_key
            self._clear_jobs()

            status, data = _post(port, "/exec",
                                 {"command": "echo activity-test", "timeout": 10},
                                 api_key=api_key)
            job_id = data["jobId"]

            # Poll and check for lastOutputAt and processAlive
            for _ in range(20):
                time.sleep(0.5)
                result = _get(port, f"/jobs/{job_id}", api_key=api_key)
                if result["status"] in ("completed", "failed"):
                    break

            assert "lastOutputAt" in result
            assert isinstance(result["lastOutputAt"], float)
            assert "processAlive" in result
            # Process should be dead after completion
            assert result["processAlive"] is False
            # _process should not leak into response
            assert "_process" not in result
        finally:
            self._clear_jobs()
            server.shutdown()

    def test_exec_failed_command(self):
        server, port, _ = _start_test_server()
        try:
            api_key = _server_module._api_key
            self._clear_jobs()

            status, data = _post(port, "/exec",
                                 {"command": "cmd /c exit 1", "timeout": 10},
                                 api_key=api_key)
            job_id = data["jobId"]

            for _ in range(20):
                time.sleep(0.5)
                result = _get(port, f"/jobs/{job_id}", api_key=api_key)
                if result["status"] in ("completed", "failed"):
                    break

            assert result["status"] == "failed"
            assert result["exitCode"] == 1
        finally:
            self._clear_jobs()
            server.shutdown()

    def test_exec_env_visible_to_shell_process(self):
        server, port, _ = _start_test_server()
        try:
            api_key = _server_module._api_key
            self._clear_jobs()

            command = (
                f'"{sys.executable}" -c '
                "\"import os; print(os.environ.get('DEVPILOT_TEST_ENV', ''))\""
            )
            status, data = _post(
                port,
                "/exec",
                {
                    "command": command,
                    "timeout": 10,
                    "env": {"DEVPILOT_TEST_ENV": "env-visible"},
                },
                api_key=api_key,
            )
            assert status == 202
            job_id = data["jobId"]

            for _ in range(20):
                time.sleep(0.5)
                result = _get(port, f"/jobs/{job_id}", api_key=api_key)
                if result["status"] in ("completed", "failed", "timeout"):
                    break

            assert result["status"] == "completed"
            assert "env-visible" in result["stdout"]
        finally:
            self._clear_jobs()
            server.shutdown()

    def test_exec_rejects_invalid_env_payload(self):
        server, port, _ = _start_test_server()
        try:
            api_key = _server_module._api_key
            status, data = _post(
                port,
                "/exec",
                {"command": "echo hello", "env": {"BAD": 123}},
                api_key=api_key,
            )
            assert status == 400
            assert data["error"] == "env must be an object of string key/value pairs"
        finally:
            server.shutdown()

    def test_run_acp_command_passes_env_to_client(self):
        self._clear_jobs()
        with _jobs_lock:
            _jobs["acp-job"] = {"jobId": "acp-job", "status": "pending"}

        with patch("agent_runtime.acp_client.run_acp_session_sync") as mock_run:
            mock_run.return_value = ACPResult(
                session_id="session-123",
                stop_reason="no_prompt",
                output_text="",
                stderr="",
                events=[],
            )

            _run_acp_command(
                "acp-job",
                "agent-server --stdio",
                "",
                os.getcwd(),
                10,
                None,
                None,
                None,
                {"DEVPILOT_TEST_ENV": "acp-visible"},
            )

        assert mock_run.call_count == 1
        assert mock_run.call_args.kwargs["env"] == {"DEVPILOT_TEST_ENV": "acp-visible"}
        with _jobs_lock:
            assert _jobs["acp-job"]["status"] == "completed"
        self._clear_jobs()


class TestNudgeEndpoint:
    """Test POST /jobs/{id}/nudge."""

    def _clear_jobs(self):
        with _jobs_lock:
            _jobs.clear()

    def test_nudge_nonexistent_job_returns_404(self):
        server, port, _ = _start_test_server()
        try:
            api_key = _server_module._api_key
            status, data = _post(port, "/jobs/nonexistent/nudge", api_key=api_key)
            assert status == 404
        finally:
            server.shutdown()

    def test_nudge_completed_job_returns_not_running(self):
        server, port, _ = _start_test_server()
        try:
            api_key = _server_module._api_key
            self._clear_jobs()

            # Run a quick command and wait for it to finish
            _, data = _post(port, "/exec",
                            {"command": "echo done", "timeout": 10},
                            api_key=api_key)
            job_id = data["jobId"]
            for _ in range(20):
                time.sleep(0.5)
                result = _get(port, f"/jobs/{job_id}", api_key=api_key)
                if result["status"] == "completed":
                    break

            # Nudge a completed job
            status, nudge_data = _post(port, f"/jobs/{job_id}/nudge",
                                       api_key=api_key)
            assert status == 200
            assert nudge_data["nudged"] is False
            assert nudge_data["processAlive"] is False
        finally:
            self._clear_jobs()
            server.shutdown()

    def test_nudge_running_job(self):
        server, port, _ = _start_test_server()
        try:
            api_key = _server_module._api_key
            self._clear_jobs()

            # Start a slow command
            _, data = _post(port, "/exec",
                            {"command": "ping -n 5 127.0.0.1", "timeout": 30},
                            api_key=api_key)
            job_id = data["jobId"]

            # Wait a moment for it to start
            time.sleep(1)

            # Nudge should succeed
            status, nudge_data = _post(port, f"/jobs/{job_id}/nudge",
                                       api_key=api_key)
            assert status == 200
            assert nudge_data["nudged"] is True
            assert nudge_data["processAlive"] is True

            # Wait for it to finish
            for _ in range(30):
                time.sleep(1)
                result = _get(port, f"/jobs/{job_id}", api_key=api_key)
                if result["status"] in ("completed", "failed", "timeout"):
                    break
        finally:
            self._clear_jobs()
            server.shutdown()

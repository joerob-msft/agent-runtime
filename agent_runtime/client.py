"""Python client for the agent-runtime HTTP API."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping, Optional


CredentialsRefresher = Callable[[], Mapping[str, str]]


@dataclass
class CommandResult:
    """Result of a completed command execution."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    acp_session_id: Optional[str] = None
    events: Optional[list[dict]] = None
    stop_reason: Optional[str] = None
    agent_type: Optional[str] = None


@dataclass
class Job:
    """A submitted command job with status tracking."""

    id: str
    status: str
    command: str
    result: Optional[CommandResult] = None
    last_output_at: Optional[float] = None
    process_alive: Optional[bool] = None


class RuntimeClient:
    """HTTP client for the agent-runtime async execution API."""

    def __init__(
        self,
        base_url: str,
        default_workdir: str = "",
        api_key: str = "",
        tunnel_token: str = "",
        credentials_refresher: Optional[CredentialsRefresher] = None,
        client_logger: Optional[logging.Logger] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_workdir = default_workdir
        self.api_key = api_key
        self.tunnel_token = tunnel_token
        self._credentials_refresher = credentials_refresher
        self._consecutive_poll_failures = 0
        self._last_health: dict[str, object] = {}
        self._logger = client_logger or logging.getLogger("agent_runtime.client")

    def _refresh_credentials(self) -> bool:
        """Try to refresh API credentials via the configured callback."""
        if not self._credentials_refresher:
            return False
        try:
            creds = self._credentials_refresher()
        except Exception:
            self._logger.debug("credentials_refresher failed", exc_info=True)
            return False

        new_key = creds.get("api_key", "")
        new_token = creds.get("tunnel_token", "")
        if not isinstance(new_key, str):
            new_key = ""
        if not isinstance(new_token, str):
            new_token = ""

        if new_key and new_key != self.api_key:
            self._logger.info(
                "Refreshed API key for %s (runtime likely restarted)",
                self.base_url,
            )
            self.api_key = new_key
            if new_token:
                self.tunnel_token = new_token
            return True
        return False

    def _headers(self) -> dict[str, str]:
        """Build request headers with auth."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.tunnel_token:
            headers["X-Tunnel-Authorization"] = f"tunnel {self.tunnel_token}"
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def submit(
        self,
        command: str,
        workdir: str = "",
        timeout: int = 300,
        env: Optional[dict[str, str]] = None,
        mode: str = "shell",
        session_key: str = "",
        shell: str = "",
        bootstrap_command: str = "",
        bootstrap_current_command: bool = False,
    ) -> Job:
        """Submit a command to the runtime with retry."""
        last_job: Optional[Job] = None
        for attempt in range(3):
            job = self._submit_once(
                command=command,
                workdir=workdir,
                timeout=timeout,
                env=env,
                mode=mode,
                session_key=session_key,
                shell=shell,
                bootstrap_command=bootstrap_command,
                bootstrap_current_command=bootstrap_current_command,
            )
            if (
                job.status == "failed"
                and job.result
                and "401" in job.result.stderr
                and self._refresh_credentials()
            ):
                job = self._submit_once(
                    command=command,
                    workdir=workdir,
                    timeout=timeout,
                    env=env,
                    mode=mode,
                    session_key=session_key,
                    shell=shell,
                    bootstrap_command=bootstrap_command,
                    bootstrap_current_command=bootstrap_current_command,
                )
            if job.status != "failed":
                return job
            last_job = job
            if job.result and any(
                f"HTTP {code}" in job.result.stderr
                for code in ("400", "401", "403", "404", "409")
            ):
                return job
            if attempt < 2:
                import time

                time.sleep(2**attempt)
        return last_job or Job(id="", status="failed", command=command)

    def submit_acp(
        self,
        prompt: str,
        workdir: str = "",
        timeout: int = 300,
        agent: str = "",
        session_id: str = "",
        model: str = "",
        effort: str = "",
        env: Optional[dict[str, str]] = None,
        persistent_session_key: str = "",
        persistent_shell: str = "",
    ) -> Job:
        """Submit an ACP prompt to the runtime."""
        payload: dict[str, object] = {
            "mode": "acp",
            "prompt": prompt,
            "agent": agent or "copilot --acp --stdio",
            "workdir": workdir or self.default_workdir,
            "timeout": timeout,
        }
        if session_id:
            payload["acp_session_id"] = session_id
        if model:
            payload["model"] = model
        if effort:
            payload["effort"] = effort
        if env:
            payload["env"] = env
        if persistent_session_key:
            payload["persistent_session_key"] = persistent_session_key
        if persistent_shell:
            payload["persistent_shell"] = persistent_shell

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/exec",
            data=data,
            headers=self._headers(),
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError:
                    result = {}
                return Job(
                    id=result.get("jobId", ""),
                    status="pending",
                    command=f"[acp] {prompt[:80]}",
                )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            return Job(
                id="",
                status="failed",
                command=f"[acp] {prompt[:80]}",
                result=CommandResult(
                    exit_code=-1,
                    stdout="",
                    stderr=str(e),
                    duration_ms=0,
                    timed_out=False,
                ),
            )

    def _submit_once(
        self,
        command: str,
        workdir: str,
        timeout: int,
        env: Optional[dict[str, str]] = None,
        mode: str = "shell",
        session_key: str = "",
        shell: str = "",
        bootstrap_command: str = "",
        bootstrap_current_command: bool = False,
    ) -> Job:
        """Single submit attempt."""
        payload: dict[str, object] = {
            "command": command,
            "workdir": workdir or self.default_workdir,
            "timeout": timeout,
        }
        if mode != "shell":
            payload["mode"] = mode
        if env:
            payload["env"] = env
        if mode == "persistent_shell":
            payload["sessionKey"] = session_key
            payload["shell"] = shell
            if bootstrap_command:
                payload["bootstrapCommand"] = bootstrap_command
            if bootstrap_current_command:
                payload["bootstrapCurrentCommand"] = True

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/exec",
            data=data,
            headers=self._headers(),
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError:
                    return Job(
                        id="",
                        status="failed",
                        command=command,
                        result=CommandResult(
                            exit_code=-1,
                            stdout="",
                            stderr=f"Invalid JSON from runtime: {raw[:500]}",
                            duration_ms=0,
                            timed_out=False,
                        ),
                    )
                return Job(
                    id=result.get("jobId", ""),
                    status="pending",
                    command=command,
                )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            err_body = ""
            if isinstance(e, urllib.error.HTTPError) and e.fp:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            return Job(
                id="",
                status="failed",
                command=command,
                result=CommandResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"{e}: {err_body}".strip(),
                    duration_ms=0,
                    timed_out=False,
                ),
            )

    def poll(self, job_id: str) -> Job:
        """Poll the runtime for job status."""
        job = self._poll_once(job_id)
        if (
            job.status == "failed"
            and job.result
            and "401" in job.result.stderr
            and self._refresh_credentials()
        ):
            job = self._poll_once(job_id)
        return job

    def _poll_once(self, job_id: str) -> Job:
        """Single poll attempt."""
        req = urllib.request.Request(
            f"{self.base_url}/jobs/{job_id}",
            headers=self._headers(),
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    self._consecutive_poll_failures += 1
                    if self._consecutive_poll_failures >= 5:
                        self._logger.warning(
                            "Poll %s: %d consecutive non-JSON responses",
                            job_id[:8],
                            self._consecutive_poll_failures,
                        )
                    return Job(id=job_id, status="running", command="")

                self._consecutive_poll_failures = 0
                result: Optional[CommandResult] = None
                if data.get("status") in ("completed", "failed", "timeout"):
                    result = CommandResult(
                        exit_code=data.get("exitCode", -1),
                        stdout=data.get("stdout", ""),
                        stderr=data.get("stderr", ""),
                        duration_ms=data.get("durationMs", 0),
                        timed_out=data.get("timedOut", False),
                        acp_session_id=data.get("acpSessionId"),
                        events=data.get("events"),
                        stop_reason=data.get("stopReason"),
                        agent_type="acp" if data.get("mode") == "acp" else None,
                    )

                return Job(
                    id=job_id,
                    status=data.get("status", "unknown"),
                    command=data.get("command", ""),
                    result=result,
                    last_output_at=data.get("lastOutputAt"),
                    process_alive=data.get("processAlive"),
                )
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self._consecutive_poll_failures = 0
                return Job(
                    id=job_id,
                    status="failed",
                    command="",
                    result=CommandResult(-1, "", "HTTP 401: auth rejected", 0, False),
                )
            self._consecutive_poll_failures += 1
            if self._consecutive_poll_failures >= 5:
                self._logger.warning(
                    "Poll %s: %d consecutive HTTP errors (last: %s)",
                    job_id[:8],
                    self._consecutive_poll_failures,
                    e,
                )
            return Job(id=job_id, status="running", command="")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self._consecutive_poll_failures += 1
            if self._consecutive_poll_failures >= 5:
                self._logger.warning(
                    "Poll %s: %d consecutive network failures (last: %s)",
                    job_id[:8],
                    self._consecutive_poll_failures,
                    e,
                )
            return Job(id=job_id, status="running", command="")

    def get_job_snapshot(self, job_id: str, tail_lines: int = 0) -> dict[str, object]:
        """Fetch raw job JSON, optionally including a preview tail."""
        try:
            return self._get_job_snapshot_once(job_id, tail_lines)
        except urllib.error.HTTPError as e:
            if e.code == 401 and self._refresh_credentials():
                return self._get_job_snapshot_once(job_id, tail_lines)
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except OSError:
                body = ""
            detail = body or str(e)
            raise RuntimeError(detail) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RuntimeError(str(e)) from e

    def _get_job_snapshot_once(self, job_id: str, tail_lines: int) -> dict[str, object]:
        """Single fetch attempt for raw job JSON."""
        url = f"{self.base_url}/jobs/{job_id}"
        if tail_lines > 0:
            url += "?" + urllib.parse.urlencode({"tailLines": tail_lines})

        req = urllib.request.Request(
            url,
            headers=self._headers(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"Invalid JSON from runtime: {raw[:500]}"
                ) from e
            if not isinstance(data, dict):
                raise RuntimeError("Invalid JSON object from runtime")
            return data

    def nudge(self, job_id: str) -> bool:
        """Send a nudge to a running job's stdin."""
        req = urllib.request.Request(
            f"{self.base_url}/jobs/{job_id}/nudge",
            data=b"{}",
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return bool(data.get("nudged", False))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as e:
            self._logger.debug("Nudge failed for %s: %s", job_id[:8], e)
            return False

    def health_check(self) -> dict[str, object]:
        """Check if the runtime is healthy and reachable."""
        req = urllib.request.Request(
            f"{self.base_url}/health",
            headers=self._headers(),
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if not isinstance(data, dict):
                    return {}
                if data.get("status") != "ok":
                    return {}
                if "jobs" not in data:
                    if data.get("authenticated") is False:
                        if self._refresh_credentials():
                            return self.health_check()
                        raise RuntimeError(
                            f"Agent runtime at {self.base_url} rejected the API key. "
                            f"Restart the agent to re-register with a fresh key."
                        )
                    raise RuntimeError(
                        f"Agent runtime at {self.base_url} is running an older sync command server. "
                        "Restart it with the current agent runtime entry point."
                    )
                if data.get("authenticated") is False:
                    if self._refresh_credentials():
                        return self.health_check()
                    raise RuntimeError(
                        f"Agent runtime at {self.base_url} rejected the API key. "
                        f"Restart the agent to re-register with a fresh key."
                    )
                self._last_health = data
                return data
        except RuntimeError:
            raise
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
            return {}

    def persistent_shell_supported(self, force_refresh: bool = False) -> bool:
        """Return True when the runtime reports persistent shell support."""
        if force_refresh or not self._last_health:
            health = self.health_check()
        else:
            health = self._last_health
        return bool(health.get("persistent_shell_supported"))

    def persistent_shell_default(self) -> str:
        """Return the runtime-reported default persistent shell, if any."""
        if not self._last_health:
            self.health_check()
        value = self._last_health.get("persistent_shell_default_shell")
        return value if isinstance(value, str) else ""

    def list_persistent_sessions(self) -> list[dict[str, object]]:
        """Return the currently tracked persistent shell sessions."""
        req = urllib.request.Request(
            f"{self.base_url}/sessions",
            headers=self._headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                sessions = data.get("sessions", [])
                if isinstance(sessions, list):
                    return [item for item in sessions if isinstance(item, dict)]
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
            return []
        return []

    def get_persistent_session(self, session_key: str) -> dict[str, object]:
        """Return metadata for a named persistent shell session."""
        if not session_key:
            return {}
        req = urllib.request.Request(
            f"{self.base_url}/sessions/{urllib.parse.quote(session_key, safe='')}",
            headers=self._headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data if isinstance(data, dict) else {}
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
            return {}

    def delete_persistent_session(self, session_key: str) -> bool:
        """Delete a named persistent shell session on the runtime."""
        if not session_key:
            return False
        req = urllib.request.Request(
            f"{self.base_url}/sessions/{urllib.parse.quote(session_key, safe='')}",
            headers=self._headers(),
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return bool(data.get("deleted"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return False

    def request_update(self) -> dict[str, object]:
        """Request a self-update from the runtime."""
        try:
            return self._request_update_once()
        except urllib.error.HTTPError as e:
            if e.code == 401 and self._refresh_credentials():
                try:
                    return self._request_update_once()
                except urllib.error.HTTPError as retry_error:
                    e = retry_error
            body = ""
            if e.fp:
                body = e.read().decode("utf-8", errors="replace")[:500]
            try:
                data = json.loads(body)
                return data if isinstance(data, dict) else {"response": data}
            except (json.JSONDecodeError, ValueError):
                return {"error": f"HTTP {e.code}: {body}".strip()}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return {"error": str(e)}

    def _request_update_once(self) -> dict[str, object]:
        """Single POST /update attempt."""
        req = urllib.request.Request(
            f"{self.base_url}/update",
            data=b"{}",
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else {"response": data}

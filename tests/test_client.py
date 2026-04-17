"""Tests for the shared runtime HTTP client."""

from __future__ import annotations

import json
import urllib.request

from agent_runtime.client import RuntimeClient


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_submit_includes_env_payload(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"jobId": "job-123"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = RuntimeClient("https://example.devtunnels.ms")
    client.submit(
        "echo hello",
        workdir=r"C:\repo",
        timeout=42,
        env={"DOTNET_ROOT": r"C:\Users\alice\AppData\Local\Microsoft\dotnet"},
    )

    assert captured["url"] == "https://example.devtunnels.ms/exec"
    assert captured["body"]["env"] == {
        "DOTNET_ROOT": r"C:\Users\alice\AppData\Local\Microsoft\dotnet"
    }


def test_submit_acp_includes_env_payload(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"jobId": "job-456"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = RuntimeClient("https://example.devtunnels.ms")
    client.submit_acp(
        prompt="Build the project",
        workdir=r"C:\repo",
        timeout=300,
        agent="copilot --acp --stdio",
        env={"PATH": r"C:\Users\alice\AppData\Local\Microsoft\dotnet"},
    )

    assert captured["url"] == "https://example.devtunnels.ms/exec"
    assert captured["body"]["mode"] == "acp"
    assert captured["body"]["env"] == {
        "PATH": r"C:\Users\alice\AppData\Local\Microsoft\dotnet"
    }


def test_submit_persistent_shell_includes_session_payload(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"jobId": "job-789"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = RuntimeClient("https://example.devtunnels.ms")
    client.submit(
        "Get-Location",
        workdir=r"C:\repo",
        timeout=60,
        mode="persistent_shell",
        session_key="workflow-session-123",
        shell="pwsh",
    )

    assert captured["body"]["mode"] == "persistent_shell"
    assert captured["body"]["sessionKey"] == "workflow-session-123"
    assert captured["body"]["shell"] == "pwsh"


def test_submit_acp_includes_persistent_session_payload(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(req, timeout=30):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"jobId": "job-acp-persistent"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = RuntimeClient("https://example.devtunnels.ms")
    client.submit_acp(
        prompt="Build the project",
        workdir=r"C:\repo",
        timeout=300,
        persistent_session_key="workflow-session-123",
        persistent_shell="pwsh",
    )

    assert captured["body"]["persistent_session_key"] == "workflow-session-123"
    assert captured["body"]["persistent_shell"] == "pwsh"


def test_submit_persistent_shell_includes_bootstrap_payload(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(req, timeout=30):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"jobId": "job-bootstrap"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = RuntimeClient("https://example.devtunnels.ms")
    client.submit(
        "Write-Output seeded",
        workdir=r"C:\repo",
        timeout=60,
        mode="persistent_shell",
        session_key="workflow-session-123",
        shell="pwsh",
        bootstrap_command="Write-Output seeded",
        bootstrap_current_command=True,
    )

    assert captured["body"]["bootstrapCommand"] == "Write-Output seeded"
    assert captured["body"]["bootstrapCurrentCommand"] is True


def test_delete_persistent_session_calls_runtime(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResponse({"deleted": True})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = RuntimeClient("https://example.devtunnels.ms")
    deleted = client.delete_persistent_session("workflow-session-123")

    assert deleted is True
    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/sessions/workflow-session-123")


def test_request_update_calls_runtime(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResponse({"status": "updating", "version": "0.4.0"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = RuntimeClient("https://example.devtunnels.ms")
    result = client.request_update()

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/update")
    assert result["status"] == "updating"

"""Tests for ACP client persistent shell integration."""

import sys
import textwrap
from pathlib import Path

import pytest

from agent_runtime.acp_client import run_acp_session_sync
from agent_runtime.persistent_shell import preferred_shell_kind, shared_manager


PREFERRED_SHELL = preferred_shell_kind()


def _write_fake_agent(tmp_path: Path) -> Path:
    script = tmp_path / "fake_acp_agent.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import sys

            current_cwd = "."

            def send(msg):
                sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\\n")
                sys.stdout.flush()

            def recv():
                line = sys.stdin.readline()
                if not line:
                    raise SystemExit(0)
                return json.loads(line)

            def request(msg_id, method, params):
                send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
                while True:
                    msg = recv()
                    if msg.get("id") != msg_id:
                        continue
                    if "error" in msg:
                        raise RuntimeError(msg["error"])
                    return msg.get("result", {})

            while True:
                msg = recv()
                method = msg.get("method")
                msg_id = msg.get("id")
                params = msg.get("params", {})

                if method == "initialize":
                    send({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "protocolVersion": 1,
                            "capabilities": {},
                            "serverInfo": {"name": "fake-agent", "version": "1.0"},
                        },
                    })
                elif method == "session/new":
                    current_cwd = params.get("cwd", ".")
                    send({"jsonrpc": "2.0", "id": msg_id, "result": {"sessionId": "fake-session"}})
                elif method == "session/load":
                    current_cwd = params.get("cwd", ".")
                    send({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {"sessionId": params.get("sessionId", "fake-session")},
                    })
                elif method == "session/set_config_option":
                    send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
                elif method == "session/prompt":
                    prompt_items = params.get("prompt", [])
                    prompt_text = " ".join(
                        item.get("text", "")
                        for item in prompt_items
                        if isinstance(item, dict)
                    ).lower()
                    if "seed" in prompt_text:
                        term_id = request(
                            100,
                            "terminal/create",
                            {
                                "command": "Invoke-Expression",
                                "args": [
                                    "function Get-WorkflowValue { 'persisted-function' }; "
                                    "$env:DEVPILOT_TEST_ENV='persisted-env'"
                                ],
                                "cwd": current_cwd,
                            },
                        )["terminalId"]
                        request(101, "terminal/wait_for_exit", {"terminalId": term_id})
                        output = "seeded\\n"
                    elif "reset" in prompt_text:
                        term_id = request(
                            150,
                            "terminal/create",
                            {
                                "command": "Invoke-Expression",
                                "args": ["Start-Sleep -Seconds 30"],
                                "cwd": current_cwd,
                            },
                        )["terminalId"]
                        request(151, "terminal/kill", {"terminalId": term_id})
                        term_id = request(
                            152,
                            "terminal/create",
                            {
                                "command": "Invoke-Expression",
                                "args": [
                                    "Get-WorkflowValue; "
                                    "Write-Output $env:DEVPILOT_TEST_ENV; "
                                    "Write-Output (Get-Location).Path"
                                ],
                                "cwd": current_cwd,
                            },
                        )["terminalId"]
                        request(153, "terminal/wait_for_exit", {"terminalId": term_id})
                        output = request(154, "terminal/output", {"terminalId": term_id}).get("output", "")
                    else:
                        term_id = request(
                            200,
                            "terminal/create",
                            {
                                "command": "Invoke-Expression",
                                "args": [
                                    "Get-WorkflowValue; Write-Output $env:DEVPILOT_TEST_ENV"
                                ],
                                "cwd": current_cwd,
                            },
                        )["terminalId"]
                        request(201, "terminal/wait_for_exit", {"terminalId": term_id})
                        output = request(202, "terminal/output", {"terminalId": term_id}).get("output", "")

                    send({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": output},
                            }
                        },
                    })
                    send({"jsonrpc": "2.0", "id": msg_id, "result": {"stopReason": "end_turn"}})
                else:
                    send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            """
        ),
        encoding="utf-8",
    )
    return script


def test_run_acp_session_reuses_persistent_shell_across_calls(tmp_path: Path) -> None:
    if PREFERRED_SHELL is None:
        pytest.skip("No supported persistent shell host available")

    manager = shared_manager()
    manager.close_all()
    script = _write_fake_agent(tmp_path)

    try:
        seed = run_acp_session_sync(
            agent_cmd=[sys.executable, str(script)],
            prompt="seed workflow state",
            workdir=str(tmp_path),
            timeout=30,
            persistent_session_key="acp-workflow",
            persistent_shell=PREFERRED_SHELL,
        )
        assert seed.stop_reason == "end_turn"
        assert "seeded" in seed.output_text

        read = run_acp_session_sync(
            agent_cmd=[sys.executable, str(script)],
            prompt="read workflow state",
            workdir=str(tmp_path),
            timeout=30,
            persistent_session_key="acp-workflow",
            persistent_shell=PREFERRED_SHELL,
        )

        assert read.stop_reason == "end_turn"
        assert read.output_text.splitlines() == [
            "persisted-function",
            "persisted-env",
        ]

        info = manager.get_session_info("acp-workflow")
        assert info is not None
        assert info["processAlive"] is True
        assert info["shell"] == PREFERRED_SHELL
    finally:
        manager.close_all()


def test_run_acp_session_replays_bootstrap_after_terminal_kill(tmp_path: Path) -> None:
    if PREFERRED_SHELL is None:
        pytest.skip("No supported persistent shell host available")

    manager = shared_manager()
    manager.close_all()
    script = _write_fake_agent(tmp_path)
    session_key = "acp-reset"
    bootstrap = "function Get-WorkflowValue { 'persisted-function' }"

    try:
        seeded = manager.run_command(
            session_key,
            bootstrap,
            shell=PREFERRED_SHELL,
            timeout=30,
            workdir=str(tmp_path),
            env={"DEVPILOT_TEST_ENV": "persisted-env"},
            bootstrap_command=bootstrap,
            bootstrap_current_command=True,
        )
        assert seeded.exit_code == 0

        reset = run_acp_session_sync(
            agent_cmd=[sys.executable, str(script)],
            prompt="reset workflow state",
            workdir=str(tmp_path),
            timeout=30,
            persistent_session_key=session_key,
            persistent_shell=PREFERRED_SHELL,
        )

        assert reset.stop_reason == "end_turn"
        assert reset.output_text.splitlines() == [
            "persisted-function",
            "persisted-env",
            str(tmp_path),
        ]

        info = manager.get_session_info(session_key)
        assert info is not None
        assert info["processAlive"] is True
    finally:
        manager.close_all()

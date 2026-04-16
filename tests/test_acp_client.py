"""Tests for the headless ACP client."""

import os
import unittest
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_runtime.acp_client import (
    ACPEvent,
    ACPResult,
    HeadlessApprovePolicy,
    NDJSONTransport,
    PermissionPolicy,
    RPCClient,
    _path_ok_for_read,
    _message_loop,
    run_acp_session,
)


class TestPermissionPolicy(unittest.TestCase):
    """Test the permission policy hierarchy."""

    def test_base_policy_denies_all(self):
        policy = PermissionPolicy()
        result = policy.decide("write_file", [{"id": "allow"}], {})
        self.assertIsNone(result)

    def test_headless_approves_path_inside_workdir(self):
        workdir = str(Path(__file__).parent.resolve())
        policy = HeadlessApprovePolicy(workdir)
        params = {
            "toolCall": {"name": "write_file", "rawInput": {
                "path": os.path.join(workdir, "somefile.txt"),
            }},
        }
        result = policy.decide("write_file", [{"id": "allow"}], params)
        self.assertEqual(result, {"id": "allow"})

    def test_headless_denies_path_outside_workdir(self):
        workdir = str(Path(__file__).parent.resolve())
        policy = HeadlessApprovePolicy(workdir)
        params = {
            "toolCall": {"name": "write_file", "rawInput": {
                "path": "C:\\Windows\\System32\\evil.txt",
            }},
        }
        result = policy.decide("write_file", [{"id": "allow"}], params)
        self.assertIsNone(result)

    def test_headless_checks_multiple_path_keys(self):
        workdir = str(Path(__file__).parent.resolve())
        policy = HeadlessApprovePolicy(workdir)
        # Uses "file" key instead of "path"
        params = {
            "toolCall": {"name": "write_file", "rawInput": {
                "file": "C:\\Windows\\System32\\evil.txt",
            }},
        }
        result = policy.decide("write_file", [{"id": "allow"}], params)
        self.assertIsNone(result)

    def test_headless_approves_safe_tool_without_path(self):
        workdir = str(Path(__file__).parent.resolve())
        policy = HeadlessApprovePolicy(workdir)
        params = {"toolCall": {"name": "search", "rawInput": {"query": "test"}}}
        result = policy.decide("search", [{"id": "allow"}], params)
        self.assertEqual(result, {"id": "allow"})


class TestPathValidation(unittest.TestCase):
    """Test path resolution against workdir."""

    def test_absolute_path_inside_workdir(self):
        workdir = Path(__file__).parent.resolve()
        path = str(workdir / "test_file.py")
        self.assertTrue(_path_ok_for_read(path, workdir))

    def test_absolute_path_outside_workdir(self):
        workdir = Path(__file__).parent.resolve()
        self.assertFalse(_path_ok_for_read("C:\\Windows\\System32\\cmd.exe", workdir))

    def test_relative_path_resolved_against_workdir(self):
        workdir = Path(__file__).parent.resolve()
        # "test_acp_client.py" is relative — should resolve against workdir
        self.assertTrue(_path_ok_for_read("test_acp_client.py", workdir))

    def test_traversal_attack_blocked(self):
        workdir = Path(__file__).parent.resolve()
        self.assertFalse(_path_ok_for_read("../../../../../../etc/passwd", workdir))


class TestACPResult(unittest.TestCase):
    """Test ACPResult dataclass."""

    def test_default_values(self):
        result = ACPResult()
        self.assertEqual(result.session_id, "")
        self.assertEqual(result.output_text, "")
        self.assertEqual(result.events, [])
        self.assertEqual(result.stop_reason, "")

    def test_populated_result(self):
        result = ACPResult(
            session_id="abc-123",
            output_text="Hello world",
            events=[ACPEvent(type="message", data={"text": "hi"})],
            stop_reason="end_turn",
        )
        self.assertEqual(result.session_id, "abc-123")
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].type, "message")


class TestACPEvent(unittest.TestCase):
    """Test ACPEvent dataclass."""

    def test_default_timestamp(self):
        event = ACPEvent(type="test")
        self.assertGreater(event.timestamp, 0)
        self.assertEqual(event.data, {})


class _FakeStream:
    async def read(self, _size: int) -> bytes:
        return b""

    def write(self, _data: bytes) -> None:
        return None

    async def drain(self) -> None:
        return None


class _FakeProcess:
    def __init__(self):
        self.stdin = _FakeStream()
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.returncode = 0

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


class _FakeTransport:
    def __init__(self, messages):
        self._messages = list(messages)

    async def recv(self, timeout=None):
        await asyncio.sleep(0)
        if self._messages:
            return self._messages.pop(0)
        return None


class _FakeRPC:
    def __init__(self):
        self.responses = []
        self.errors = []

    def resolve(self, _msg):
        return False

    async def respond(self, msg_id, result):
        self.responses.append((msg_id, result))

    async def respond_error(self, msg_id, code, message):
        self.errors.append((msg_id, code, message))


class TestACPEnv(unittest.TestCase):
    def test_run_acp_session_passes_env_to_agent_process(self):
        captured = {}
        fake_process = _FakeProcess()

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs.get("env")
            return fake_process

        async def fake_request(self, method, params, timeout):
            if method == "session/new":
                return {"sessionId": "session-123"}
            if method == "session/prompt":
                return {"stopReason": "end_turn"}
            return {}

        async def passthrough_message_loop(_transport, _rpc, wait_task, **_kwargs):
            return await wait_task

        with patch("agent_runtime.acp_client.asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
            with patch.object(RPCClient, "request", new=fake_request):
                with patch("agent_runtime.acp_client._message_loop", side_effect=passthrough_message_loop):
                    result = asyncio.run(run_acp_session(
                        agent_cmd=["python", "--version"],
                        prompt="hello",
                        workdir=str(Path(__file__).parent.resolve()),
                        env={"DEVPILOT_TEST_ENV": "agent-visible"},
                    ))

        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(captured["env"]["DEVPILOT_TEST_ENV"], "agent-visible")

    def test_message_loop_passes_env_to_terminal_process(self):
        captured = {}
        fake_process = _FakeProcess()

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs.get("env")
            return fake_process

        async def _run_test() -> None:
            wait_task = asyncio.create_task(asyncio.sleep(0.05, result={"done": True}))
            transport = _FakeTransport([{
                "jsonrpc": "2.0",
                "id": 1,
                "method": "terminal/create",
                "params": {
                    "command": "python",
                    "args": ["--version"],
                    "cwd": str(Path(__file__).parent.resolve()),
                },
            }])
            rpc = _FakeRPC()
            with patch("agent_runtime.acp_client.asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
                await _message_loop(
                    transport,
                    rpc,
                    wait_task,
                    events=[],
                    text_parts=[],
                    text_len_ref=[0],
                    replay_complete_ref=[True],
                    policy=PermissionPolicy(),
                    workdir=str(Path(__file__).parent.resolve()),
                    workdir_resolved=Path(__file__).parent.resolve(),
                    path_ok=lambda _path: True,
                    terminals={},
                    terminal_outputs={},
                    merged_env={"DEVPILOT_TEST_ENV": "terminal-visible"},
                    terminal_counter_ref=[0],
                    add_event=lambda _event: None,
                    timeout=1,
                )

        asyncio.run(_run_test())

        self.assertEqual(captured["env"]["DEVPILOT_TEST_ENV"], "terminal-visible")


if __name__ == "__main__":
    unittest.main()

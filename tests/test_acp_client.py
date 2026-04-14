"""Tests for the headless ACP client."""

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from agent_runtime.acp_client import (
    ACPEvent,
    ACPResult,
    HeadlessApprovePolicy,
    NDJSONTransport,
    PermissionPolicy,
    RPCClient,
    _path_ok_for_read,
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


if __name__ == "__main__":
    unittest.main()

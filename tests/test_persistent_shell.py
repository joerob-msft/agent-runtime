"""Tests for the experimental persistent PowerShell session manager."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_runtime.persistent_shell import (
    PersistentShellManager,
    available_shells,
    preferred_shell_kind,
)


PREFERRED_SHELL = preferred_shell_kind()


@unittest.skipUnless(PREFERRED_SHELL is not None, "No supported PowerShell shell available")
class TestPersistentShellManager(unittest.TestCase):
    def test_state_persists_across_commands(self) -> None:
        manager = PersistentShellManager()
        with tempfile.TemporaryDirectory() as tmp:
            workdir = str(Path(tmp).resolve())
            try:
                first = manager.run_command(
                    "workflow-a",
                    (
                        "function Get-WorkflowValue { 'persisted-function' }\n"
                        "Write-Output $env:DEVPILOT_TEST_ENV\n"
                        "Write-Output (Get-Location).Path"
                    ),
                    shell=PREFERRED_SHELL,
                    timeout=15,
                    workdir=workdir,
                    env={"DEVPILOT_TEST_ENV": "persisted-env"},
                )
                second = manager.run_command(
                    "workflow-a",
                    (
                        "Write-Output (Get-WorkflowValue)\n"
                        "Write-Output $env:DEVPILOT_TEST_ENV\n"
                        "Write-Output (Get-Location).Path"
                    ),
                    shell=PREFERRED_SHELL,
                    timeout=15,
                )
            finally:
                manager.close_all()

        self.assertEqual(first.exit_code, 0)
        self.assertEqual(second.exit_code, 0)
        self.assertEqual(
            second.stdout.splitlines(),
            ["persisted-function", "persisted-env", workdir],
        )

    def test_mutated_env_and_location_are_not_reset_each_command(self) -> None:
        manager = PersistentShellManager()
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp).resolve()
            nested = workdir / "nested"
            nested.mkdir()
            nested_literal = str(nested).replace("'", "''")
            try:
                first = manager.run_command(
                    "workflow-mutations",
                    (
                        "$env:DEVPILOT_TEST_ENV = 'mutated-env'\n"
                        f"Set-Location -LiteralPath '{nested_literal}'\n"
                        "function Get-MutatedValue { 'persisted-function' }"
                    ),
                    shell=PREFERRED_SHELL,
                    timeout=15,
                    workdir=str(workdir),
                    env={"DEVPILOT_TEST_ENV": "seeded-env"},
                )
                second = manager.run_command(
                    "workflow-mutations",
                    (
                        "Write-Output (Get-MutatedValue)\n"
                        "Write-Output $env:DEVPILOT_TEST_ENV\n"
                        "Write-Output (Get-Location).Path"
                    ),
                    shell=PREFERRED_SHELL,
                    timeout=15,
                )
            finally:
                manager.close_all()

        self.assertEqual(first.exit_code, 0)
        self.assertEqual(second.exit_code, 0)
        self.assertEqual(
            second.stdout.splitlines(),
            ["persisted-function", "mutated-env", str(nested)],
        )

    def test_reset_session_replays_seed_and_bootstrap(self) -> None:
        manager = PersistentShellManager()
        with tempfile.TemporaryDirectory() as tmp:
            workdir = str(Path(tmp).resolve())
            bootstrap = "function Get-BootstrapValue { 'bootstrapped' }"
            try:
                seeded = manager.run_command(
                    "workflow-bootstrap",
                    bootstrap,
                    shell=PREFERRED_SHELL,
                    timeout=15,
                    workdir=workdir,
                    env={"DEVPILOT_TEST_ENV": "seeded-env"},
                    bootstrap_command=bootstrap,
                    bootstrap_current_command=True,
                )
                manager.reset_session("workflow-bootstrap")
                after = manager.run_command(
                    "workflow-bootstrap",
                    (
                        "Write-Output (Get-BootstrapValue)\n"
                        "Write-Output $env:DEVPILOT_TEST_ENV\n"
                        "Write-Output (Get-Location).Path"
                    ),
                    shell=PREFERRED_SHELL,
                    timeout=15,
                )
            finally:
                manager.close_all()

        self.assertEqual(seeded.exit_code, 0)
        self.assertEqual(after.exit_code, 0)
        self.assertEqual(
            after.stdout.splitlines(),
            ["bootstrapped", "seeded-env", workdir],
        )

    def test_sessions_are_isolated(self) -> None:
        manager = PersistentShellManager()
        try:
            manager.run_command(
                "workflow-a",
                "Write-Output 'session-a'",
                shell=PREFERRED_SHELL,
                timeout=15,
                env={"DEVPILOT_ISOLATED_ENV": "only-a"},
            )
            result = manager.run_command(
                "workflow-b",
                (
                    "if ($env:DEVPILOT_ISOLATED_ENV) { "
                    "Write-Output $env:DEVPILOT_ISOLATED_ENV "
                    "} else { Write-Output 'missing' }"
                ),
                shell=PREFERRED_SHELL,
                timeout=15,
            )
        finally:
            manager.close_all()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), "missing")

    def test_timeout_drops_shell_state(self) -> None:
        manager = PersistentShellManager()
        try:
            seeded = manager.run_command(
                "workflow-timeout",
                "function Get-TimeoutValue { 'still-here' }",
                shell=PREFERRED_SHELL,
                timeout=15,
            )
            timed_out = manager.run_command(
                "workflow-timeout",
                "Start-Sleep -Seconds 2",
                shell=PREFERRED_SHELL,
                timeout=1,
            )
            after = manager.run_command(
                "workflow-timeout",
                (
                    "if (Get-Command Get-TimeoutValue -ErrorAction SilentlyContinue) { "
                    "Write-Output 'present' "
                    "} else { Write-Output 'missing' }"
                ),
                shell=PREFERRED_SHELL,
                timeout=15,
            )
        finally:
            manager.close_all()

        self.assertEqual(seeded.exit_code, 0)
        self.assertTrue(timed_out.timed_out)
        self.assertEqual(after.exit_code, 0)
        self.assertEqual(after.stdout.strip(), "missing")

    def test_close_session_resets_state(self) -> None:
        manager = PersistentShellManager()
        try:
            manager.run_command(
                "workflow-close",
                "function Get-CloseValue { 'before-close' }",
                shell=PREFERRED_SHELL,
                timeout=15,
            )
            manager.close_session("workflow-close")
            result = manager.run_command(
                "workflow-close",
                (
                    "if (Get-Command Get-CloseValue -ErrorAction SilentlyContinue) { "
                    "Write-Output 'present' "
                    "} else { Write-Output 'missing' }"
                ),
                shell=PREFERRED_SHELL,
                timeout=15,
            )
        finally:
            manager.close_all()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), "missing")

    def test_session_info_reports_shell_and_status(self) -> None:
        manager = PersistentShellManager()
        try:
            manager.run_command(
                "workflow-info",
                "Write-Output 'hello'",
                shell=PREFERRED_SHELL,
                timeout=15,
            )
            info = manager.get_session_info("workflow-info")
        finally:
            manager.close_all()

        assert info is not None
        self.assertEqual(info["shell"], PREFERRED_SHELL)
        self.assertEqual(info["status"], "idle")
        self.assertTrue(info["processAlive"])
        self.assertFalse(info["activeCommand"])

    def test_same_session_key_rejects_shell_change(self) -> None:
        shells = [kind for kind, supported in available_shells().items() if supported]
        if len(shells) < 2:
            self.skipTest("Only one supported shell available")

        manager = PersistentShellManager()
        try:
            manager.run_command(
                "workflow-shell-change",
                "Write-Output 'hello'",
                shell=shells[0],
                timeout=15,
            )
            with self.assertRaises(RuntimeError):
                manager.run_command(
                    "workflow-shell-change",
                    "Write-Output 'hello'",
                    shell=shells[1],
                    timeout=15,
                )
        finally:
            manager.close_all()


if __name__ == "__main__":
    unittest.main()

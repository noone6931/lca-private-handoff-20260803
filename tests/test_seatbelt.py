from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from local_agent.cancellation import RunCancelled
from local_agent.agent import AgentRuntime
from local_agent.config import AgentConfig
from local_agent.platform import seatbelt
from local_agent.platform.seatbelt import SeatbeltPreparationError
from local_agent.platform.seatbelt import prepare_seatbelt_command
from local_agent.tools.base import ToolContext
from local_agent.tools.shell import run_shell, run_tests


@contextmanager
def _fake_seatbelt_backend(root: Path):
    backend = root / "sandbox-exec"
    backend.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    backend.chmod(0o755)
    with (
        patch.object(seatbelt.sys, "platform", "darwin"),
        patch.object(seatbelt, "SEATBELT_EXECUTABLE", backend),
    ):
        yield backend


class SeatbeltAdapterTests(unittest.TestCase):
    def test_shell_transform_uses_profile_parameters_and_inner_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            runtime_temp = workspace / "runtime"
            runtime_temp.mkdir()
            with _fake_seatbelt_backend(workspace) as backend:
                prepared = prepare_seatbelt_command(
                    "printf hello > output.txt",
                    shell=True,
                    writable_roots=(workspace, runtime_temp, workspace),
                )

        self.assertEqual(prepared.argv[0], str(backend))
        self.assertEqual(prepared.argv[1], "-p")
        self.assertNotIn(str(workspace), prepared.argv[2])
        self.assertNotIn("network", prepared.argv[2])
        self.assertIn("(deny default)", prepared.argv[2])
        separator = prepared.argv.index("--")
        self.assertEqual(prepared.argv[separator + 1 :], ("/bin/sh", "-c", "printf hello > output.txt"))
        definitions = prepared.argv[3:separator]
        self.assertIn(f"LCA_WRITE_ROOT_0={workspace}", definitions)
        self.assertIn(f"LCA_WRITE_ROOT_1={runtime_temp}", definitions)
        self.assertEqual(prepared.writable_roots, (workspace, runtime_temp))
        self.assertFalse(prepared.shell)

    def test_structured_command_stays_structured_after_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with _fake_seatbelt_backend(workspace):
                prepared = prepare_seatbelt_command(
                    ("/usr/bin/python3", "-m", "unittest"),
                    shell=False,
                    writable_roots=(workspace,),
                )

        separator = prepared.argv.index("--")
        self.assertEqual(prepared.argv[separator + 1 :], ("/usr/bin/python3", "-m", "unittest"))
        self.assertNotIn("/bin/sh", prepared.argv[separator + 1 :])

    def test_unsupported_missing_backend_and_invalid_roots_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            missing = workspace / "missing-sandbox-exec"
            with patch.object(seatbelt.sys, "platform", "linux"):
                with self.assertRaises(SeatbeltPreparationError) as unsupported:
                    prepare_seatbelt_command(("true",), shell=False, writable_roots=(workspace,))
            with (
                patch.object(seatbelt.sys, "platform", "darwin"),
                patch.object(seatbelt, "SEATBELT_EXECUTABLE", missing),
            ):
                with self.assertRaises(SeatbeltPreparationError) as unavailable:
                    prepare_seatbelt_command(("true",), shell=False, writable_roots=(workspace,))
            with _fake_seatbelt_backend(workspace):
                with self.assertRaises(SeatbeltPreparationError) as invalid:
                    prepare_seatbelt_command(("true",), shell=False, writable_roots=(Path("relative"),))

        self.assertEqual(unsupported.exception.kind, "unsupported_platform")
        self.assertFalse(unsupported.exception.metadata()["sandboxed"])
        self.assertEqual(unavailable.exception.kind, "backend_unavailable")
        self.assertEqual(invalid.exception.kind, "invalid_root")
        for error in (unsupported.exception, unavailable.exception, invalid.exception):
            self.assertTrue(error.metadata()["sandbox_requested"])


class SeatbeltToolProjectionTests(unittest.TestCase):
    def test_runtime_projects_typed_mode_without_a_sandbox_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            runtime = AgentRuntime(
                AgentConfig(
                    provider="openai-compatible",
                    api_base_url="https://example.invalid/v1",
                    api_key="token",
                    model="model",
                    workspace=workspace,
                    approval_mode="yolo",
                    workflow_profile="coding",
                    sandbox_mode="seatbelt",
                ),
                show_tool_logs=False,
            )

        self.assertEqual(runtime._tool_context.sandbox_mode, "seatbelt")

    def test_off_path_does_not_probe_or_transform(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with (
                patch("local_agent.tools.shell.prepare_seatbelt_command") as prepare,
                patch("local_agent.tools.shell.trusted_runtime_temp_root") as runtime_temp,
                patch("local_agent.tools.shell._run_process") as execute,
            ):
                execute.return_value = subprocess.CompletedProcess("printf ok", 0, stdout="ok", stderr="")
                result = run_shell(
                    {"command": "printf ok"},
                    ToolContext(workspace=workspace, approval_mode="yolo"),
                )

        prepare.assert_not_called()
        runtime_temp.assert_not_called()
        self.assertEqual(execute.call_args.args[0], "printf ok")
        self.assertTrue(execute.call_args.kwargs["shell"])
        self.assertFalse(result.metadata["sandboxed"])

    def test_exhausted_test_budget_does_not_prepare_or_claim_a_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            context = ToolContext(
                workspace,
                "yolo",
                sandbox_mode="seatbelt",
                deadline_monotonic=time.monotonic() - 1,
            )
            with patch("local_agent.tools.shell.prepare_seatbelt_command") as prepare:
                result = run_tests({"command": "python3 -m unittest"}, context)

        prepare.assert_not_called()
        self.assertTrue(result.is_error)
        self.assertEqual(result.metadata["execution_status"], "not_run")
        self.assertFalse(result.metadata["sandboxed"])

    def test_shell_and_run_tests_use_distinct_typed_write_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            allowed = root / "allowed"
            runtime_temp = root / "runtime"
            for path in (workspace, allowed, runtime_temp):
                path.mkdir()
            context = ToolContext(
                workspace=workspace,
                approval_mode="yolo",
                sandbox_mode="seatbelt",
                allowed_dirs=(allowed,),
            )
            captured_roots: list[tuple[Path, ...]] = []

            def prepare(command, *, shell, writable_roots):
                captured_roots.append(tuple(writable_roots))
                return seatbelt.PreparedSeatbeltCommand(("/usr/bin/sandbox-exec", "--", "true"), tuple(writable_roots))

            with (
                patch("local_agent.tools.shell.trusted_runtime_temp_root", return_value=runtime_temp),
                patch("local_agent.tools.shell.prepare_seatbelt_command", side_effect=prepare),
                patch("local_agent.tools.shell._run_process") as execute,
            ):
                execute.return_value = subprocess.CompletedProcess(["wrapped"], 0, stdout="ok", stderr="")
                shell_result = run_shell({"command": "printf ok"}, context)
                test_result = run_tests(
                    {
                        "command": f"TMPDIR={shlex.quote(str(root / 'untrusted'))} python3 -m unittest",
                        "cwd": str(allowed),
                    },
                    context,
                )

        self.assertEqual(captured_roots[0], (workspace, runtime_temp))
        self.assertEqual(captured_roots[1], (workspace, allowed, runtime_temp))
        self.assertNotIn(root / "untrusted", captured_roots[1])
        self.assertTrue(shell_result.metadata["sandboxed"])
        self.assertTrue(test_result.metadata["sandboxed"])
        self.assertFalse(execute.call_args.kwargs["shell"])
        self.assertEqual(test_result.metadata["working_directory"], str(allowed))

    def test_success_nonzero_and_timeout_report_applied_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            context = ToolContext(workspace, "yolo", sandbox_mode="seatbelt")
            prepared = seatbelt.PreparedSeatbeltCommand(("/usr/bin/sandbox-exec", "--", "/bin/sh"), (workspace,))
            outcomes = (
                subprocess.CompletedProcess(prepared.argv, 0, stdout="ok", stderr=""),
                subprocess.CompletedProcess(prepared.argv, 7, stdout="", stderr="bad"),
                subprocess.TimeoutExpired(prepared.argv, 1, output="partial", stderr=""),
            )
            results = []
            for outcome in outcomes:
                with (
                    patch("local_agent.tools.shell._prepare_sandbox", return_value=prepared),
                    patch("local_agent.tools.shell._run_process", side_effect=[outcome] if isinstance(outcome, BaseException) else None) as execute,
                ):
                    if not isinstance(outcome, BaseException):
                        execute.return_value = outcome
                    results.append(run_shell({"command": "command", "timeout": 1}, context))

        self.assertEqual([result.is_error for result in results], [False, True, True])
        self.assertTrue(all(result.metadata["sandboxed"] for result in results))
        self.assertTrue(all(result.metadata["sandbox_platform"] == "macos" for result in results))
        self.assertEqual(results[0].metadata["sandbox_mode"], "workspace-write-network-deny")

    def test_adapter_and_launch_failures_are_not_run_and_never_claim_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            context = ToolContext(workspace, "yolo", sandbox_mode="seatbelt")
            prepared = seatbelt.PreparedSeatbeltCommand(("/usr/bin/sandbox-exec", "--", "/bin/sh"), (workspace,))
            with patch(
                "local_agent.tools.shell._prepare_sandbox",
                side_effect=SeatbeltPreparationError("backend_unavailable", "Seatbelt unavailable."),
            ):
                unavailable = run_shell({"command": "command"}, context)
            with (
                patch("local_agent.tools.shell._prepare_sandbox", return_value=prepared),
                patch("local_agent.tools.shell._run_process", side_effect=OSError("raw command must not leak")),
            ):
                launch_failed = run_shell({"command": "private command"}, context)
            with (
                patch("local_agent.tools.shell._prepare_sandbox", return_value=prepared),
                patch("local_agent.tools.shell._run_process", side_effect=OSError("private test command")),
            ):
                test_launch_failed = run_tests({"command": "python3 -m unittest"}, context)

        self.assertEqual(unavailable.metadata["sandbox_error_kind"], "backend_unavailable")
        self.assertEqual(launch_failed.metadata["sandbox_error_kind"], "launch_failed")
        self.assertEqual(unavailable.metadata["execution_status"], "not_run")
        self.assertEqual(launch_failed.metadata["execution_status"], "not_run")
        self.assertFalse(unavailable.metadata["sandboxed"])
        self.assertFalse(launch_failed.metadata["sandboxed"])
        self.assertFalse(test_launch_failed.metadata["sandboxed"])
        self.assertEqual(test_launch_failed.metadata["sandbox_error_kind"], "launch_failed")
        self.assertEqual(test_launch_failed.metadata["execution_status"], "not_run")
        self.assertNotIn("private command", launch_failed.content)
        self.assertNotIn("raw command", launch_failed.content)
        self.assertNotIn("private test command", test_launch_failed.content)
        self.assertNotIn("(deny default)", str(launch_failed.metadata))

    def test_approval_denial_happens_before_any_sandbox_preparation(self) -> None:
        from local_agent.tools import shell_tools
        from local_agent.tools.base import ToolRegistry

        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(
                Path(tmp).resolve(),
                "yolo",
                sandbox_mode="seatbelt",
                tool_approval={"shell": "deny"},
            )
            with patch("local_agent.tools.shell.prepare_seatbelt_command") as prepare:
                result = ToolRegistry(shell_tools()).execute("shell", {"command": "printf blocked"}, context)

        self.assertTrue(result.is_error)
        self.assertEqual(result.metadata["denial_kind"], "approval")
        prepare.assert_not_called()


def _kernel_seatbelt_available() -> bool:
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        return False
    probe = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", "(version 1) (allow default)", "--", "/usr/bin/true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return probe.returncode == 0


@unittest.skipUnless(_kernel_seatbelt_available(), "current verifier cannot apply a nested macOS Seatbelt profile")
class SeatbeltKernelTests(unittest.TestCase):
    def test_public_shell_workspace_sibling_descendant_network_and_environment_matrix(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as batch:
            root = Path(batch).resolve()
            workspace = root / "workspace"
            sibling = root / "sibling"
            workspace.mkdir()
            sibling.mkdir()
            context = ToolContext(workspace, "yolo", sandbox_mode="seatbelt", allowed_dirs=(sibling,))
            direct = run_shell({"command": "printf inside > direct.txt; printf out; printf err >&2"}, context)

            sibling_marker = sibling / "blocked.txt"
            deny_code = (
                "from pathlib import Path; import sys; "
                "p=Path(sys.argv[1]); "
                "\ntry: p.write_text('blocked')\nexcept OSError as exc: print('errno='+str(exc.errno))"
            )
            denied = run_shell(
                {"command": f"{shlex.quote(sys.executable)} -c {shlex.quote(deny_code)} {shlex.quote(str(sibling_marker))}"},
                context,
            )

            descendant_sibling_marker = sibling / "descendant-blocked.txt"
            descendant_deny_code = (
                "from pathlib import Path; import sys; "
                "\ntry: Path(sys.argv[1]).write_text('blocked')"
                "\nexcept OSError as exc: print('errno='+str(exc.errno))"
            )
            descendant_parent_code = (
                "import subprocess,sys; "
                "subprocess.run([sys.executable,'-c',sys.argv[1],sys.argv[2]],check=True)"
            )
            descendant_denied = run_shell(
                {
                    "command": f"{shlex.quote(sys.executable)} -c {shlex.quote(descendant_parent_code)} "
                    f"{shlex.quote(descendant_deny_code)} {shlex.quote(str(descendant_sibling_marker))}"
                },
                context,
            )

            descendant_marker = workspace / "descendant.txt"
            descendant_code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('child')"
            parent_code = "import subprocess,sys; subprocess.run([sys.executable,'-c',sys.argv[1],sys.argv[2]],check=True)"
            descendant = run_shell(
                {
                    "command": f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_code)} "
                    f"{shlex.quote(descendant_code)} {shlex.quote(str(descendant_marker))}"
                },
                context,
            )
            nonzero = run_shell({"command": "printf nonzero-out; printf nonzero-err >&2; exit 7"}, context)

            runtime_temp_marker = seatbelt.trusted_runtime_temp_root() / f"lca-seatbelt-{os.getpid()}.marker"
            runtime_temp_marker.unlink(missing_ok=True)
            runtime_temp = run_shell(
                {"command": f"printf runtime-temp > {shlex.quote(str(runtime_temp_marker))}"},
                context,
            )

            accepted = threading.Event()
            ready = threading.Event()
            port: list[int] = []

            def server() -> None:
                with socket.socket() as listening:
                    listening.bind(("127.0.0.1", 0))
                    listening.listen(1)
                    listening.settimeout(0.8)
                    port.append(listening.getsockname()[1])
                    ready.set()
                    try:
                        connection, _address = listening.accept()
                    except TimeoutError:
                        return
                    connection.close()
                    accepted.set()

            thread = threading.Thread(target=server)
            thread.start()
            self.assertTrue(ready.wait(1))
            network_code = (
                "import socket,sys; s=socket.socket(); s.settimeout(.3); "
                "\ntry: s.connect(('127.0.0.1',int(sys.argv[1]))); print('connected')"
                "\nexcept OSError as exc: print('errno='+str(exc.errno))"
            )
            network = run_shell(
                {"command": f"{shlex.quote(sys.executable)} -c {shlex.quote(network_code)} {port[0]}"},
                context,
            )
            thread.join(2)

            parent_credentials = {
                "Ai_Api_Key": "ai-secret",
                "DASHSCOPE_API_KEY": "dash-secret",
                "bailian_api_key": "bailian-secret",
                "CUSTOM_TOOLCHAIN": "kept",
            }
            with patch.dict(os.environ, parent_credentials, clear=False):
                environment = run_shell(
                    {
                        "command": f"{shlex.quote(sys.executable)} -c "
                        + shlex.quote(
                            "import os; print([k for k in os.environ if k.casefold() in "
                            "{'ai_api_key','dashscope_api_key','bailian_api_key'}]); "
                            "print(os.getenv('CUSTOM_TOOLCHAIN'))"
                        )
                    },
                    context,
                )
                parent_after = dict(os.environ)
            observations = {
                "direct": (workspace / "direct.txt").read_text(),
                "sibling_exists": sibling_marker.exists(),
                "descendant_sibling_exists": descendant_sibling_marker.exists(),
                "descendant": descendant_marker.read_text(),
                "accepted": accepted.is_set(),
                "runtime_temp": runtime_temp_marker.read_text(),
                "parent_credentials": {key: parent_after[key] for key in parent_credentials},
            }
            runtime_temp_marker.unlink(missing_ok=True)

            self.assertFalse(direct.is_error, direct.content)
            self.assertTrue(direct.metadata["sandboxed"])
            self.assertIn("out", direct.content)
            self.assertIn("err", direct.content)
            self.assertIn("errno=1", denied.content)
            self.assertIn("errno=1", descendant_denied.content)
            self.assertFalse(descendant.is_error, descendant.content)
            self.assertTrue(nonzero.is_error)
            self.assertTrue(nonzero.metadata["sandboxed"])
            self.assertTrue(nonzero.content.endswith("[exit_code] 7"))
            self.assertIn("nonzero-out", nonzero.content)
            self.assertIn("nonzero-err", nonzero.content)
            self.assertFalse(runtime_temp.is_error, runtime_temp.content)
            self.assertIn("errno=1", network.content)
            self.assertIn("[]", environment.content)
            self.assertIn("kept", environment.content)

        self.assertEqual(observations["direct"], "inside")
        self.assertFalse(observations["sibling_exists"])
        self.assertFalse(observations["descendant_sibling_exists"])
        self.assertEqual(observations["descendant"], "child")
        self.assertFalse(observations["accepted"])
        self.assertEqual(observations["runtime_temp"], "runtime-temp")
        self.assertEqual(observations["parent_credentials"], parent_credentials)

    def test_timeout_cancel_and_allowed_directory_run_tests(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as batch:
            root = Path(batch).resolve()
            workspace = root / "workspace"
            allowed = root / "allowed"
            workspace.mkdir()
            allowed.mkdir()
            context = ToolContext(workspace, "yolo", sandbox_mode="seatbelt", allowed_dirs=(allowed,))
            late = workspace / "late.txt"
            child_code = "from pathlib import Path; import sys,time; time.sleep(1.3); Path(sys.argv[1]).write_text('late')"
            parent_code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); time.sleep(10)"
            timeout_result = run_shell(
                {
                    "command": f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_code)} "
                    f"{shlex.quote(child_code)} {shlex.quote(str(late))}",
                    "timeout": 1,
                },
                context,
            )
            time.sleep(0.4)

            cancel_marker = workspace / "cancel.txt"
            cancel = threading.Event()
            timer = threading.Timer(0.15, cancel.set)
            timer.start()
            try:
                with self.assertRaises(RunCancelled):
                    run_shell(
                        {
                            "command": f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_code)} "
                            f"{shlex.quote(child_code)} {shlex.quote(str(cancel_marker))}",
                            "timeout": 5,
                        },
                        ToolContext(workspace, "yolo", sandbox_mode="seatbelt", cancel_event=cancel),
                    )
            finally:
                timer.cancel()
            time.sleep(0.4)

            (allowed / "test_allowed.py").write_text(
                "from pathlib import Path\nimport os, unittest\n"
                "class AllowedTests(unittest.TestCase):\n"
                "    def test_write(self):\n"
                "        Path('allowed.txt').write_text('ok')\n"
                "        self.assertEqual(os.getenv('CUSTOM_TOOLCHAIN'), 'explicit')\n"
                "        self.assertFalse(any(k.casefold() in {'ai_api_key','dashscope_api_key','bailian_api_key'} for k in os.environ))\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"AI_API_KEY": "secret"}, clear=False):
                tests = run_tests(
                    {
                        "command": "PYTHONPATH=. CUSTOM_TOOLCHAIN=explicit python3 -m unittest test_allowed",
                        "cwd": str(allowed),
                        "timeout": 10,
                    },
                    context,
                )
                parent_credential = os.environ["AI_API_KEY"]
            observations = {
                "late": late.exists(),
                "cancel": cancel_marker.exists(),
                "allowed": (allowed / "allowed.txt").read_text() if (allowed / "allowed.txt").exists() else None,
            }

            self.assertTrue(timeout_result.is_error)
            self.assertTrue(timeout_result.metadata["sandboxed"])
        self.assertFalse(tests.is_error, tests.content)
        self.assertTrue(tests.metadata["sandboxed"])

        self.assertFalse(observations["late"])
        self.assertFalse(observations["cancel"])
        self.assertEqual(observations["allowed"], "ok")
        self.assertEqual(parent_credential, "secret")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest
from pathlib import Path

from local_agent.execution.container_backend import (
    discover_container_probe,
    missing_container_probe_result,
    parse_container_probe_result,
)


DOCKER = Path("/usr/local/bin/docker")
PODMAN = Path("/opt/homebrew/bin/podman")


class ContainerIsolationBackendTests(unittest.TestCase):
    def test_auto_prefers_docker_and_builds_typed_probe(self) -> None:
        paths = {"docker": str(DOCKER), "podman": str(PODMAN)}
        plan = discover_container_probe(which=paths.get)
        assert plan is not None
        self.assertEqual(plan.engine, "docker")
        self.assertEqual(plan.executable, DOCKER)
        self.assertEqual(
            plan.argv,
            (str(DOCKER), "version", "--format", "{{json .Server}}"),
        )

    def test_explicit_podman_does_not_fall_back_to_docker(self) -> None:
        paths = {"docker": str(DOCKER), "podman": None}
        self.assertIsNone(discover_container_probe(preferred="podman", which=paths.get))

    def test_missing_engine_is_typed_unavailable(self) -> None:
        self.assertIsNone(discover_container_probe(which=lambda _name: None))
        result = missing_container_probe_result()
        self.assertEqual(result.capability.availability, "unavailable")
        self.assertEqual(result.capability.reason_code, "engine_missing")
        self.assertIsNone(result.identity)

    def test_daemon_timeout_spawn_failure_and_nonzero_are_distinct(self) -> None:
        plan = discover_container_probe(which=lambda _name: str(DOCKER))
        assert plan is not None
        cases = (
            ({"exit_code": None, "stdout": "", "spawn_failed": True}, "probe_spawn_failed"),
            ({"exit_code": None, "stdout": "", "timed_out": True}, "probe_timed_out"),
            ({"exit_code": 1, "stdout": ""}, "daemon_unavailable"),
        )
        for kwargs, reason in cases:
            with self.subTest(reason=reason):
                result = parse_container_probe_result(plan, **kwargs)
                self.assertEqual(result.capability.reason_code, reason)
                self.assertFalse(result.capability.enforces_isolation)

    def test_invalid_probe_output_never_claims_available(self) -> None:
        plan = discover_container_probe(which=lambda _name: str(DOCKER))
        assert plan is not None
        malformed = parse_container_probe_result(plan, exit_code=0, stdout="not-json")
        missing = parse_container_probe_result(plan, exit_code=0, stdout="{}")
        darwin = parse_container_probe_result(
            plan,
            exit_code=0,
            stdout=json.dumps({"Version": "1", "Os": "darwin", "Arch": "arm64"}),
        )
        self.assertEqual(malformed.capability.reason_code, "probe_invalid_json")
        self.assertEqual(missing.capability.reason_code, "probe_invalid_identity")
        self.assertEqual(darwin.capability.reason_code, "probe_invalid_identity")

    def test_docker_ready_requires_linux_server_identity(self) -> None:
        plan = discover_container_probe(which=lambda _name: str(DOCKER))
        assert plan is not None
        result = parse_container_probe_result(
            plan,
            exit_code=0,
            stdout=json.dumps(
                {
                    "Platform": {"Name": "Docker Engine"},
                    "Version": "28.0.0",
                    "Os": "linux",
                    "Arch": "arm64",
                }
            ),
        )
        self.assertEqual(result.capability.availability, "available")
        self.assertTrue(result.capability.enforces_isolation)
        self.assertEqual(result.identity.server_version, "28.0.0")
        self.assertEqual(
            result.identity.event_payload(),
            {
                "engine": "docker",
                "executable": str(DOCKER),
                "server_version": "28.0.0",
                "server_os": "linux",
                "server_arch": "arm64",
            },
        )

    def test_podman_ready_parses_its_structured_shape(self) -> None:
        plan = discover_container_probe(preferred="podman", which=lambda _name: str(PODMAN))
        assert plan is not None
        result = parse_container_probe_result(
            plan,
            exit_code=0,
            stdout=json.dumps(
                {
                    "host": {"os": "linux", "arch": "amd64"},
                    "version": {"Version": "5.6.0"},
                }
            ),
        )
        self.assertEqual(result.capability.reason_code, "engine_ready")
        self.assertEqual(result.identity.engine, "podman")

    def test_probe_does_not_accept_relative_resolver_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute"):
            discover_container_probe(which=lambda _name: "docker")


if __name__ == "__main__":
    unittest.main()

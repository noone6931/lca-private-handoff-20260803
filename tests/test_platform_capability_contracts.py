from __future__ import annotations

import ast
import unittest
from pathlib import Path

from local_agent.agents.contracts import AgentSnapshot, AgentSpec, JobSnapshot
from local_agent.execution.contracts import (
    AppliedIsolationProof,
    IsolationBackendCapability,
    IsolationRequest,
)
from local_agent.extensions.contracts import ExtensionManifest, ExtensionToolDeclaration
from local_agent.providers.capabilities import ProviderCapabilities, ProviderDescriptor


ROOT = Path("/workspace")


class IsolationContractTests(unittest.TestCase):
    def test_required_container_request_requires_proof_and_rejects_local(self) -> None:
        request = IsolationRequest(
            mode="required",
            profile="workspace-write",
            backend="container",
            network_policy="deny",
            workspace=ROOT,
            readable_roots=(Path("/requirements"),),
            writable_roots=(ROOT,),
        )
        self.assertTrue(request.requires_applied_proof)
        capability = IsolationBackendCapability(
            backend="container",
            availability="available",
            reason_code="engine_ready",
            supported_profiles=frozenset({"read-only", "workspace-write"}),
            supported_network_policies=frozenset({"deny", "allow"}),
            enforces_isolation=True,
        )
        self.assertTrue(capability.supports(request))
        with self.assertRaisesRegex(ValueError, "local backend"):
            IsolationRequest(
                mode="required",
                profile="workspace-write",
                backend="local",
                network_policy="deny",
                workspace=ROOT,
                writable_roots=(ROOT,),
            )

    def test_applied_proof_is_bounded_typed_metadata(self) -> None:
        proof = AppliedIsolationProof(
            backend="container",
            backend_instance_id="opaque-id",
            profile="workspace-write",
            network_policy="deny",
            workspace=ROOT,
            readable_roots=(Path("/requirements"),),
            writable_roots=(ROOT,),
            image_digest="sha256:abc",
        )
        self.assertEqual(proof.event_payload()["applied"], True)
        self.assertNotIn("environment", proof.event_payload())

    def test_read_only_rejects_writable_roots(self) -> None:
        with self.assertRaisesRegex(ValueError, "read-only"):
            IsolationRequest(
                mode="required",
                profile="read-only",
                backend="container",
                network_policy="deny",
                workspace=ROOT,
                writable_roots=(ROOT,),
            )


class ExtensionContractTests(unittest.TestCase):
    def test_manifest_namespaces_tools(self) -> None:
        tool = ExtensionToolDeclaration(
            name="search",
            tier="read",
            input_schema={"type": "object", "properties": {}},
            output_bytes=4096,
        )
        manifest = ExtensionManifest(
            plugin_id="example",
            version="1.0.0",
            transport="mcp-stdio",
            tools=(tool,),
            server_command=("example-mcp",),
        )
        self.assertEqual(manifest.namespaced_tool_name(tool), "plugin__example__search")

    def test_manifest_rejects_duplicate_or_ambiguous_transport(self) -> None:
        tool = ExtensionToolDeclaration(
            name="search",
            tier="read",
            input_schema={"type": "object"},
            output_bytes=1,
        )
        with self.assertRaisesRegex(ValueError, "unique"):
            ExtensionManifest(
                plugin_id="example",
                version="1",
                transport="mcp-stdio",
                tools=(tool, tool),
                server_command=("server",),
            )
        with self.assertRaisesRegex(ValueError, "connector_id"):
            ExtensionManifest(
                plugin_id="example",
                version="1",
                transport="connector",
            )


class AgentContractTests(unittest.TestCase):
    def test_implement_agent_requires_separate_worktree(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be the primary"):
            AgentSpec(
                agent_id="agent-1",
                role="implement",
                parent_agent_id="parent",
                workspace=ROOT,
                worktree=ROOT,
                budget_seconds=60,
                tool_allowlist=frozenset({"read_file", "apply_patch"}),
            )

    def test_agent_and_job_terminal_truth(self) -> None:
        with self.assertRaisesRegex(ValueError, "result_ref"):
            AgentSnapshot(
                agent_id="agent-1",
                state="completed",
                origin_run_id="run-1",
            )
        with self.assertRaisesRegex(ValueError, "execution_ref"):
            JobSnapshot(
                job_id="job-1",
                owner_agent_id="agent-1",
                state="completed",
                cwd=ROOT,
            )


class ProviderContractTests(unittest.TestCase):
    def test_provider_capability_is_explicit(self) -> None:
        descriptor = ProviderDescriptor(
            provider_id="bailian",
            transport="openai-chat",
            auth_scheme="api-key",
            tool_protocol="openai-tools",
            context_window=128_000,
            capabilities=ProviderCapabilities(
                streaming=True,
                tool_calls=True,
                vision=False,
                structured_output=True,
                web_search=True,
                prompt_cache=False,
                reasoning=False,
            ),
        )
        descriptor.require_capability("tool_calls")
        with self.assertRaisesRegex(ValueError, "vision"):
            descriptor.require_capability("vision")

    def test_structured_output_is_independent_from_tool_calls(self) -> None:
        descriptor = ProviderDescriptor(
            provider_id="structured-text",
            transport="openai-chat",
            auth_scheme="bearer",
            tool_protocol="openai-tools",
            context_window=4096,
            capabilities=ProviderCapabilities(
                streaming=True,
                tool_calls=False,
                vision=False,
                structured_output=True,
                web_search=False,
                prompt_cache=False,
                reasoning=False,
            ),
        )
        descriptor.require_capability("structured_output")


class PlatformContractArchitectureTests(unittest.TestCase):
    def test_contracts_do_not_depend_on_runtime_frontend_or_tool_implementations(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "local_agent"
        paths = (
            root / "execution" / "contracts.py",
            root / "extensions" / "contracts.py",
            root / "agents" / "contracts.py",
            root / "providers" / "capabilities.py",
        )
        forbidden = {"agent", "runtime", "frontends", "tools"}
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.lstrip(".").split(".")[0])
            self.assertTrue(forbidden.isdisjoint(imported), f"{path}: {sorted(forbidden & imported)}")


if __name__ == "__main__":
    unittest.main()

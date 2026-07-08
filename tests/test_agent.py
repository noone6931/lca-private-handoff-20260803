from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.agent import AgentRuntime
from local_agent.agent import _resolve_compaction_threshold_chars
from local_agent.config import AgentConfig


class _FailingClient:
    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        raise AssertionError("LLM should not be called after the budget is exhausted")


class _FinalClient:
    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        return type("Response", (), {"message": {"content": "done"}})()


class _TimeoutRecordingClient:
    timeouts: list[float] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        self.timeouts.append(timeout)
        return type("Response", (), {"message": {"content": "done"}})()


class _MessageRecordingClient:
    messages: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).messages = messages
        return type("Response", (), {"message": {"content": "done"}})()


class _ReadFileThenFinalClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if len(type(self).calls) == 1:
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_read",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": "README.md"}),
                                },
                            }
                        ],
                    }
                },
            )()
        return type("Response", (), {"message": {"content": "done with evidence"}})()


class _SummaryThenFinalClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        self.calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if not tools:
            return type("Response", (), {"message": {"content": "LLM kept the important earlier facts."}})()
        return type("Response", (), {"message": {"content": "done"}})()


class _MemoryConsolidationClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        self.calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if tools:
            return type("Response", (), {"message": {"content": "Finished the task and learned a convention."}})()
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": json.dumps(
                        {
                            "project": [],
                            "decisions": [],
                            "conventions": ["Use focused tests before the full test suite in this project."],
                            "learned": ["When changing memory code, verify both focused agent tests and config tests."],
                        }
                    )
                }
            },
        )()


class _InvalidMemoryConsolidationClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        self.calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if tools:
            return type("Response", (), {"message": {"content": "Finished the task."}})()
        return type("Response", (), {"message": {"content": "not json"}})()


class _TwoToolClient:
    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "unknown_one", "arguments": "{}"},
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "unknown_two", "arguments": "{}"},
                        },
                    ],
                }
            },
        )()


class _LengthToolClient:
    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        return type(
            "Response",
            (),
            {
                "finish_reason": "length",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "shell", "arguments": "{\"command\":\"unterminated"},
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "run_tests", "arguments": "{}"},
                        },
                    ],
                },
            },
        )()


class _RepeatingToolClient:
    calls = 0
    tools_seen: list[int] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls += 1
        type(self).tools_seen.append(len(tools))
        if not tools:
            return type("Response", (), {"message": {"content": "final answer from collected evidence"}})()
        arguments = '{"b": 2, "a": 1}' if type(self).calls % 2 else '{"a": 1, "b": 2}'
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{type(self).calls}",
                            "type": "function",
                            "function": {"name": "unknown_repeat", "arguments": arguments},
                        }
                    ],
                }
            },
        )()


class _UselessSearchPatternClient:
    calls = 0
    tools_seen: list[int] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls += 1
        type(self).tools_seen.append(len(tools))
        if not tools:
            return type("Response", (), {"message": {"content": "final answer after empty search evidence"}})()
        path = f"dir{type(self).calls % 10}"
        pattern = "MissingBusinessTerm" if type(self).calls % 2 else "missingbusinessterm"
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{type(self).calls}",
                            "type": "function",
                            "function": {
                                "name": "search_code",
                                "arguments": json.dumps({"pattern": pattern, "path": path}),
                            },
                        }
                    ],
                }
            },
        )()


class _UselessLspSymbolClient:
    calls = 0
    tools_seen: list[int] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls += 1
        type(self).tools_seen.append(len(tools))
        if not tools:
            return type("Response", (), {"message": {"content": "final answer after empty lsp evidence"}})()
        query = f"MissingGeneratedSymbol{type(self).calls}"
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{type(self).calls}",
                            "type": "function",
                            "function": {
                                "name": "lsp_workspace_symbols",
                                "arguments": json.dumps({"query": query, "path": "."}),
                            },
                        }
                    ],
                }
            },
        )()


class _AllowedDirRequirementClient:
    calls: list[dict] = []
    doc_path: str = ""

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        tool_names = [schema.get("function", {}).get("name") for schema in tools]
        type(self).calls.append({"messages": messages, "tool_names": tool_names})
        if len(type(self).calls) == 1:
            return type("Response", (), {"message": {"content": "premature answer without reading docs"}})()
        if len(type(self).calls) == 2:
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_read_req",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": type(self).doc_path}),
                                },
                            }
                        ],
                    }
                },
            )()
        return type("Response", (), {"message": {"content": "done after reading requirement docs"}})()


class _RepeatedReadFileRangeClient:
    calls = 0
    tools_seen: list[int] = []
    file_path: str = ""

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls += 1
        type(self).tools_seen.append(len(tools))
        if not tools:
            return type("Response", (), {"message": {"content": "final answer after enough file evidence"}})()
        start_line = type(self).calls
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{type(self).calls}",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps(
                                    {
                                        "path": type(self).file_path,
                                        "start_line": start_line,
                                        "end_line": start_line,
                                    }
                                ),
                            },
                        }
                    ],
                }
            },
        )()


class _InterruptingRegistry:
    def schemas(self):
        return []

    def execute(self, name, raw_arguments, context):
        raise KeyboardInterrupt


class _UnexpectedRegistry:
    def schemas(self):
        return []

    def execute(self, name, raw_arguments, context):
        raise AssertionError("Tool should not execute after finish_reason=length")


class AgentRuntimeTests(unittest.TestCase):
    def test_runtime_state_dir_keeps_sessions_and_todos_out_of_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            state_dir = root / "state" / "workspace-key"
            workspace.mkdir()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                state_dir=state_dir,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _FinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("hello")
                todo_path = state_dir / "todos" / f"{runtime._session.session_id}.json"
                runtime._registry.execute(
                    "todo_add",
                    {"id": "T1", "task": "Track state dir"},
                    runtime._tool_context,
                )

            self.assertEqual(result, "done")
            self.assertTrue((state_dir / "sessions" / f"{runtime._session.session_id}.jsonl").exists())
            self.assertTrue(todo_path.exists())
            self.assertFalse((workspace / ".local-agent" / "sessions").exists())
            self.assertFalse((workspace / ".local-agent" / "todos").exists())

    def test_allowed_dirs_are_visible_to_the_model(self) -> None:
        _MessageRecordingClient.messages = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            requirements = root / "requirements"
            workspace.mkdir()
            requirements.mkdir()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                allowed_dirs=(requirements,),
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _MessageRecordingClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("hello")

        system_content = runtime._messages[0]["content"]
        sent_system = [message for message in _MessageRecordingClient.messages if message.get("role") == "system"][0]
        self.assertEqual(result, "done")
        self.assertIn("[Workspace roots]", system_content)
        self.assertIn(f"Primary workspace (--cwd): {workspace}", system_content)
        self.assertIn(str(requirements), system_content)
        self.assertIn("first list/read the relevant allowed directory by its exact absolute path", system_content)
        self.assertIn(str(requirements), sent_system["content"])
        self.assertEqual(sent_system["content"].count("[Workspace roots]"), 1)

    def test_current_task_contract_is_sent_to_provider_context(self) -> None:
        _MessageRecordingClient.messages = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _MessageRecordingClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("最后必须按以下结构输出：1 项目边界判断；2 证据文件路径")

        sent_system = [message for message in _MessageRecordingClient.messages if message.get("role") == "system"][0]
        self.assertEqual(result, "done")
        self.assertIn("[Current task contract]", sent_system["content"])
        self.assertIn("最后必须按以下结构输出", sent_system["content"])
        self.assertIn("Do not replace the requested final analysis with a summary of the last file", sent_system["content"])
        self.assertIn("File paths in final answers must be evidence-backed", sent_system["content"])
        self.assertEqual(sent_system["content"].count("[Current task contract]"), 1)
        self.assertNotIn("[Current task contract]", runtime._messages[0]["content"])

    def test_evidence_ledger_is_sent_to_provider_context_after_tool_results(self) -> None:
        _ReadFileThenFinalClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "README.md").write_text("# Demo\nEvidence matters.\n", encoding="utf-8")
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                context_char_budget=0,
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _ReadFileThenFinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("先读取 README，再回答")
                evidence_events = [
                    json.loads(line)
                    for line in runtime._session.path.read_text(encoding="utf-8").splitlines()
                    if json.loads(line).get("event") == "evidence"
                ]

        second_call_messages = _ReadFileThenFinalClient.calls[1]["messages"]
        sent_system = [message for message in second_call_messages if message.get("role") == "system"][0]
        self.assertEqual(result, "done with evidence")
        self.assertIn("[Evidence ledger]", sent_system["content"])
        self.assertIn("Runtime-collected tool evidence", sent_system["content"])
        self.assertIn("read_file README.md", sent_system["content"])
        self.assertIn("[README.md#", sent_system["content"])
        self.assertNotIn("[Evidence ledger]", runtime._messages[0]["content"])
        self.assertEqual(len(evidence_events), 1)
        self.assertEqual(evidence_events[0]["payload"]["tool"], "read_file")

    def test_requirement_tasks_must_read_allowed_directory_docs_before_answering(self) -> None:
        _AllowedDirRequirementClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            requirements = root / "requirements"
            workspace.mkdir()
            requirements.mkdir()
            doc = requirements / "需求文档-demo.md"
            doc.write_text("# Requirement\nRead me first.\n", encoding="utf-8")
            _AllowedDirRequirementClient.doc_path = str(doc)
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                allowed_dirs=(requirements,),
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _AllowedDirRequirementClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("读取需求目录中的需求文档，然后结合源码分析")

        self.assertEqual(result, "done after reading requirement docs")
        self.assertEqual(set(_AllowedDirRequirementClient.calls[0]["tool_names"]), {"list_files", "read_file"})
        self.assertEqual(set(_AllowedDirRequirementClient.calls[1]["tool_names"]), {"list_files", "read_file"})
        self.assertIn("search_code", _AllowedDirRequirementClient.calls[2]["tool_names"])
        sent_text = "\n".join(
            str(message.get("content") or "")
            for call in _AllowedDirRequirementClient.calls[:2]
            for message in call["messages"]
        )
        self.assertIn("[Runtime tool requirement]", sent_text)
        self.assertIn(str(doc), sent_text)
        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        self.assertTrue(any(str(doc) in str(message.get("content") or "") for message in tool_messages))

    def test_budget_seconds_stops_before_next_llm_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=100,
                budget_seconds=1,
                approval_mode="yolo",
            )
            with (
                patch("local_agent.agent.OpenAICompatibleClient", _FailingClient),
                patch("local_agent.agent.time.monotonic", side_effect=[0.0, 2.0]),
            ):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("hello")

        self.assertEqual(result, "Stopped after reaching budget_seconds=1.")

    def test_zero_max_steps_means_unlimited_not_zero_iterations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _FinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("hello")

        self.assertEqual(result, "done")

    def test_startup_memory_is_injected_as_advisory_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            state_dir = root / "state"
            workspace.mkdir()
            memory_dir = workspace / ".local-agent" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "project.md").write_text("Use pytest for this project.\n", encoding="utf-8")
            state_memory_dir = state_dir / "memory"
            state_memory_dir.mkdir(parents=True)
            (state_memory_dir / "learned.md").write_text("Run focused memory tests first.\n", encoding="utf-8")
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                state_dir=state_dir,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _FinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)

        system_content = runtime._messages[0]["content"]
        self.assertIn("[Memory]", system_content)
        self.assertIn(".local-agent/memory/project.md", system_content)
        self.assertIn(str(state_memory_dir / "learned.md"), system_content)
        self.assertIn("Use pytest for this project.", system_content)
        self.assertIn("Run focused memory tests first.", system_content)
        self.assertIn("advisory", system_content)

    def test_user_and_project_agents_context_are_injected_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            user_config = root / "user-config"
            workspace.mkdir()
            user_config.mkdir()
            project_dir = workspace / ".local-agent"
            project_dir.mkdir()
            (user_config / "AGENTS.md").write_text("Prefer concise final answers.\n", encoding="utf-8")
            (project_dir / "AGENTS.md").write_text("Project context: run unit tests.\n", encoding="utf-8")
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with (
                patch.dict("os.environ", {"AGENT_CONFIG_DIR": str(user_config)}),
                patch("local_agent.agent.OpenAICompatibleClient", _FinalClient),
            ):
                runtime = AgentRuntime(config, show_tool_logs=False)

        system_content = runtime._messages[0]["content"]
        self.assertIn("[User/project context]", system_content)
        self.assertIn(str(user_config / "AGENTS.md"), system_content)
        self.assertIn(".local-agent/AGENTS.md", system_content)
        self.assertIn("Prefer concise final answers.", system_content)
        self.assertIn("Project context: run unit tests.", system_content)
        self.assertLess(
            system_content.index("Prefer concise final answers."),
            system_content.index("Project context: run unit tests."),
        )

    def test_user_and_project_sticky_rules_are_sent_to_provider_context(self) -> None:
        _MessageRecordingClient.messages = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            user_config = root / "user-config"
            workspace.mkdir()
            user_config.mkdir()
            project_dir = workspace / ".local-agent"
            project_dir.mkdir()
            (user_config / "RULES.md").write_text("Never commit unless asked.\n", encoding="utf-8")
            (project_dir / "RULES.md").write_text("Always summarize verification.\n", encoding="utf-8")
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with (
                patch.dict("os.environ", {"AGENT_CONFIG_DIR": str(user_config)}),
                patch("local_agent.agent.OpenAICompatibleClient", _MessageRecordingClient),
            ):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("hello")

        sent_system = [message for message in _MessageRecordingClient.messages if message.get("role") == "system"][0]
        self.assertEqual(result, "done")
        self.assertIn("[Sticky rules]", sent_system["content"])
        self.assertIn("Never commit unless asked.", sent_system["content"])
        self.assertIn("Always summarize verification.", sent_system["content"])
        self.assertNotIn("[Sticky rules]", runtime._messages[0]["content"])

    def test_authored_skills_metadata_is_injected_without_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            skills_dir = workspace / ".local-agent" / "skills"
            review_skill = skills_dir / "code-review"
            hidden_skill = skills_dir / "hidden"
            review_skill.mkdir(parents=True)
            hidden_skill.mkdir()
            (review_skill / "SKILL.md").write_text(
                "---\n"
                "name: code-review\n"
                "description: Use when reviewing a patch before commit.\n"
                "---\n"
                "\n"
                "# Code Review\n"
                "\n"
                "SECRET_BODY_SHOULD_NOT_BE_IN_SYSTEM_PROMPT\n",
                encoding="utf-8",
            )
            (hidden_skill / "SKILL.md").write_text(
                "---\n"
                "name: hidden\n"
                "description: Do not show.\n"
                "hide: true\n"
                "---\n",
                encoding="utf-8",
            )
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _FinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)

        system_content = runtime._messages[0]["content"]
        self.assertIn("[Available project skills]", system_content)
        self.assertIn("code-review: Use when reviewing a patch before commit.", system_content)
        self.assertIn(".local-agent/skills/code-review/SKILL.md", system_content)
        self.assertIn("read its SKILL.md with read_file", system_content)
        self.assertNotIn("SECRET_BODY_SHOULD_NOT_BE_IN_SYSTEM_PROMPT", system_content)
        self.assertNotIn("hidden", system_content)

    def test_authored_skill_without_frontmatter_uses_first_body_line_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            skill_dir = workspace / ".local-agent" / "skills" / "release-check"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "# Release Check\n"
                "\n"
                "Use before cutting a local release.\n"
                "\n"
                "Full procedure stays out of startup context.\n",
                encoding="utf-8",
            )
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _FinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)

        system_content = runtime._messages[0]["content"]
        self.assertIn("release-check: Use before cutting a local release.", system_content)
        self.assertNotIn("Full procedure stays out of startup context.", system_content)

    def test_llm_timeout_is_clamped_to_remaining_budget(self) -> None:
        _TimeoutRecordingClient.timeouts = []
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                request_timeout=120,
                max_steps=0,
                budget_seconds=10,
                approval_mode="yolo",
            )
            with (
                patch("local_agent.agent.OpenAICompatibleClient", _TimeoutRecordingClient),
                patch("local_agent.agent.time.monotonic", side_effect=[100.0, 101.0, 102.0]),
            ):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("hello")

        self.assertEqual(result, "done")
        self.assertEqual(_TimeoutRecordingClient.timeouts, [8.0])

    def test_budget_stop_synthesizes_remaining_tool_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=0,
                budget_seconds=1,
                approval_mode="yolo",
            )
            with (
                patch("local_agent.agent.OpenAICompatibleClient", _TwoToolClient),
                patch(
                    "local_agent.agent.time.monotonic",
                    side_effect=[0.0, 0.1, 0.2, 0.3, 2.0],
                ),
            ):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("hello")

        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        self.assertEqual(result, "Stopped after reaching budget_seconds=1.")
        self.assertEqual([message["tool_call_id"] for message in tool_messages], ["call_1", "call_2"])
        self.assertIn("Tool call was not executed", tool_messages[1]["content"])

    def test_length_finish_reason_synthesizes_tool_results_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with (
                patch("local_agent.agent.OpenAICompatibleClient", _LengthToolClient),
                patch("local_agent.agent.create_default_registry", return_value=_UnexpectedRegistry()),
            ):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("hello")

        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        self.assertIn("finish_reason=length", result)
        self.assertEqual([message["tool_call_id"] for message in tool_messages], ["call_1", "call_2"])
        self.assertTrue(all("output token limit" in message["content"] for message in tool_messages))

    def test_repeated_identical_tool_calls_are_steered_to_final_answer(self) -> None:
        _RepeatingToolClient.calls = 0
        _RepeatingToolClient.tools_seen = []
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                context_char_budget=0,
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _RepeatingToolClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("repeat forever")

        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        steering_messages = [message for message in runtime._messages if "Runtime steering:" in str(message.get("content"))]
        duplicate_messages = [
            message for message in tool_messages if "identical call to 'unknown_repeat'" in message["content"]
        ]
        self.assertEqual(result, "final answer from collected evidence")
        self.assertGreaterEqual(len(duplicate_messages), 1)
        self.assertIn("already run 3 times", duplicate_messages[0]["content"])
        self.assertEqual(len(steering_messages), 1)
        self.assertEqual(_RepeatingToolClient.calls, 5)
        self.assertEqual(_RepeatingToolClient.tools_seen[-1], 0)

    def test_repeated_useless_search_pattern_is_steered_to_final_answer(self) -> None:
        _UselessSearchPatternClient.calls = 0
        _UselessSearchPatternClient.tools_seen = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            for index in range(10):
                directory = workspace / f"dir{index}"
                directory.mkdir()
                (directory / "sample.txt").write_text("unrelated content\n", encoding="utf-8")
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                context_char_budget=0,
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _UselessSearchPatternClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("只读分析这个项目并最后输出结论")

        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        skipped_messages = [
            message for message in tool_messages if "search_code has already returned no matches" in message["content"]
        ]
        steering_messages = [
            str(message.get("content"))
            for message in runtime._messages
            if "repeated search_code calls with the same no-match pattern" in str(message.get("content"))
        ]
        self.assertEqual(result, "final answer after empty search evidence")
        self.assertEqual(_UselessSearchPatternClient.tools_seen[-1], 0)
        self.assertEqual(len(skipped_messages), 1)
        self.assertGreaterEqual(len(steering_messages), 1)

    def test_repeated_useless_lsp_symbol_queries_are_steered_to_final_answer(self) -> None:
        _UselessLspSymbolClient.calls = 0
        _UselessLspSymbolClient.tools_seen = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "Sample.java").write_text("public class Sample {}\n", encoding="utf-8")
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                context_char_budget=0,
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _UselessLspSymbolClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("只读分析这个项目并最后输出结论")

        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        skipped_messages = [
            message for message in tool_messages if "lsp symbol queries have returned no matches" in message["content"]
        ]
        steering_messages = [
            str(message.get("content"))
            for message in runtime._messages
            if "repeated lsp symbol queries with no matches" in str(message.get("content"))
        ]
        self.assertEqual(result, "final answer after empty lsp evidence")
        self.assertEqual(_UselessLspSymbolClient.tools_seen[-1], 0)
        self.assertEqual(len(skipped_messages), 1)
        self.assertGreaterEqual(len(steering_messages), 1)

    def test_repeated_read_file_ranges_are_steered_to_final_answer(self) -> None:
        _RepeatedReadFileRangeClient.calls = 0
        _RepeatedReadFileRangeClient.tools_seen = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "large.py"
            target.write_text("\n".join(f"line {index}" for index in range(1, 80)), encoding="utf-8")
            _RepeatedReadFileRangeClient.file_path = "large.py"
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                context_char_budget=0,
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _RepeatedReadFileRangeClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("只读分析这个项目并最后输出结论")

        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        repeated_messages = [
            message for message in tool_messages if "read_file has already read" in message["content"]
        ]
        steering_messages = [
            message
            for message in runtime._messages
            if "repeated read_file slices from the same file" in str(message.get("content"))
        ]
        self.assertEqual(result, "final answer after enough file evidence")
        self.assertEqual(_RepeatedReadFileRangeClient.tools_seen[-1], 0)
        self.assertEqual(len(repeated_messages), 1)
        self.assertGreaterEqual(len(steering_messages), 1)

    def test_repeated_read_file_guard_does_not_force_final_for_edit_tasks(self) -> None:
        _RepeatedReadFileRangeClient.calls = 0
        _RepeatedReadFileRangeClient.tools_seen = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "large.py"
            target.write_text("\n".join(f"line {index}" for index in range(1, 80)), encoding="utf-8")
            _RepeatedReadFileRangeClient.file_path = "large.py"
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=10,
                budget_seconds=None,
                approval_mode="yolo",
                context_char_budget=0,
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _RepeatedReadFileRangeClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("修改这个大文件里的逻辑，不要写 memory")

        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        repeated_messages = [
            message for message in tool_messages if "read_file has already read" in message["content"]
        ]
        self.assertEqual(result, "Stopped after reaching max_steps=10.")
        self.assertEqual(repeated_messages, [])
        self.assertNotIn(0, _RepeatedReadFileRangeClient.tools_seen)

    def test_explicit_readonly_keeps_repeated_read_file_guard_with_implementation_wording(self) -> None:
        _RepeatedReadFileRangeClient.calls = 0
        _RepeatedReadFileRangeClient.tools_seen = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "large.py"
            target.write_text("\n".join(f"line {index}" for index in range(1, 80)), encoding="utf-8")
            _RepeatedReadFileRangeClient.file_path = "large.py"
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                context_char_budget=0,
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _RepeatedReadFileRangeClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("这是一次只读压测。如果下一步要实现，请列出建议文件。")

        steering_messages = [
            str(message.get("content"))
            for message in runtime._messages
            if "repeated read_file slices from the same file" in str(message.get("content"))
        ]
        self.assertEqual(result, "final answer after enough file evidence")
        self.assertEqual(_RepeatedReadFileRangeClient.tools_seen[-1], 0)
        self.assertTrue(any("Already read these files in this run" in message for message in steering_messages))
        self.assertTrue(any("Original user request to satisfy now" in message for message in steering_messages))
        self.assertTrue(any("do not claim they were unread" in message for message in steering_messages))
        self.assertTrue(any("large.py" in message for message in steering_messages))

    def test_keyboard_interrupt_synthesizes_remaining_tool_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with (
                patch("local_agent.agent.OpenAICompatibleClient", _TwoToolClient),
                patch("local_agent.agent.create_default_registry", return_value=_InterruptingRegistry()),
            ):
                runtime = AgentRuntime(config, show_tool_logs=False)
                with self.assertRaises(KeyboardInterrupt):
                    runtime.run("hello")
            records = [json.loads(line) for line in runtime._session.path.read_text(encoding="utf-8").splitlines()]

        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        self.assertEqual([message["tool_call_id"] for message in tool_messages], ["call_1", "call_2"])
        self.assertTrue(all("interrupted execution" in message["content"] for message in tool_messages))
        self.assertTrue(
            any(
                record.get("event") == "final"
                and record.get("payload", {}).get("content") == "Stopped after user interrupt."
                for record in records
            )
        )

    def test_context_compaction_injects_summary_and_open_todos(self) -> None:
        _MessageRecordingClient.messages = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                context_char_budget=1200,
                context_recent_messages=4,
                summary_mode="local",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _MessageRecordingClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                todo_path = workspace / ".local-agent" / "todos" / f"{runtime._session.session_id}.json"
                todo_path.parent.mkdir(parents=True)
                todo_path.write_text(
                    json.dumps(
                        [
                            {"id": "T1", "task": "Finish compaction", "status": "in_progress", "note": "keep this"},
                            {"id": "T2", "task": "Already done", "status": "done", "note": ""},
                        ]
                    ),
                    encoding="utf-8",
                )
                runtime._messages.extend(
                    [
                        {"role": "user", "content": "old request " + ("x" * 500)},
                        {"role": "assistant", "content": "old answer " + ("y" * 500)},
                        {"role": "user", "content": "recent request"},
                    ]
                )
                result = runtime.run("new request " + ("details " * 80) + "FINAL_MARKER_DO_NOT_DROP")

        sent = _MessageRecordingClient.messages
        sent_system_messages = [message for message in sent if message.get("role") == "system"]
        self.assertEqual(result, "done")
        self.assertEqual(len(sent_system_messages), 1)
        self.assertIn("You are a local coding agent", sent_system_messages[0].get("content", ""))
        self.assertIn("[Local context compaction]", sent_system_messages[0].get("content", ""))
        self.assertIn("Current user request:", sent_system_messages[0].get("content", ""))
        self.assertIn("FINAL_MARKER_DO_NOT_DROP", sent_system_messages[0].get("content", ""))
        self.assertTrue(any("Earlier conversation was compacted" in m.get("content", "") for m in sent))
        self.assertTrue(any("T1: Finish compaction" in m.get("content", "") for m in sent))
        self.assertFalse(any("T2: Already done" in m.get("content", "") for m in sent))

    def test_context_compaction_truncates_large_recent_tool_outputs_for_model_only(self) -> None:
        _MessageRecordingClient.messages = []
        large_tool_output = "tool-output-" + ("z" * 10000)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                context_char_budget=16000,
                context_recent_messages=4,
                summary_mode="local",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _MessageRecordingClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                runtime._messages.extend(
                    [
                        {"role": "user", "content": "old request " + ("x" * 3000)},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "shell", "arguments": "{}"},
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "call_1",
                            "content": large_tool_output,
                        },
                    ]
                )
                result = runtime.run("new request")

        sent_tool_messages = [message for message in _MessageRecordingClient.messages if message.get("role") == "tool"]
        stored_tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        self.assertEqual(result, "done")
        self.assertEqual(len(sent_tool_messages), 1)
        self.assertIn("...<truncated", sent_tool_messages[0]["content"])
        self.assertLess(len(sent_tool_messages[0]["content"]), len(large_tool_output))
        self.assertEqual(stored_tool_messages[0]["content"], large_tool_output)

    def test_context_pruning_elides_useless_and_superseded_tool_outputs_for_model_only(self) -> None:
        _MessageRecordingClient.messages = []
        read_arguments = json.dumps({"path": "README.md"}, sort_keys=True)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                context_char_budget=16000,
                context_recent_messages=7,
                summary_mode="local",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _MessageRecordingClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                runtime._messages.extend(
                    [
                        {"role": "user", "content": "old request " + ("x" * 20000)},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "read_old",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": read_arguments},
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "read_old",
                            "content": "old read body",
                            "_lca_tool_name": "read_file",
                            "_lca_is_error": False,
                        },
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "search_empty",
                                    "type": "function",
                                    "function": {"name": "search_code", "arguments": '{"pattern":"missing"}'},
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "search_empty",
                            "content": "No matches.",
                            "_lca_tool_name": "search_code",
                            "_lca_is_error": False,
                            "_lca_useless": True,
                        },
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "read_new",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": read_arguments},
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "read_new",
                            "content": "new read body",
                            "_lca_tool_name": "read_file",
                            "_lca_is_error": False,
                        },
                    ]
                )
                result = runtime.run("new request")

        sent_tool_messages = {
            message["tool_call_id"]: message for message in _MessageRecordingClient.messages if message.get("role") == "tool"
        }
        stored_tool_messages = {
            message["tool_call_id"]: message for message in runtime._messages if message.get("role") == "tool"
        }
        self.assertEqual(result, "done")
        self.assertIn("Superseded by a newer equivalent tool result", sent_tool_messages["read_old"]["content"])
        self.assertIn("Uneventful tool result elided", sent_tool_messages["search_empty"]["content"])
        self.assertEqual(sent_tool_messages["read_new"]["content"], "new read body")
        self.assertEqual(stored_tool_messages["read_old"]["content"], "old read body")
        self.assertEqual(stored_tool_messages["search_empty"]["content"], "No matches.")
        self.assertFalse(any(any(key.startswith("_lca_") for key in message) for message in _MessageRecordingClient.messages))

    def test_open_todos_are_injected_as_runtime_reminder_without_compaction(self) -> None:
        _MessageRecordingClient.messages = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                context_char_budget=0,
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _MessageRecordingClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                runtime._messages.extend(
                    [
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "search_empty",
                                    "type": "function",
                                    "function": {"name": "search_code", "arguments": '{"pattern":"missing"}'},
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "search_empty",
                            "content": "No matches.",
                            "_lca_tool_name": "search_code",
                            "_lca_is_error": False,
                            "_lca_useless": True,
                        },
                    ]
                )
                todo_path = workspace / ".local-agent" / "todos" / f"{runtime._session.session_id}.json"
                todo_path.parent.mkdir(parents=True)
                todo_path.write_text(
                    json.dumps([{"id": "T1", "task": "Keep direction", "status": "todo", "note": ""}]),
                    encoding="utf-8",
                )
                result = runtime.run("continue")

        system_messages = [message for message in _MessageRecordingClient.messages if message.get("role") == "system"]
        tool_messages = [message for message in _MessageRecordingClient.messages if message.get("role") == "tool"]
        self.assertEqual(result, "done")
        self.assertEqual(len(system_messages), 1)
        self.assertIn("[Runtime todo reminder]", system_messages[0]["content"])
        self.assertIn("T1: Keep direction", system_messages[0]["content"])
        self.assertIn("Uneventful tool result elided", tool_messages[0]["content"])

    def test_llm_summary_mode_summarizes_dropped_history_before_main_call(self) -> None:
        _SummaryThenFinalClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                context_char_budget=1200,
                context_recent_messages=1,
                summary_mode="llm",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _SummaryThenFinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                runtime._messages.extend(
                    [
                        {"role": "user", "content": "old request " + ("x" * 1000)},
                        {"role": "assistant", "content": "old answer " + ("y" * 1000)},
                    ]
                )
                result = runtime.run("update the code")

        self.assertEqual(result, "done")
        self.assertGreaterEqual(len(_SummaryThenFinalClient.calls), 2)
        self.assertEqual(_SummaryThenFinalClient.calls[0]["tools"], [])
        main_system = _SummaryThenFinalClient.calls[-1]["messages"][0]["content"]
        self.assertIn("summarized by the configured LLM", main_system)
        self.assertIn("LLM kept the important earlier facts", main_system)

    def test_auto_summary_mode_uses_llm_when_compaction_triggers(self) -> None:
        _SummaryThenFinalClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                context_char_budget=1200,
                context_recent_messages=1,
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _SummaryThenFinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                runtime._messages.extend(
                    [
                        {"role": "user", "content": "old request " + ("x" * 1000)},
                        {"role": "assistant", "content": "old answer " + ("y" * 1000)},
                    ]
                )
                result = runtime.run("update the code")

        self.assertEqual(result, "done")
        self.assertGreaterEqual(len(_SummaryThenFinalClient.calls), 2)
        self.assertEqual(_SummaryThenFinalClient.calls[0]["tools"], [])

    def test_memory_consolidation_auto_writes_extracted_markdown_memory(self) -> None:
        _MemoryConsolidationClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            state_dir = root / "state"
            workspace.mkdir()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                state_dir=state_dir,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                memory_consolidation="auto",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _MemoryConsolidationClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("记住这个经验：memory 代码改动后要跑 focused tests")

            conventions = state_dir / "memory" / "conventions.md"
            learned = state_dir / "memory" / "learned.md"
            project_memory_dir = workspace / ".local-agent" / "memory"
            records = [json.loads(line) for line in runtime._session.path.read_text(encoding="utf-8").splitlines()]
            conventions_exists = conventions.exists()
            learned_exists = learned.exists()
            project_memory_exists = project_memory_dir.exists()
            conventions_text = conventions.read_text(encoding="utf-8")
            learned_text = learned.read_text(encoding="utf-8")

        self.assertEqual(result, "Finished the task and learned a convention.")
        self.assertGreaterEqual(len(_MemoryConsolidationClient.calls), 2)
        self.assertNotEqual(_MemoryConsolidationClient.calls[0]["tools"], [])
        self.assertEqual(_MemoryConsolidationClient.calls[-1]["tools"], [])
        self.assertTrue(conventions_exists)
        self.assertTrue(learned_exists)
        self.assertFalse(project_memory_exists)
        self.assertIn("focused tests before the full test suite", conventions_text)
        self.assertIn("verify both focused agent tests and config tests", learned_text)
        self.assertTrue(
            any(
                record.get("event") == "memory_consolidation"
                and record.get("payload", {}).get("scope") == "state"
                and record.get("payload", {}).get("memory_root") == str(state_dir / "memory")
                for record in records
            )
        )

    def test_memory_consolidation_project_scope_writes_project_memory(self) -> None:
        _MemoryConsolidationClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            state_dir = root / "state"
            workspace.mkdir()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                state_dir=state_dir,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                memory_consolidation="auto",
                memory_scope="project",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _MemoryConsolidationClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("记住这个经验：memory 代码改动后要跑 focused tests")

            conventions = workspace / ".local-agent" / "memory" / "conventions.md"
            state_memory_dir = state_dir / "memory"
            records = [json.loads(line) for line in runtime._session.path.read_text(encoding="utf-8").splitlines()]
            conventions_exists = conventions.exists()
            state_memory_exists = state_memory_dir.exists()
            conventions_text = conventions.read_text(encoding="utf-8")

        self.assertEqual(result, "Finished the task and learned a convention.")
        self.assertTrue(conventions_exists)
        self.assertFalse(state_memory_exists)
        self.assertIn("focused tests before the full test suite", conventions_text)
        self.assertTrue(
            any(
                record.get("event") == "memory_consolidation"
                and record.get("payload", {}).get("scope") == "project"
                and record.get("payload", {}).get("memory_root") == str(workspace / ".local-agent" / "memory")
                for record in records
            )
        )

    def test_memory_consolidation_default_off_does_not_call_llm_or_write_memory(self) -> None:
        _MemoryConsolidationClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            state_dir = root / "state"
            workspace.mkdir()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                state_dir=state_dir,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _MemoryConsolidationClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("记住这个经验：默认关闭时也不要隐式写 memory")

        self.assertEqual(result, "Finished the task and learned a convention.")
        self.assertEqual(len(_MemoryConsolidationClient.calls), 1)
        self.assertFalse((workspace / ".local-agent" / "memory").exists())
        self.assertFalse((state_dir / "memory").exists())

    def test_memory_consolidation_invalid_json_does_not_write_memory(self) -> None:
        _InvalidMemoryConsolidationClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "workspace"
            state_dir = root / "state"
            workspace.mkdir()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                state_dir=state_dir,
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
                memory_consolidation="llm",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _InvalidMemoryConsolidationClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("update the code")

            records = [json.loads(line) for line in runtime._session.path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(result, "Finished the task.")
        self.assertFalse((workspace / ".local-agent" / "memory").exists())
        self.assertFalse((state_dir / "memory").exists())
        self.assertTrue(any(record.get("event") == "memory_consolidation_error" for record in records))

    def test_compaction_threshold_reserves_at_least_fifteen_percent(self) -> None:
        self.assertEqual(_resolve_compaction_threshold_chars(1200), 1020)
        self.assertEqual(_resolve_compaction_threshold_chars(100000), 34464)

    def test_workflow_nudge_is_added_for_coding_tasks(self) -> None:
        _MessageRecordingClient.messages = []
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _MessageRecordingClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("修改 README 里的说明")

        self.assertEqual(result, "done")
        user_messages = [message for message in _MessageRecordingClient.messages if message.get("role") == "user"]
        self.assertIn("[Runtime workflow reminder]", user_messages[-1]["content"])

    def test_workflow_nudge_is_not_added_for_short_non_coding_prompt(self) -> None:
        _MessageRecordingClient.messages = []
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _MessageRecordingClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("只回答 OK")

        self.assertEqual(result, "done")
        user_messages = [message for message in _MessageRecordingClient.messages if message.get("role") == "user"]
        self.assertNotIn("[Runtime workflow reminder]", user_messages[-1]["content"])

    def test_session_tool_policy_rejects_unknown_tool_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=0,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _FinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)

        with self.assertRaisesRegex(ValueError, "unknown tool: run_test"):
            runtime.set_session_tool_policy("run_test", "allow")


if __name__ == "__main__":
    unittest.main()

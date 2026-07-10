from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.agent import AgentRuntime
from local_agent.compaction import resolve_compaction_threshold_chars
from local_agent.compaction import resolve_compaction_threshold_tokens
from local_agent.config import AgentConfig
from local_agent.protocol.events import ListEventSink
from local_agent.steering.final_answer import SourceEvidence
from local_agent.task_contract import generate_requirement_contract
from local_agent.tool_choice_queue import ToolResultSummary
from local_agent.tools.base import ToolResult


def _tool_names_from_schema_call(tools: list[dict]) -> set[str]:
    return {str(schema.get("function", {}).get("name") or "") for schema in tools}


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


class _ToolSchemaRecordingClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        return type("Response", (), {"message": {"content": "done"}})()


class _WriteFileThenFinalClient:
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
                                "id": "call_write",
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps({"path": "generated.txt", "content": "hello\n"}),
                                },
                            }
                        ],
                    }
                },
            )()
        return type("Response", (), {"message": {"content": "done"}})()


class _FinalStructureThenTableClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if len(type(self).calls) == 1:
            return type("Response", (), {"message": {"content": "Ready to output final scope analysis table."}})()
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": (
                        "| 分类 | 服务 |\n"
                        "|---|---|\n"
                        "| 必须关注 | zqyl-charge |\n"
                        "| 可能关注 | zqyl-file |\n"
                        "| 暂不关注 | zqyl-nlp |\n"
                        "| 需要用户确认 | download-center |\n"
                    )
                }
            },
        )()


class _FinalProjectScopeTableClient:
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
                        "content": (
                            "| 分类 | 表名 |\n"
                            "|---|---|\n"
                            "| 必须关注 | intention_config |\n"
                            "| 可能关注 | user_extend |\n"
                            "| 暂不关注 | audit_log |\n"
                        )
                    }
                },
            )()
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": (
                        "| 分类 | 项目/服务 | 证据状态 | 依据 |\n"
                        "|---|---|---|---|\n"
                        "| 必须关注 | zqyl-plan | 已验证 | SQL 和 Controller 证据 |\n"
                        "| 可能关注 | user-center | 推断 | 需要用户确认 |\n"
                        "| 暂不关注 | zqyl-nlp | 已验证 | 未命中需求关键词 |\n"
                    )
                }
            },
        )()


class _FinalEvidenceStatusClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if len(type(self).calls) == 1:
            return type(
                "Response",
                (),
                {"message": {"content": "IntentionConfigApplication 可能是 Spring Boot 启动配置类。"}},
            )()
        return type(
            "Response",
            (),
            {"message": {"content": "已验证：文件被读取并包含 IntentionConfigApplication。推断：其具体运行角色仍需继续确认。"}},
        )()


class _ReadOnlyEvidenceGateClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if len(type(self).calls) == 1:
            return type("Response", (), {"message": {"content": "可能采用 HTTPS 明文传输，后端再做哈希。"}})()
        if len(type(self).calls) == 2:
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
                                    "arguments": json.dumps({"path": "src/LoginController.java"}),
                                },
                            }
                        ],
                    }
                },
            )()
        return type(
            "Response",
            (),
            {"message": {"content": "已验证：src/LoginController.java 读取 password，并调用 PasswordUtil.check。"}},
        )()


class _CompletionAuditReadOnlyClient:
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
                                    "arguments": json.dumps({"path": "src/LoginController.java"}),
                                },
                            }
                        ],
                    }
                },
            )()
        if len(type(self).calls) == 2:
            return type("Response", (), {"message": {"content": "密码在后端校验。"}})()
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": (
                        "已验证：src/LoginController.java 调用 PasswordUtil.check 校验 password。"
                        "推断：前端加密方式未在已读代码中确认。"
                    )
                }
            },
        )()


class _ReadOnlyEvidenceNoMatchClient:
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
                                "id": "call_search",
                                "type": "function",
                                "function": {
                                    "name": "search_code",
                                    "arguments": json.dumps({"pattern": "MissingPasswordEncryptor"}),
                                },
                            }
                        ],
                    }
                },
            )()
        return type("Response", (), {"message": {"content": "未找到代码证据：search_code 没有匹配 MissingPasswordEncryptor。"}})()


class _InvalidToolCallThenFinalClient:
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
                                "id": "call_empty_name",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        ],
                    }
                },
            )()
        return type("Response", (), {"message": {"content": "recovered"}})()


class _MalformedToolArgumentsThenFinalClient:
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
                                "id": "call_bad_args",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "README.md"}}',
                                },
                            }
                        ],
                    }
                },
            )()
        return type("Response", (), {"message": {"content": "recovered from malformed args"}})()


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


class _ReadEnumThenWrongNumericClient:
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
                                "id": "call_read_enum",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": "src/PreOrderStatusEnum.java"}),
                                },
                            }
                        ],
                    }
                },
            )()
        if len(type(self).calls) == 2:
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": (
                            "### PreOrderStatusEnum\n"
                            "- MAKING(1, \"制单中\")\n"
                            "- MADE(3, \"已制单\")\n"
                            "- CANCEL(5, \"已取消\")\n"
                            "证据文件：src/PreOrderStatusEnum.java"
                        )
                    }
                },
            )()
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": (
                        "已验证：src/PreOrderStatusEnum.java 中 "
                        "PreOrderStatusEnum.MAKING = 2，"
                        "PreOrderStatusEnum.MADE = 3，PreOrderStatusEnum.CANCEL = 4。"
                    )
                }
            },
        )()


class _ReadEnumThenFalseNegativeClient:
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
                                "id": "call_read_enum",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": "src/PreOrderStatusEnum.java"}),
                                },
                            }
                        ],
                    }
                },
            )()
        if len(type(self).calls) == 2:
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": (
                            "PreOrderStatusEnum 已读取但源码不完整；MAKING、MADE、CANCEL 未找到，"
                            "需补充读取后续内容。"
                        )
                    }
                },
            )()
        return type(
            "Response",
            (),
            {
                "message": {
                    "content": (
                        "已验证：src/PreOrderStatusEnum.java 中 PreOrderStatusEnum.MAKING = 2（待制单），"
                        "MADE = 3（已制单），CANCEL = 4（已作废）。"
                    )
                }
            },
        )()


class _ReadSkillThenFinalClient:
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
                                "id": "call_skill",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps(
                                        {"path": ".local-agent/skills/project-scope-analysis/SKILL.md"}
                                    ),
                                },
                            }
                        ],
                    }
                },
            )()
        return type("Response", (), {"message": {"content": "skill applied"}})()


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


class _NoEditThenHygieneClient:
    calls: list[dict] = []

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if len(type(self).calls) == 1:
            return type(
                "Response",
                (),
                {"message": {"content": "无法安全实现：当前仓库不包含目标服务，未修改文件。"}},
            )()
        if len(type(self).calls) == 2:
            return type(
                "Response",
                (),
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_todo",
                                "type": "function",
                                "function": {
                                    "name": "todo_add",
                                    "arguments": json.dumps(
                                        {
                                            "id": "T075",
                                            "task": "Stop because target service is missing",
                                            "status": "blocked",
                                            "note": "No safe local edit.",
                                        }
                                    ),
                                },
                            },
                            {
                                "id": "call_git_status",
                                "type": "function",
                                "function": {"name": "git_status", "arguments": "{}"},
                            },
                        ],
                    }
                },
            )()
        return type("Response", (), {"message": {"content": "已收束：未修改文件，git_status 已检查，todo 已记录。"}})()


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


class _SemanticListFilesClient:
    calls = 0
    tool_names_seen: list[set[str]] = []
    paths = (
        "service-a/src/main/java/com/example/login",
        "service-a/src/main/java/com/example/auth",
        "service-a/src/main/java/com/example/account",
        "service-a/src/main/java/com/example/password",
        "service-a/src/main/java/com/example/session",
    )

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls += 1
        tool_names = {schema.get("function", {}).get("name") for schema in tools}
        type(self).tool_names_seen.append(tool_names)
        if "list_files" not in tool_names:
            return type("Response", (), {"message": {"content": "final answer after semantic exploration guard"}})()
        path = type(self).paths[(type(self).calls - 1) % len(type(self).paths)]
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
                                "name": "list_files",
                                "arguments": json.dumps({"path": path}),
                            },
                        }
                    ],
                }
            },
        )()


class _SemanticPathNotFoundClient:
    calls = 0
    tool_names_seen: list[set[str]] = []
    paths = (
        "missing-service/interfaces/controller",
        "missing-service/interfaces/service",
        "missing-service/interfaces/repository",
        "missing-service/interfaces/model",
        "missing-service/interfaces/util",
    )

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls += 1
        tool_names = {schema.get("function", {}).get("name") for schema in tools}
        type(self).tool_names_seen.append(tool_names)
        if "list_files" not in tool_names:
            return type("Response", (), {"message": {"content": "final answer after path guessing guard"}})()
        path = type(self).paths[(type(self).calls - 1) % len(type(self).paths)]
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
                                "name": "list_files",
                                "arguments": json.dumps({"path": path}),
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


class _RepeatedReadFileSameRangeClient:
    calls = 0
    tools_seen: list[int] = []
    file_path: str = ""

    def __init__(self, config: AgentConfig):
        self.config = config

    def chat(self, messages, tools, *, timeout=None):
        type(self).calls += 1
        type(self).tools_seen.append(len(tools))
        if not tools:
            return type(
                "Response",
                (),
                {"message": {"content": "已验证：service.java 已读取，可作为 repeated same file evidence。推断：无需继续重复读取。"}},
            )()
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
                                "arguments": json.dumps({"path": type(self).file_path}),
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
    def test_requirement_contract_is_injected_into_runtime_context(self) -> None:
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
                runtime.run("只读代码，请给出源码证据说明登录密码在哪里校验。")

        system_content = "\n".join(
            str(message.get("content") or "")
            for message in _MessageRecordingClient.messages
            if message.get("role") == "system"
        )
        self.assertIn("[Requirement contract]", system_content)
        self.assertIn("Task kind: read-only", system_content)
        self.assertIn("repository-grounded evidence", system_content)

    def test_planner_explore_context_is_sent_for_implementation_tasks(self) -> None:
        _MessageRecordingClient.messages = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=1,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _MessageRecordingClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                runtime.run("请实现用户注册接口邮箱唯一性校验，并补充测试。")

        system_content = "\n".join(
            str(message.get("content") or "")
            for message in _MessageRecordingClient.messages
            if message.get("role") == "system"
        )
        self.assertIn("[Planner / Explore]", system_content)
        self.assertIn("Current phase: explore", system_content)
        self.assertIn("do not write files yet", system_content)

    def test_tool_choice_queue_restricts_read_only_evidence_task_to_evidence_tools(self) -> None:
        _ToolSchemaRecordingClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=1,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _ToolSchemaRecordingClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                runtime.run("不要推测，请给出代码证据说明登录密码在哪里校验。不要修改文件。")

        first_tools = _tool_names_from_schema_call(_ToolSchemaRecordingClient.calls[0]["tools"])
        self.assertIn("read_file", first_tools)
        self.assertIn("search_code", first_tools)
        self.assertIn("lsp_symbols", first_tools)
        self.assertNotIn("apply_patch", first_tools)
        self.assertNotIn("run_tests", first_tools)
        self.assertNotIn("shell", first_tools)

    def test_tool_choice_queue_restricts_implementation_to_explore_tools_before_evidence(self) -> None:
        _ToolSchemaRecordingClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=1,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _ToolSchemaRecordingClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                runtime.run("请实现用户注册接口邮箱唯一性校验，并补充测试。")

        first_tools = _tool_names_from_schema_call(_ToolSchemaRecordingClient.calls[0]["tools"])
        self.assertIn("read_file", first_tools)
        self.assertIn("search_code", first_tools)
        self.assertIn("lsp_symbols", first_tools)
        self.assertIn("todo_add", first_tools)
        self.assertNotIn("apply_patch", first_tools)
        self.assertNotIn("write_file", first_tools)
        self.assertNotIn("run_tests", first_tools)

    def test_tool_choice_queue_requires_tests_or_diff_after_workspace_write(self) -> None:
        _WriteFileThenFinalClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=Path(tmp).resolve(),
                max_steps=2,
                budget_seconds=None,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _WriteFileThenFinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                runtime.run("请实现一个小功能，创建 generated.txt，并补充测试。")

        self.assertGreaterEqual(len(_WriteFileThenFinalClient.calls), 2)
        second_tools = _tool_names_from_schema_call(_WriteFileThenFinalClient.calls[1]["tools"])
        self.assertEqual(second_tools, {"git_diff", "run_tests"})
        second_messages = _WriteFileThenFinalClient.calls[1]["messages"]
        self.assertTrue(
            any(
                message.get("role") == "user"
                and "[Runtime tool choice queue]" in str(message.get("content") or "")
                and "implementation_final_hygiene" in str(message.get("content") or "")
                for message in second_messages
            )
        )

    def test_runtime_emits_protocol_events_and_records_event_v1(self) -> None:
        _ReadFileThenFinalClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "README.md").write_text("# Demo\n\nhello\n", encoding="utf-8")
            state_dir = workspace / "state"
            sink = ListEventSink()
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
            with patch("local_agent.agent.OpenAICompatibleClient", _ReadFileThenFinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False, event_sink=sink)
                result = runtime.run("先读取 README，再回答")

            records = [json.loads(line) for line in runtime._session.path.read_text(encoding="utf-8").splitlines()]

        event_types = [event.type for event in sink.events]
        self.assertEqual(result, "done with evidence")
        self.assertIn("SessionStarted", event_types)
        self.assertIn("UserMessage", event_types)
        self.assertIn("LlmRequest", event_types)
        self.assertIn("AssistantMessage", event_types)
        self.assertIn("ToolStarted", event_types)
        self.assertIn("ToolOutput", event_types)
        self.assertIn("ToolFinished", event_types)
        self.assertIn("RunSummary", event_types)
        self.assertIn("SessionFinished", event_types)
        self.assertTrue(all(event.session_id == runtime._session.session_id for event in sink.events))
        self.assertTrue(any(event.run_id for event in sink.events if event.type != "SessionStarted"))
        run_summary_events = [event for event in sink.events if event.type == "RunSummary"]
        self.assertEqual(len(run_summary_events), 1)
        self.assertEqual(run_summary_events[0].payload["termination_reason"], "final")
        self.assertEqual(run_summary_events[0].payload["llm_requests"], 2)
        self.assertEqual(run_summary_events[0].payload["tool_calls"], 1)
        self.assertEqual(run_summary_events[0].payload["tool_counts"], {"read_file": 1})
        status = runtime.status_summary()
        self.assertIn("- last_run:", status)
        self.assertIn("read_file=1", status)
        session_finished = [event for event in sink.events if event.type == "SessionFinished"][-1]
        self.assertEqual(session_finished.payload["run_summary"]["termination_reason"], "final")
        self.assertTrue(
            any(
                record.get("event") == "event_v1"
                and record.get("payload", {}).get("type") == "ToolStarted"
                and record.get("payload", {}).get("payload", {}).get("name") == "read_file"
                for record in records
            )
        )
        self.assertTrue(
            any(
                record.get("event") == "run_summary"
                and record.get("payload", {}).get("tool_counts") == {"read_file": 1}
                for record in records
            )
        )

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

    def test_runtime_status_and_tool_summary_are_available_for_terminal_frontend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            config = AgentConfig(
                provider="openai-compatible",
                api_base_url="https://example.invalid/v1",
                api_key="token",
                model="model",
                workspace=workspace,
                max_steps=0,
                budget_seconds=600,
                approval_mode="yolo",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _FinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)

        status = runtime.status_summary()
        tools = runtime.tool_summary()
        self.assertIn("Runtime status:", status)
        self.assertIn(f"- workspace: {workspace}", status)
        self.assertIn("- provider: openai-compatible", status)
        self.assertIn("- model: model", status)
        self.assertIn("- approval_mode: yolo", status)
        self.assertIn("- last_run: none", status)
        self.assertIn("Available tools:", tools)
        self.assertIn("- read_file", tools)
        self.assertIn("- apply_patch", tools)

    def test_no_edit_final_hygiene_context_is_sent_for_implementation_tasks(self) -> None:
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
                result = runtime.run("实现 Java 导入校验需求，必须维护 todo")

        sent_system = [message for message in _MessageRecordingClient.messages if message.get("role") == "system"][0]
        self.assertEqual(result, "done")
        self.assertIn("[No-edit final hygiene]", sent_system["content"])
        self.assertIn("git_status or git_diff", sent_system["content"])
        self.assertIn("update/read todo state", sent_system["content"])
        self.assertNotIn("[No-edit final hygiene]", runtime._messages[0]["content"])

    def test_analysis_scope_task_does_not_receive_coding_nudge_or_no_edit_hygiene(self) -> None:
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
                result = runtime.run("仅根据需求和服务边界判断需要关注哪些项目，禁止扫描源码，最终用表格输出")

        sent_system = [message for message in _MessageRecordingClient.messages if message.get("role") == "system"][0]
        user_messages = [message for message in runtime._messages if message.get("role") == "user"]
        self.assertEqual(result, "done")
        self.assertNotIn("[No-edit final hygiene]", sent_system["content"])
        self.assertFalse(any("[Runtime workflow reminder]" in str(message.get("content")) for message in user_messages))

    def test_just_based_on_code_fix_task_still_receives_coding_nudge(self) -> None:
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
                result = runtime.run("仅根据当前源码修复 README 文档里的一个小问题")

        sent_system = [message for message in _MessageRecordingClient.messages if message.get("role") == "system"][0]
        user_messages = [message for message in runtime._messages if message.get("role") == "user"]
        self.assertEqual(result, "done")
        self.assertIn("[No-edit final hygiene]", sent_system["content"])
        self.assertTrue(any("[Runtime workflow reminder]" in str(message.get("content")) for message in user_messages))

    def test_no_edit_final_is_steered_to_todo_and_git_hygiene(self) -> None:
        _NoEditThenHygieneClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            subprocess.run(["git", "init"], cwd=workspace, text=True, capture_output=True, check=True)
            state_dir = workspace / "state"
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
            with patch("local_agent.agent.OpenAICompatibleClient", _NoEditThenHygieneClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("实现 Java 导入校验需求，必须维护 todo")
                records = [
                    json.loads(line)
                    for line in runtime._session.path.read_text(encoding="utf-8").splitlines()
                ]
                todo_path = state_dir / "todos" / f"{runtime._session.session_id}.json"
                todo_path_exists = todo_path.exists()

        second_call_tools = {
            schema["function"]["name"] for schema in _NoEditThenHygieneClient.calls[1]["tools"]
        }
        self.assertEqual(result, "已收束：未修改文件，git_status 已检查，todo 已记录。")
        self.assertEqual(
            second_call_tools,
            {"todo_read", "todo_add", "todo_update", "git_status", "git_diff"},
        )
        self.assertTrue(todo_path_exists)
        self.assertTrue(
            any(
                record.get("event") == "runtime_steering"
                and record.get("payload", {}).get("kind") == "no_edit_final_hygiene"
                for record in records
            )
        )
        self.assertTrue(
            any(
                record.get("event") == "tool_result"
                and record.get("payload", {}).get("name") == "git_status"
                for record in records
            )
        )
        self.assertTrue(
            any(
                record.get("event") == "tool_result"
                and record.get("payload", {}).get("name") == "todo_add"
                for record in records
            )
        )

    def test_final_structure_gate_forces_requested_table_without_tools(self) -> None:
        _FinalStructureThenTableClient.calls = []
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
            with patch("local_agent.agent.OpenAICompatibleClient", _FinalStructureThenTableClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run(
                    "最终回答必须用 Markdown 表格输出，包含：必须关注、可能关注、暂不关注、需要用户确认"
                )

        self.assertIn("| 必须关注 | zqyl-charge |", result)
        self.assertEqual(len(_FinalStructureThenTableClient.calls), 2)
        self.assertEqual(_FinalStructureThenTableClient.calls[1]["tools"], [])

    def test_final_structure_gate_requires_project_scope_column(self) -> None:
        _FinalProjectScopeTableClient.calls = []
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
            with patch("local_agent.agent.OpenAICompatibleClient", _FinalProjectScopeTableClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run(
                    "最终必须输出项目范围表，包含：必须关注、可能关注、暂不关注项目，并标注证据状态。"
                )
                records = [json.loads(line) for line in runtime._session.path.read_text(encoding="utf-8").splitlines()]

        steering_records = [
            record
            for record in records
            if record.get("event") == "runtime_steering"
            and record.get("payload", {}).get("kind") == "final_structure"
        ]
        self.assertIn("| 分类 | 项目/服务 | 证据状态 | 依据 |", result)
        self.assertEqual(len(_FinalProjectScopeTableClient.calls), 2)
        self.assertEqual(_FinalProjectScopeTableClient.calls[1]["tools"], [])
        self.assertIn("missing_project_or_service_table_column", steering_records[0]["payload"]["issues"])

    def test_final_structure_gate_requires_evidence_status_labels(self) -> None:
        _FinalEvidenceStatusClient.calls = []
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
            with patch("local_agent.agent.OpenAICompatibleClient", _FinalEvidenceStatusClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("请把每条结论标为已验证或推断。")
                records = [json.loads(line) for line in runtime._session.path.read_text(encoding="utf-8").splitlines()]

        steering_records = [
            record
            for record in records
            if record.get("event") == "runtime_steering"
            and record.get("payload", {}).get("kind") == "final_structure"
        ]
        self.assertIn("已验证：", result)
        self.assertIn("推断：", result)
        self.assertEqual(len(_FinalEvidenceStatusClient.calls), 2)
        self.assertEqual(_FinalEvidenceStatusClient.calls[1]["tools"], [])
        self.assertIn("missing_evidence_status_labels", steering_records[0]["payload"]["issues"])

    def test_read_only_evidence_gate_requires_file_evidence_before_final(self) -> None:
        _ReadOnlyEvidenceGateClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source_dir = workspace / "src"
            source_dir.mkdir()
            (source_dir / "LoginController.java").write_text(
                "class LoginController { boolean login(String password) { return PasswordUtil.check(password); } }\n",
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
            with patch("local_agent.agent.OpenAICompatibleClient", _ReadOnlyEvidenceGateClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("我们是怎么解决前端密码加密问题的？后端怎么处理的")
                records = [json.loads(line) for line in runtime._session.path.read_text(encoding="utf-8").splitlines()]

        second_tool_names = {
            schema.get("function", {}).get("name")
            for schema in _ReadOnlyEvidenceGateClient.calls[1]["tools"]
        }
        steering_records = [
            record
            for record in records
            if record.get("event") == "runtime_steering"
            and record.get("payload", {}).get("kind") == "read_only_evidence"
        ]
        self.assertIn("已验证：src/LoginController.java", result)
        self.assertEqual(len(_ReadOnlyEvidenceGateClient.calls), 3)
        self.assertIn("read_file", second_tool_names)
        self.assertIn("search_code", second_tool_names)
        self.assertNotIn("apply_patch", second_tool_names)
        self.assertEqual(len(steering_records), 1)
        self.assertEqual(runtime._last_run_summary["steering_counts"], {"read_only_evidence": 1})

    def test_completion_audit_rewrites_read_only_answer_without_evidence_status(self) -> None:
        _CompletionAuditReadOnlyClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source_dir = workspace / "src"
            source_dir.mkdir()
            (source_dir / "LoginController.java").write_text(
                "class LoginController { boolean login(String password) { return PasswordUtil.check(password); } }\n",
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
            with patch("local_agent.agent.OpenAICompatibleClient", _CompletionAuditReadOnlyClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("只读代码，请根据源码证据说明登录密码在哪里校验。不要修改文件。")
                records = [json.loads(line) for line in runtime._session.path.read_text(encoding="utf-8").splitlines()]

        steering_records = [
            record
            for record in records
            if record.get("event") == "runtime_steering"
            and record.get("payload", {}).get("kind") == "completion_audit"
        ]
        self.assertIn("已验证：src/LoginController.java", result)
        self.assertIn("推断：", result)
        self.assertEqual(len(_CompletionAuditReadOnlyClient.calls), 3)
        self.assertEqual(_CompletionAuditReadOnlyClient.calls[2]["tools"], [])
        self.assertEqual(len(steering_records), 1)
        self.assertGreaterEqual(steering_records[0]["payload"]["missing_count"], 1)
        self.assertEqual(runtime._last_run_summary["steering_counts"], {"completion_audit": 1})

    def test_patch_reviewer_steers_after_diff_when_requested_test_is_missing(self) -> None:
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
            runtime = AgentRuntime(config, show_tool_logs=False)
            runtime._current_user_request = "请实现用户名规范化，并补充单元测试。"
            runtime._requirement_contract = generate_requirement_contract(runtime._current_user_request)
            runtime._tool_choice_results = [
                ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
                ToolResultSummary("apply_patch", "Applied patch", path="src/UserService.java", changed=True),
                ToolResultSummary(
                    "git_diff",
                    (
                        "diff --git a/src/UserService.java b/src/UserService.java\n"
                        "--- a/src/UserService.java\n"
                        "+++ b/src/UserService.java\n"
                        "@@ -1 +1 @@\n"
                        "-    private String normalize(String value) { return value; }\n"
                        "+    private String normalize(String value) { return value.trim(); }\n"
                        "\n[diff summary]\n- Total: 1 file(s), +1 -1, 1 hunk(s).\n"
                    ),
                ),
            ]

            decision = runtime._decide_final_answer_steering("已完成用户名规范化。", 0)

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.kind, "patch_reviewer")
        self.assertIn("requested_test_missing", str(decision.payload))
        self.assertFalse(decision.force_final_answer_without_tools)
        self.assertIn("apply_patch", decision.temporary_tool_allowlist or set())
        self.assertIn("run_tests", decision.temporary_tool_allowlist or set())

    def test_post_diff_patch_reviewer_steers_before_the_model_attempts_a_final_answer(self) -> None:
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
            runtime = AgentRuntime(config, show_tool_logs=False)
            runtime._current_user_request = "请修复用户名规范化，并补充单元测试。"
            runtime._requirement_contract = generate_requirement_contract(runtime._current_user_request)
            runtime._tool_choice_results = [
                ToolResultSummary("read_file", "class UserService {}", path="src/UserService.java"),
                ToolResultSummary("apply_patch", "Applied patch", path="src/UserService.java", changed=True),
                ToolResultSummary(
                    "git_diff",
                    (
                        "diff --git a/src/UserService.java b/src/UserService.java\n"
                        "--- a/src/UserService.java\n"
                        "+++ b/src/UserService.java\n"
                        "@@ -1 +1 @@\n"
                        "-    private String normalize(String value) { return value; }\n"
                        "+    private String normalize(String value) { return value.trim(); }\n"
                        "\n[diff summary]\n- Total: 1 file(s), +1 -1, 1 hunk(s).\n"
                    ),
                ),
            ]

            decision = runtime._decide_post_diff_patch_review(0)

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.kind, "patch_reviewer")
        self.assertIn("requested_test_missing", str(decision.payload))
        self.assertIn("apply_patch", decision.temporary_tool_allowlist or set())

    def test_final_answer_steer_counts_reset_at_the_start_of_each_run(self) -> None:
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
                runtime._completion_audit_steers = 2
                runtime._patch_reviewer_steers = 2
                runtime.run("简单回答 OK")

        self.assertEqual(runtime._completion_audit_steers, 0)
        self.assertEqual(runtime._patch_reviewer_steers, 0)

    def test_read_only_evidence_gate_allows_negative_search_evidence(self) -> None:
        _ReadOnlyEvidenceNoMatchClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "README.md").write_text("# Demo\n", encoding="utf-8")
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
            with patch("local_agent.agent.OpenAICompatibleClient", _ReadOnlyEvidenceNoMatchClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("请根据代码证据回答 MissingPasswordEncryptor 是怎么处理密码的")

        self.assertIn("未找到代码证据", result)
        self.assertEqual(len(_ReadOnlyEvidenceNoMatchClient.calls), 2)
        self.assertEqual(runtime._last_run_summary["steering_counts"], {})

    def test_source_grounded_numeric_gate_rewrites_status_values_from_read_source(self) -> None:
        _ReadEnumThenWrongNumericClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source_dir = workspace / "src"
            source_dir.mkdir()
            (source_dir / "PreOrderStatusEnum.java").write_text(
                "\n".join(
                    [
                        "public enum PreOrderStatusEnum {",
                        '    MAKING(2, "待制单"),',
                        '    MADE(3, "已制单"),',
                        '    CANCEL(4, "已作废");',
                        "}",
                    ]
                ),
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
            with patch("local_agent.agent.OpenAICompatibleClient", _ReadEnumThenWrongNumericClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("请根据代码证据说明 PreOrderStatusEnum 的状态码")
                records = [
                    json.loads(line)
                    for line in runtime._session.path.read_text(encoding="utf-8").splitlines()
                ]

        steering_records = [
            record
            for record in records
            if record.get("event") == "runtime_steering"
            and record.get("payload", {}).get("kind") == "source_grounded_numeric"
        ]
        self.assertIn("MAKING = 2", result)
        self.assertIn("MADE = 3", result)
        self.assertIn("CANCEL = 4", result)
        self.assertNotIn("MAKING(1", result)
        self.assertNotIn("CANCEL(5", result)
        self.assertEqual(len(_ReadEnumThenWrongNumericClient.calls), 3)
        self.assertEqual(_ReadEnumThenWrongNumericClient.calls[2]["tools"], [])
        self.assertEqual(len(steering_records), 1)
        self.assertEqual(runtime._last_run_summary["steering_counts"], {"source_grounded_numeric": 1})

    def test_source_evidence_false_negative_gate_rewrites_incomplete_read_claim(self) -> None:
        _ReadEnumThenFalseNegativeClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            source_dir = workspace / "src"
            source_dir.mkdir()
            (source_dir / "PreOrderStatusEnum.java").write_text(
                "\n".join(
                    [
                        "public enum PreOrderStatusEnum {",
                        '    MAKING(2, "待制单"),',
                        '    MADE(3, "已制单"),',
                        '    CANCEL(4, "已作废");',
                        "}",
                    ]
                ),
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
            with patch("local_agent.agent.OpenAICompatibleClient", _ReadEnumThenFalseNegativeClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run(
                    "请根据代码证据说明 PreOrderStatusEnum 的 MAKING、MADE、CANCEL 状态"
                )
                records = [
                    json.loads(line)
                    for line in runtime._session.path.read_text(encoding="utf-8").splitlines()
                ]

        steering_records = [
            record
            for record in records
            if record.get("event") == "runtime_steering"
            and record.get("payload", {}).get("kind") == "source_evidence_false_negative"
        ]
        self.assertIn("MAKING = 2", result)
        self.assertIn("CANCEL = 4", result)
        self.assertNotIn("未找到", result)
        self.assertEqual(len(_ReadEnumThenFalseNegativeClient.calls), 3)
        self.assertEqual(_ReadEnumThenFalseNegativeClient.calls[2]["tools"], [])
        self.assertEqual(len(steering_records), 1)
        self.assertEqual(
            runtime._last_run_summary["steering_counts"],
            {"source_evidence_false_negative": 1},
        )

    def test_successful_write_invalidates_only_stale_source_snapshot_for_that_path(self) -> None:
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
            runtime = AgentRuntime(config, show_tool_logs=False)
            runtime._read_file_evidence_paths = ["src/UserService.java", "src/Other.java"]
            runtime._source_evidence = [
                SourceEvidence("src/UserService.java", "old implementation"),
                SourceEvidence("src/Other.java", "stable implementation"),
            ]

            runtime._invalidate_stale_source_evidence_after_write(
                "apply_patch",
                {"path": "src/UserService.java"},
                ToolResult("Applied patch"),
            )

        self.assertEqual(runtime._read_file_evidence_paths, ["src/UserService.java", "src/Other.java"])
        self.assertEqual(runtime._source_evidence, [SourceEvidence("src/Other.java", "stable implementation")])

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

    def test_workspace_root_evidence_is_sent_on_first_model_request(self) -> None:
        _MessageRecordingClient.messages = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "src" / "main" / "java").mkdir(parents=True)
            (workspace / "pom.xml").write_text("<project />\n", encoding="utf-8")
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
                result = runtime.run("实现 Java 导入校验需求")
                evidence_events = [
                    json.loads(line)
                    for line in runtime._session.path.read_text(encoding="utf-8").splitlines()
                    if json.loads(line).get("event") == "evidence"
                ]

        sent_system = [message for message in _MessageRecordingClient.messages if message.get("role") == "system"][0]
        self.assertEqual(result, "done")
        self.assertIn("[Evidence ledger]", sent_system["content"])
        self.assertIn("workspace root", sent_system["content"])
        self.assertIn("pom.xml", sent_system["content"])
        self.assertIn("src/main/java", sent_system["content"])
        self.assertTrue(any(event["payload"]["tool"] == "workspace" for event in evidence_events))

    def test_patch_relevance_gate_blocks_unmentioned_deployment_config_for_code_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "deployMessage" / "nacos" / "app.properties"
            target.parent.mkdir(parents=True)
            target.write_text("old=true\n", encoding="utf-8")
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

            runtime._current_user_request = "实现 Java 导入校验需求"
            runtime._read_file_evidence_paths = ["deployMessage/nacos/app.properties"]
            denied = runtime._patch_relevance_denial_reason(
                "deployMessage/nacos/app.properties",
                target,
            )
            runtime._current_user_request = "请修改 nacos 配置"
            allowed = runtime._patch_relevance_denial_reason(
                "deployMessage/nacos/app.properties",
                target,
            )

        self.assertIsNotNone(denied)
        self.assertIn("deployment/config", denied or "")
        self.assertIsNone(allowed)

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
            (memory_dir / "enterprise-service-boundary.md").write_text(
                "zqyl-charge owns platform fee settlement.\n",
                encoding="utf-8",
            )
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
        self.assertIn(".local-agent/memory/enterprise-service-boundary.md", system_content)
        self.assertIn(str(state_memory_dir / "learned.md"), system_content)
        self.assertIn("Use pytest for this project.", system_content)
        self.assertIn("zqyl-charge owns platform fee settlement.", system_content)
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

    def test_named_authored_skill_is_soft_required_before_final_answer(self) -> None:
        _ReadSkillThenFinalClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            skill_dir = workspace / ".local-agent" / "skills" / "project-scope-analysis"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: project-scope-analysis\n"
                "description: Analyze project scope from service boundaries.\n"
                "---\n\n"
                "Read the boundary table before answering.\n",
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
            with patch("local_agent.agent.OpenAICompatibleClient", _ReadSkillThenFinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("请使用 project-scope-analysis 判断需要关注哪些项目")

        first_call_messages = _ReadSkillThenFinalClient.calls[0]["messages"]
        first_call_text = "\n".join(str(message.get("content") or "") for message in first_call_messages)
        self.assertEqual(result, "skill applied")
        self.assertIn("Required skill file", first_call_text)
        self.assertIn(".local-agent/skills/project-scope-analysis/SKILL.md", first_call_text)
        self.assertEqual(len(_ReadSkillThenFinalClient.calls), 2)

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

    def test_invalid_tool_call_name_is_sanitized_before_next_provider_request(self) -> None:
        _InvalidToolCallThenFinalClient.calls = []
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
            with patch("local_agent.agent.OpenAICompatibleClient", _InvalidToolCallThenFinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("hello")

        self.assertEqual(result, "recovered")
        self.assertGreaterEqual(len(_InvalidToolCallThenFinalClient.calls), 2)
        second_request_messages = _InvalidToolCallThenFinalClient.calls[1]["messages"]
        assistant_messages = [
            message
            for message in second_request_messages
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        self.assertEqual(
            assistant_messages[-1]["tool_calls"][0]["function"]["name"],
            "__invalid_tool_call",
        )
        self.assertEqual(
            assistant_messages[-1]["tool_calls"][0]["function"]["arguments"],
            "{}",
        )
        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        self.assertEqual(tool_messages[-1]["tool_call_id"], "call_empty_name")
        self.assertIn("Unknown tool", tool_messages[-1]["content"])

    def test_malformed_tool_call_arguments_are_sanitized_before_next_provider_request(self) -> None:
        _MalformedToolArgumentsThenFinalClient.calls = []
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
            with patch("local_agent.agent.OpenAICompatibleClient", _MalformedToolArgumentsThenFinalClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("hello")

        self.assertEqual(result, "recovered from malformed args")
        self.assertGreaterEqual(len(_MalformedToolArgumentsThenFinalClient.calls), 2)
        second_request_messages = _MalformedToolArgumentsThenFinalClient.calls[1]["messages"]
        assistant_messages = [
            message
            for message in second_request_messages
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        arguments = assistant_messages[-1]["tool_calls"][0]["function"]["arguments"]
        self.assertIn("_invalid_arguments", json.loads(arguments))
        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        self.assertEqual(tool_messages[-1]["tool_call_id"], "call_bad_args")
        self.assertIn("Missing required argument(s): path", tool_messages[-1]["content"])

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

    def test_semantic_list_files_exploration_is_steered_to_evidence_tools(self) -> None:
        _SemanticListFilesClient.calls = 0
        _SemanticListFilesClient.tool_names_seen = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            for path in _SemanticListFilesClient.paths:
                directory = workspace / path
                directory.mkdir(parents=True)
                (directory / "Sample.java").write_text("class Sample {}\n", encoding="utf-8")
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
            with patch("local_agent.agent.OpenAICompatibleClient", _SemanticListFilesClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("查看目录结构后总结当前项目")

        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        skipped_messages = [
            message for message in tool_messages if "directory exploration under 'service-a'" in message["content"]
        ]
        steering_messages = [
            str(message.get("content"))
            for message in runtime._messages
            if "directory/path exploration is repeating" in str(message.get("content"))
        ]
        final_tools = _SemanticListFilesClient.tool_names_seen[-1]
        self.assertEqual(result, "final answer after semantic exploration guard")
        self.assertEqual(len(skipped_messages), 1)
        self.assertGreaterEqual(len(steering_messages), 1)
        self.assertNotIn("list_files", final_tools)
        self.assertIn("search_code", final_tools)
        self.assertIn("read_file", final_tools)
        self.assertEqual(runtime._last_run_summary["guard_hits"], {"semantic_exploration": 1})
        self.assertEqual(runtime._last_run_summary["steering_counts"], {"semantic_exploration": 1})

    def test_semantic_path_not_found_exploration_is_steered_to_evidence_tools(self) -> None:
        _SemanticPathNotFoundClient.calls = 0
        _SemanticPathNotFoundClient.tool_names_seen = []
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
            with patch("local_agent.agent.OpenAICompatibleClient", _SemanticPathNotFoundClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("查看目录结构后回答")

        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        not_found_messages = [message for message in tool_messages if "Path not found" in message["content"]]
        skipped_messages = [
            message
            for message in tool_messages
            if "directory exploration under 'missing-service/interfaces'" in message["content"]
        ]
        final_tools = _SemanticPathNotFoundClient.tool_names_seen[-1]
        self.assertEqual(result, "final answer after path guessing guard")
        self.assertEqual(len(not_found_messages), 4)
        self.assertEqual(len(skipped_messages), 1)
        self.assertNotIn("list_files", final_tools)
        self.assertIn("search_code", final_tools)
        self.assertEqual(runtime._last_run_summary["guard_hits"], {"semantic_exploration": 1})

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

    def test_repeated_same_read_file_range_is_steered_to_final_answer(self) -> None:
        _RepeatedReadFileSameRangeClient.calls = 0
        _RepeatedReadFileSameRangeClient.tools_seen = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            target = workspace / "service.java"
            target.write_text("class Service {}\n", encoding="utf-8")
            _RepeatedReadFileSameRangeClient.file_path = "service.java"
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
            with patch("local_agent.agent.OpenAICompatibleClient", _RepeatedReadFileSameRangeClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                result = runtime.run("只读分析这个项目并输出证据表")

        tool_messages = [message for message in runtime._messages if message.get("role") == "tool"]
        repeated_messages = [
            message for message in tool_messages if "service.java from line 1" in message["content"]
        ]
        steering_messages = [
            str(message.get("content"))
            for message in runtime._messages
            if "repeated read_file slices from the same file" in str(message.get("content"))
        ]
        self.assertIn("已验证：service.java", result)
        self.assertEqual(_RepeatedReadFileSameRangeClient.tools_seen[-1], 0)
        self.assertEqual(len(repeated_messages), 1)
        self.assertIn("Existing evidence:", repeated_messages[0]["content"])
        self.assertGreaterEqual(len(steering_messages), 1)
        self.assertEqual(runtime._last_run_summary["guard_hits"], {"repeated_read_file": 1})
        self.assertEqual(runtime._last_run_summary["steering_counts"], {"repeated_read_file_final_answer": 1})

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

    def test_context_compaction_can_trigger_from_token_budget(self) -> None:
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
                context_token_budget=300,
                context_recent_messages=2,
                summary_mode="local",
            )
            with patch("local_agent.agent.OpenAICompatibleClient", _MessageRecordingClient):
                runtime = AgentRuntime(config, show_tool_logs=False)
                runtime._messages.extend(
                    [
                        {"role": "user", "content": "old request " + ("x" * 1200)},
                        {"role": "assistant", "content": "old answer " + ("y" * 1200)},
                    ]
                )
                result = runtime.run("new request")
                records = [
                    json.loads(line)
                    for line in runtime._session.path.read_text(encoding="utf-8").splitlines()
                ]

        compaction_records = [
            record for record in records if record.get("event") == "context_compaction"
        ]
        sent_system_messages = [
            message for message in _MessageRecordingClient.messages if message.get("role") == "system"
        ]
        self.assertEqual(result, "done")
        self.assertEqual(compaction_records[0]["payload"]["threshold_tokens"], 255)
        self.assertNotIn("threshold_chars", compaction_records[0]["payload"])
        self.assertIn("estimated_tokens", compaction_records[0]["payload"])
        self.assertIn("[Local context compaction]", sent_system_messages[0]["content"])

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
        self.assertEqual(resolve_compaction_threshold_chars(1200), 1020)
        self.assertEqual(resolve_compaction_threshold_chars(100000), 34464)
        self.assertEqual(resolve_compaction_threshold_tokens(1200), 1020)
        self.assertEqual(resolve_compaction_threshold_tokens(100000), 85000)

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

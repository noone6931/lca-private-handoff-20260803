from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


AgentRole = Literal["explore", "implement", "review"]
AgentState = Literal["queued", "running", "waiting", "completed", "failed", "cancelled", "closed"]
JobState = Literal["queued", "running", "completed", "failed", "cancelled", "closed"]

AGENT_ROLES = frozenset({"explore", "implement", "review"})
AGENT_STATES = frozenset({"queued", "running", "waiting", "completed", "failed", "cancelled", "closed"})
JOB_STATES = frozenset({"queued", "running", "completed", "failed", "cancelled", "closed"})
TERMINAL_AGENT_STATES = frozenset({"completed", "failed", "cancelled", "closed"})
TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled", "closed"})


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    role: AgentRole
    parent_agent_id: str | None
    workspace: Path
    worktree: Path | None
    budget_seconds: int
    tool_allowlist: frozenset[str]

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise ValueError("agent_id must not be empty")
        if self.role not in AGENT_ROLES:
            raise ValueError(f"role must be one of: {', '.join(sorted(AGENT_ROLES))}")
        if not self.workspace.is_absolute():
            raise ValueError("workspace must be an absolute path")
        if not 1 <= self.budget_seconds <= 86_400:
            raise ValueError("budget_seconds must be between 1 and 86400")
        if self.role == "implement":
            if self.worktree is None or not self.worktree.is_absolute():
                raise ValueError("implement agent requires an absolute worktree")
            if self.worktree == self.workspace:
                raise ValueError("implement agent worktree must not be the primary workspace")
        elif self.worktree is not None:
            raise ValueError("only implement agents may declare a worktree")


@dataclass(frozen=True)
class AgentSnapshot:
    agent_id: str
    state: AgentState
    origin_run_id: str
    result_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.agent_id.strip() or not self.origin_run_id.strip():
            raise ValueError("agent_id and origin_run_id must not be empty")
        if self.state not in AGENT_STATES:
            raise ValueError(f"state must be one of: {', '.join(sorted(AGENT_STATES))}")
        if self.state == "completed" and not self.result_ref:
            raise ValueError("completed agent requires result_ref")
        if self.state not in TERMINAL_AGENT_STATES and self.result_ref is not None:
            raise ValueError("non-terminal agent cannot expose result_ref")


@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    owner_agent_id: str
    state: JobState
    cwd: Path
    execution_ref: str | None = None
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if not self.job_id.strip() or not self.owner_agent_id.strip():
            raise ValueError("job_id and owner_agent_id must not be empty")
        if self.state not in JOB_STATES:
            raise ValueError(f"state must be one of: {', '.join(sorted(JOB_STATES))}")
        if not self.cwd.is_absolute():
            raise ValueError("cwd must be an absolute path")
        if self.state == "completed" and (self.execution_ref is None or self.exit_code is None):
            raise ValueError("completed job requires execution_ref and exit_code")
        if self.state not in TERMINAL_JOB_STATES and (
            self.execution_ref is not None or self.exit_code is not None
        ):
            raise ValueError("non-terminal job cannot expose execution outcome")

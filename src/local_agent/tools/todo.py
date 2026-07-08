from __future__ import annotations

import json
from typing import Any

from .base import Tool, ToolContext, ToolResult, tool_state_dir

TODO_STATUSES = ["todo", "in_progress", "done", "blocked", "skipped"]


def todo_tools() -> list[Tool]:
    return [
        Tool(
            name="todo_read",
            description="Read the current session todo list.",
            tier="read",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=todo_read,
        ),
        Tool(
            name="todo_add",
            description="Add a task to the current session todo list.",
            tier="state",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "task": {"type": "string"},
                    "status": {"type": "string", "enum": TODO_STATUSES},
                    "note": {"type": "string"},
                },
                "required": ["id", "task"],
                "additionalProperties": False,
            },
            handler=todo_add,
        ),
        Tool(
            name="todo_update",
            description="Update a task in the current session todo list.",
            tier="state",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "task": {"type": "string"},
                    "status": {"type": "string", "enum": TODO_STATUSES},
                    "note": {"type": "string"},
                },
                "required": ["id"],
                "additionalProperties": False,
            },
            handler=todo_update,
        ),
    ]


def todo_read(args: dict[str, Any], context: ToolContext) -> ToolResult:
    todos = _load_todos(context)
    return ToolResult(_render_todos(todos))


def todo_add(args: dict[str, Any], context: ToolContext) -> ToolResult:
    todos = _load_todos(context)
    todo_id = _clean_id(args["id"])
    if _find_todo(todos, todo_id) is not None:
        return ToolResult(f"Todo already exists: {todo_id}", is_error=True)

    task = args["task"].strip()
    if not task:
        return ToolResult("Todo task must not be empty.", is_error=True)
    status = args.get("status") or "todo"
    todos.append(
        {
            "id": todo_id,
            "task": task,
            "status": status,
            "note": (args.get("note") or "").strip(),
        }
    )
    _save_todos(context, todos)
    return ToolResult(_render_todos(todos))


def todo_update(args: dict[str, Any], context: ToolContext) -> ToolResult:
    todos = _load_todos(context)
    todo_id = _clean_id(args["id"])
    todo = _find_todo(todos, todo_id)
    if todo is None:
        return ToolResult(f"Todo not found: {todo_id}", is_error=True)

    changed = False
    if "task" in args:
        task = args["task"].strip()
        if not task:
            return ToolResult("Todo task must not be empty.", is_error=True)
        todo["task"] = task
        changed = True
    if "status" in args:
        todo["status"] = args["status"]
        changed = True
    if "note" in args:
        todo["note"] = args["note"].strip()
        changed = True
    if not changed:
        return ToolResult("No todo fields to update.", is_error=True)

    _save_todos(context, todos)
    return ToolResult(_render_todos(todos))


def _todo_path(context: ToolContext):
    session_id = context.session_id or "default"
    return tool_state_dir(context) / "todos" / f"{session_id}.json"


def _load_todos(context: ToolContext) -> list[dict[str, str]]:
    path = _todo_path(context)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    todos: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        todo_id = str(item.get("id") or "").strip()
        task = str(item.get("task") or "").strip()
        status = str(item.get("status") or "todo")
        if not todo_id or not task or status not in TODO_STATUSES:
            continue
        todos.append(
            {
                "id": todo_id,
                "task": task,
                "status": status,
                "note": str(item.get("note") or "").strip(),
            }
        )
    return todos


def _save_todos(context: ToolContext, todos: list[dict[str, str]]) -> None:
    path = _todo_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(todos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _render_todos(todos: list[dict[str, str]]) -> str:
    if not todos:
        return "No todos yet."
    lines = ["Current todos:"]
    for todo in todos:
        note = f" — {todo['note']}" if todo.get("note") else ""
        lines.append(f"- [{todo['status']}] {todo['id']}: {todo['task']}{note}")
    return "\n".join(lines)


def _find_todo(todos: list[dict[str, str]], todo_id: str) -> dict[str, str] | None:
    for todo in todos:
        if todo["id"] == todo_id:
            return todo
    return None


def _clean_id(raw_id: str) -> str:
    todo_id = raw_id.strip()
    if not todo_id:
        raise ValueError("Todo id must not be empty.")
    if len(todo_id) > 64:
        raise ValueError("Todo id must be 64 characters or fewer.")
    if any(char.isspace() for char in todo_id):
        raise ValueError("Todo id must not contain whitespace.")
    return todo_id

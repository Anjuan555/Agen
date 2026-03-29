from __future__ import annotations
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from agen_runtime import State, agen_loop

WORKDIR = Path(__file__).with_name("s03_workspace")

@dataclass
class DummyResponse:
    stop_reason: str
    content: list[dict]

class TodoManager:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def update(self, items: list[dict]) -> str:
        if len(items) > 20:
            raise ValueError("Max 20 todos allowed")
        validated: list[dict] = []
        in_progress = 0
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", i + 1))
            if not text:
                raise ValueError(f"Item {item_id}: text required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
            if status == "in_progress":
                in_progress += 1
            validated.append({"id": item_id, "text": text, "status": status})
        if in_progress > 1:
            raise ValueError("Only one task can be in_progress at a time")
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "No todos."
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        lines = [f"{marker[item['status']]} #{item['id']}: {item['text']}" for item in self.items]
        done = sum(item["status"] == "completed" for item in self.items)
        return "\n".join(lines + [f"\n({done}/{len(self.items)} completed)"])

TODO = TodoManager()

def _tool_response(text: str, tool_id: str, name: str, input: dict) -> DummyResponse:
    return DummyResponse(
        stop_reason="tool_use",
        content=[
            {"type": "text", "text": text},
            {"type": "tool_use", "id": tool_id, "name": name, "input": input},
        ],
    )

class DummyAPI:
    def query(self, *, messages: list[dict]) -> DummyResponse:
        last = messages[-1] if messages else None
        content = last.get("content") if isinstance(last, dict) else None
        if not isinstance(content, list):
            return _tool_response(
                "First I should inspect the workspace.",
                "tool-1",
                "write_file",
                {"path": "plan/notes.txt", "content": "draft\n"},
            )
        tool_rounds = sum(
            isinstance(message, dict)
            and message.get("role") == "user"
            and isinstance(message.get("content"), list)
            and any(item.get("type") == "tool_result" for item in message["content"] if isinstance(item, dict))
            for message in messages
        )
        has_reminder = any(item.get("type") == "text" and "Update your todos" in item.get("text", "") for item in content if isinstance(item, dict))
        used_todo = any(item.get("type") == "tool_result" and isinstance(item.get("content"), str) and "#" in item.get("content", "") for item in content if isinstance(item, dict))
        if has_reminder and not used_todo:
            return _tool_response(
                "Right, I should update the plan.",
                "tool-9",
                "todo",
                {"items": [
                    {"id": "1", "text": "Inspect workspace", "status": "completed"},
                    {"id": "2", "text": "Edit notes", "status": "completed"},
                    {"id": "3", "text": "Summarize state", "status": "in_progress"},
                ]},
            )
        if used_todo:
            return DummyResponse(stop_reason="end_turn", content=[{"type": "text", "text": "done: todo list updated"}])
        if tool_rounds == 1:
            return _tool_response("I'll inspect what I just wrote.", "tool-2", "read_file", {"path": "plan/notes.txt"})
        if tool_rounds == 2:
            return _tool_response(
                "I'll update the draft without a todo once more.",
                "tool-3",
                "edit_file",
                {"path": "plan/notes.txt", "old_text": "draft", "new_text": "final draft"},
            )
        return DummyResponse(stop_reason="end_turn", content=[{"type": "text", "text": "done: unexpected path"}])

def safe_path(path: str) -> Path:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    target = (WORKDIR / path).resolve()
    if not target.is_relative_to(WORKDIR.resolve()):
        raise ValueError(f"Path escapes workspace: {path}")
    return target

def run_bash(command: str) -> str:
    try:
        result = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return "Error: Timeout"
    output = (result.stdout + result.stderr).strip()
    return output or "(no output)"

def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)
    except Exception as exc:
        return f"Error: {exc}"

def run_write(path: str, content: str) -> str:
    try:
        target = safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes"
    except Exception as exc:
        return f"Error: {exc}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        target = safe_path(path)
        content = target.read_text(encoding="utf-8")
        if old_text not in content:
            return f"Error: Text not found in {path}"
        target.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as exc:
        return f"Error: {exc}"

TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo": lambda **kw: TODO.update(kw["items"]),
}

_API = DummyAPI()
QUERY = _API.query

def DISPATCH(*, name: str, input: dict) -> str:
    handler = TOOL_HANDLERS.get(name)
    try:
        return handler(**input) if handler else f"Unknown tool: {name}"
    except Exception as exc:
        return f"Error: {exc}"

HELPERS = {"QUERY": QUERY, "DISPATCH": DISPATCH}

if __name__ == "__main__":
    state = agen_loop(State(query="todo demo"), source_path=Path(__file__).with_name("s03.agen"), helpers=HELPERS)
    print(json.dumps(state.messages or [], ensure_ascii=False, indent=2))

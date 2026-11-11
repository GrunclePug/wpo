"""Readable views of sessions: `wpo show <log>` renders one session log as an HTML page, and
`wpo show --wp <id>` renders every session of a WP with an index of its runs.

A Claude-CLI stream-json log becomes a conversation: the system prompt and prompt the session was
given, then each assistant message (text, thinking when the model shows it, tool calls with their
inputs), each tool's result with the time it took, subagent turns indented under their caller,
compactions, background tasks, the orchestrator's messages and the results. Other harnesses' logs
and verification logs are shown as text. Pages are written to `<runs>/<WP>/view/`.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .usage import SESSION_LOG

RESULT_LIMIT = 20_000
TEXT_LOG_LIMIT = 400_000
STYLE = """
body { font: 14px/1.45 system-ui, sans-serif; margin: 0 auto; max-width: 1100px; padding: 16px; background: #f6f7f9; color: #1d2330; }
h1 { font-size: 20px; } h2 { font-size: 16px; margin-top: 28px; }
.meta { color: #5b6475; font-size: 12px; }
.turn { background: #fff; border: 1px solid #dde1e8; border-radius: 6px; margin: 10px 0; padding: 8px 12px; }
.assistant { border-left: 4px solid #3b6fd8; }
.user { border-left: 4px solid #2f9e6a; background: #f1faf5; }
.subagent { margin-left: 32px; border-left-color: #a06ad8; }
.system { border-left: 4px solid #b0b6c2; background: #f3f4f6; font-size: 12px; }
.result { border-left: 4px solid #d89a3b; background: #fff8ee; }
.error { border-left-color: #d83b3b !important; background: #fff1f1; }
.tool { margin: 6px 0; padding: 6px 8px; background: #f3f5fa; border-radius: 4px; }
.tool b { color: #3b4f8a; }
pre { white-space: pre-wrap; word-break: break-word; margin: 4px 0; font: 12px/1.4 ui-monospace, Consolas, monospace; }
.old { background: #ffecec; } .new { background: #eaffea; }
details > summary { cursor: pointer; color: #3b4f8a; }
.text { white-space: pre-wrap; }
table { border-collapse: collapse; } td, th { padding: 2px 10px; border-bottom: 1px solid #e3e6ec; text-align: left; font-size: 13px; }
"""


def escape(text: object) -> str:
    return html.escape(str(text), quote=False)


def clipped(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return f"{text[:head]}\n\n[... {len(text) - limit:,} characters left out ...]\n\n{text[-(limit - head):]}"


def details(summary: str, body: str, open_: bool = False) -> str:
    return f"<details{' open' if open_ else ''}><summary>{summary}</summary>{body}</details>"


def pre(text: str, css: str = "") -> str:
    return f"<pre class='{css}'>{escape(text)}</pre>"


def tool_call(name: str, tool_input: dict) -> str:
    """One tool call: the essentials inline, the rest of the input on demand."""
    path = tool_input.get("file_path") or tool_input.get("path") or ""
    if name in ("Bash", "PowerShell"):
        head = f"<b>{name}</b> {pre(tool_input.get('command', ''))}"
        extra = " (background)" if tool_input.get("run_in_background") else ""
        return head + (f"<span class='meta'>{escape(tool_input.get('description', ''))}{extra}</span>" if tool_input.get("description") or extra else "")
    if name == "Read":
        span = f" lines {tool_input['offset']}+{tool_input.get('limit', '')}" if tool_input.get("offset") else ""
        return f"<b>Read</b> {escape(path)}{escape(span)}"
    if name == "Edit":
        flag = " (all)" if tool_input.get("replace_all") else ""
        return (f"<b>Edit</b> {escape(path)}{flag}" + pre(tool_input.get("old_string", ""), "old") + pre(tool_input.get("new_string", ""), "new"))
    if name == "Write":
        content = tool_input.get("content", "")
        return f"<b>Write</b> {escape(path)} " + details(f"{len(content):,} characters", pre(clipped(content, RESULT_LIMIT)))
    if name in ("Glob", "Grep"):
        where = f" in {tool_input['path']}" if tool_input.get("path") else ""
        return f"<b>{name}</b> {escape(tool_input.get('pattern', ''))}{escape(where)}"
    if name in ("Agent", "Task"):
        return (f"<b>{name}</b> {escape(tool_input.get('subagent_type', ''))}: {escape(tool_input.get('description', ''))}"
                + details("task", pre(tool_input.get("prompt", ""))))
    return f"<b>{escape(name)}</b> " + details("input", pre(json.dumps(tool_input, indent=1)))


def result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    return json.dumps(content)


@dataclass
class Pending:
    """A tool call waiting for its result, to report how long it took."""

    name: str
    started: float | None


def stamp(event: dict) -> float | None:
    value = event.get("timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


def clock(when: float | None) -> str:
    return datetime.fromtimestamp(when).strftime("%H:%M:%S") if when else ""


def render_claude_log(log_path: Path) -> str:
    """The conversation in a Claude-CLI stream-json log, as HTML."""
    parts: list[str] = []
    pending: dict[str, Pending] = {}
    previous: float | None = None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind, subtype = event.get("type"), event.get("subtype")
        when = stamp(event)
        nested = " subagent" if event.get("parent_tool_use_id") else ""
        if kind == "system":
            if subtype == "init":
                parts.append(f"<div class='turn system'>session {escape(event.get('session_id', ''))} · model {escape(event.get('model', ''))} · "
                             f"tools {escape(', '.join(event.get('tools', [])))}</div>")
            elif subtype == "compact_boundary":
                parts.append("<div class='turn system'><b>compaction</b>: the conversation so far was summarized</div>")
            elif subtype in ("task_started", "task_notification"):
                parts.append(f"<div class='turn system'>{escape(subtype.replace('_', ' '))}: {escape(event.get('description') or event.get('summary') or '')}</div>")
            continue
        if kind == "result":
            css = "turn result error" if event.get("is_error") else "turn result"
            usage = event.get("usage") or {}
            figures = (f"turns {event.get('num_turns', '?')} · {event.get('duration_ms', 0) / 60000:.1f} min · "
                       f"cost estimate ${event.get('total_cost_usd') or 0:.2f} · cache read {usage.get('cache_read_input_tokens', 0):,}")
            parts.append(f"<div class='{css}'><b>result</b> <span class='meta'>{escape(figures)}</span>"
                         f"<div class='text'>{escape(event.get('result', ''))}</div></div>")
            continue
        if kind not in ("assistant", "user"):
            continue
        message = event.get("message") or {}
        content = message.get("content")
        if kind == "user" and isinstance(content, str):
            parts.append(f"<div class='turn user{nested}'><b>message</b> <span class='meta'>{clock(when)}</span><div class='text'>{escape(content)}</div></div>")
            continue
        blocks = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and block.get("text"):
                blocks.append(f"<div class='text'>{escape(block['text'])}</div>")
            elif block_type == "thinking" and block.get("thinking"):
                blocks.append(details("thinking", pre(block["thinking"])))
            elif block_type == "tool_use":
                pending[block["id"]] = Pending(block.get("name", "?"), when)
                blocks.append(f"<div class='tool'>{tool_call(block.get('name', '?'), block.get('input') or {})}</div>")
            elif block_type == "tool_result":
                call = pending.pop(block.get("tool_use_id"), None)
                took = f" after {when - call.started:.0f} s" if call and when and call.started else ""
                text = result_text(block.get("content"))
                label = f"{escape(call.name if call else 'tool')} result{took} · {len(text):,} chars"
                css = "tool error" if block.get("is_error") else "tool"
                blocks.append(f"<div class='{css}'>" + details(label, pre(clipped(text, RESULT_LIMIT))) + "</div>")
        if not blocks:
            continue
        usage = message.get("usage") or {}
        context = sum(int(usage.get(key) or 0) for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        gap = f" · +{when - previous:.0f} s" if when and previous else ""
        meta = f"{clock(when)}{gap}" + (f" · context {context:,}" if context else "")
        role = "assistant" if kind == "assistant" else "user"
        parts.append(f"<div class='turn {role}{nested}'><span class='meta'>{role}{' (subagent)' if nested else ''} · {meta}</span>{''.join(blocks)}</div>")
        if when:
            previous = when
    return "\n".join(parts)


def page(title: str, body: str) -> str:
    return f"<!doctype html><html><head><meta charset='utf-8'><title>{escape(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>"


def render_session(log_path: Path) -> str:
    """One session log (or a text log) as a page, with the prompt and system prompt it was given."""
    sections = [f"<h1>{escape(log_path.name)}</h1><p class='meta'>{escape(log_path)}</p>"]
    for suffix, label in ((".system.md", "system prompt (appended)"), (".prompt.md", "prompt")):
        companion = log_path.with_name(log_path.name + suffix)
        if companion.exists():
            sections.append(details(label, pre(clipped(companion.read_text(encoding="utf-8", errors="replace"), RESULT_LIMIT * 3))))
    match = SESSION_LOG.match(log_path.name)
    route = match.group("route") if match else ""
    if route.startswith(("claude-", "gpt-", "local-")):
        sections.append(render_claude_log(log_path))
    else:
        sections.append(pre(clipped(log_path.read_text(encoding="utf-8", errors="replace"), TEXT_LOG_LIMIT)))
    inbox_record = log_path.with_name(log_path.name + ".inbox.log")
    if inbox_record.exists():
        sections.append("<h2>Messages delivered</h2>" + pre(inbox_record.read_text(encoding="utf-8")))
    return page(log_path.name, "\n".join(sections))


def run_files(run_dir: Path) -> list[Path]:
    """A WP's session and verification logs in the order they ran."""
    logs = [path for path in run_dir.rglob("*.log") if "view" not in path.relative_to(run_dir).parts]
    return sorted(logs, key=lambda path: path.stat().st_mtime)


def view_name(run_dir: Path, log_path: Path) -> str:
    return re.sub(r"[\\/]", "__", log_path.relative_to(run_dir).as_posix()) + ".html"


def render_wp(run_dir: Path) -> Path:
    """Every log of a WP rendered, with an index page; returns the index."""
    view = run_dir / "view"
    view.mkdir(exist_ok=True)
    rows = []
    for log_path in run_files(run_dir):
        name = view_name(run_dir, log_path)
        (view / name).write_text(render_session(log_path), encoding="utf-8")
        modified = datetime.fromtimestamp(log_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        rows.append(f"<tr><td>{modified}</td><td><a href='{escape(name)}'>{escape(log_path.relative_to(run_dir).as_posix())}</a></td>"
                    f"<td>{log_path.stat().st_size / 1024:,.0f} KB</td></tr>")
    index = view / "index.html"
    index.write_text(page(run_dir.name, f"<h1>{escape(run_dir.name)}</h1><p class='meta'>sessions and logs, last written last</p>"
                                        f"<table><tr><th>last write</th><th>log</th><th>size</th></tr>{''.join(rows)}</table>"), encoding="utf-8")
    return index


def render_one(log_path: Path) -> Path:
    view = log_path.parent / "view"
    view.mkdir(exist_ok=True)
    target = view / (log_path.name + ".html")
    target.write_text(render_session(log_path), encoding="utf-8")
    return target

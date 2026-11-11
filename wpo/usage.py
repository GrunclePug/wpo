"""Usage reports: what every session cost in tokens, cache and time, and what each change to the
tooling did to it (`wpo usage report|changes`).

Sessions are read from the run logs (`<runs>/<WP>/<stem>.<route>.log[.resumed]`, delegated workers under
`<WP>/delegate/<task>/`, dispatches under `dispatch/<name>/`). Claude-CLI logs (claude, gpt and local
harnesses) carry per-message usage, timestamps and tool calls; other harnesses' logs are counted by
wall time only. Parsed records are cached in `<runs>/usage/index.json`, keyed by each log's size and
modification time, so a report re-reads only logs that changed.

Changes are the skill's own commits plus the project commits that touch its configuration or
instructions; `changes` splits the timeline at them and compares each segment with the one before.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .project import Project
from .whats_new import merged_wps

SKILL_ROOT = Path(__file__).resolve().parent.parent
INDEX_VERSION = 1
SESSION_LOG = re.compile(r"^(?P<stem>[^.]+)\.(?P<route>.+?)(?P<resumed>\.resumed)?\.log$")
CLAUDE_CLI_ROUTE_PREFIXES = ("claude-", "gpt-", "local-")
# Project files whose changes alter what sessions are told or how they run.
PROJECT_CHANGE_PATHS = (".orchestration/config.json", "AGENTS.md")
TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


@dataclass
class SessionRecord:
    """One harness session, as its log shows it."""

    log: str
    wp: str | None
    role: str
    route: str
    family: str
    resumed: bool
    parsed: bool
    started: float
    ended: float
    turns: int = 0
    subagent_turns: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    max_context: int = 0
    compactions: int = 0
    cost_usd: float | None = None
    model_usage: dict[str, dict] = field(default_factory=dict)
    model_seconds: float = 0.0
    tool_seconds: float = 0.0
    tool_seconds_by_kind: dict[str, float] = field(default_factory=dict)

    @property
    def prompt_tokens(self) -> int:
        return sum(self.tokens.get(key, 0) for key in TOKEN_KEYS[:3])

    @property
    def wall_seconds(self) -> float:
        return max(0.0, self.ended - self.started)


def route_family(route: str) -> str:
    for prefix, family in (("claude-", "claude"), ("gpt-", "gpt"), ("local-", "local"), ("agy-", "agy"), ("xai-", "xai")):
        if route.startswith(prefix):
            return family
    return "free"


def session_role(path: Path, runs: Path, stem: str) -> tuple[str | None, str]:
    """The WP a log belongs to and the session's role."""
    parts = path.relative_to(runs).parts
    if parts[0] == "dispatch":
        return None, "dispatch"
    wp = parts[0]
    if "delegate" in parts:
        return wp, "worker"
    role = re.sub(r"^attempt-\d+-", "", stem)
    return wp, re.sub(r"-\d+$", "", role)


def tool_kind(name: str, tool_input: dict) -> str:
    """A tool call's kind for the time breakdown: the program and subcommand for shell calls."""
    if name not in ("Bash", "PowerShell"):
        return name
    words = str(tool_input.get("command", "")).split()
    while words and words[0] == "rtk":
        words = words[1:]
    if not words:
        return name
    if len(words) > 1 and words[0] in ("cargo", "git", "wpo", "uv", "bun", "npm"):
        return f"{words[0]} {words[1]}"
    return words[0][:24]


def timestamp(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def parse_claude_log(path: Path, record: SessionRecord) -> None:
    """Fills `record` from a Claude-CLI stream-json log."""
    usage_by_message: dict[str, dict] = {}
    subagent_messages: set[str] = set()
    timeline: list[tuple[float, str, dict]] = []
    kinds: dict[str, str] = {}
    result: dict | None = None
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "result":
                result = event
            elif kind == "system" and event.get("subtype") == "compact_boundary":
                record.compactions += 1
            if kind not in ("assistant", "user"):
                continue
            message = event.get("message") or {}
            if event.get("parent_tool_use_id"):
                if kind == "assistant":
                    subagent_messages.add(message.get("id") or event.get("uuid", ""))
                continue
            if kind == "assistant":
                if message.get("usage"):
                    usage_by_message[message.get("id") or event.get("request_id") or event.get("uuid", "")] = message["usage"]
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        kinds[block["id"]] = tool_kind(block.get("name", "?"), block.get("input") or {})
            if "timestamp" in event:
                timeline.append((timestamp(event["timestamp"]), kind, message))
    record.turns = len(usage_by_message)
    record.subagent_turns = len(subagent_messages)
    record.max_context = max((sum(usage.get(key) or 0 for key in TOKEN_KEYS[:3]) for usage in usage_by_message.values()), default=0)
    final_usage = (result or {}).get("usage") or {}
    # The result's usage is the session's total (the streamed per-message counts of proxied models
    # are partial); without a result, the streamed counts are all there is.
    sources = [final_usage] if final_usage else list(usage_by_message.values())
    record.tokens = {key: sum(int(source.get(key) or 0) for source in sources) for key in TOKEN_KEYS}
    if result is not None:
        record.cost_usd = result.get("total_cost_usd")
        record.model_usage = {model: {key: value for key, value in entry.items() if isinstance(value, (int, float))}
                              for model, entry in (result.get("modelUsage") or {}).items()}
    timeline.sort(key=lambda item: item[0])
    by_kind: Counter[str] = Counter()
    for (start, _, _), (end, kind, message) in zip(timeline, timeline[1:]):
        gap = end - start
        if gap < 0:
            continue
        if kind == "assistant":
            record.model_seconds += gap
            continue
        record.tool_seconds += gap
        call = next((block.get("tool_use_id") for block in message.get("content") or []
                     if isinstance(block, dict) and block.get("type") == "tool_result"), None)
        by_kind[kinds.get(call, "other")] += gap
    record.tool_seconds_by_kind = dict(by_kind)
    if result is not None and result.get("backend") == "t3" and not final_usage and not usage_by_message:
        record.parsed = False
        record.tokens = {}
    if timeline:
        record.started, record.ended = timeline[0][0], timeline[-1][0]


def read_session(path: Path, runs: Path) -> SessionRecord | None:
    match = SESSION_LOG.match(path.name)
    if match is None or match.group("stem") in ("worker-process", "integrate", "orchestrator-verify"):
        return None
    route = match.group("route")
    wp, role = session_role(path, runs, match.group("stem"))
    status = path.stat()
    record = SessionRecord(path.relative_to(runs).as_posix(), wp, role, route, route_family(route), bool(match.group("resumed")),
                           route.startswith(CLAUDE_CLI_ROUTE_PREFIXES), status.st_ctime, status.st_mtime)
    if record.parsed:
        parse_claude_log(path, record)
    return record


def session_logs(runs: Path) -> list[Path]:
    # Session logs live in a WP's or a dispatch's directory; the runs root holds supervisor logs.
    return sorted(path for path in runs.rglob("*.log") if SESSION_LOG.match(path.name)
                  and len(path.relative_to(runs).parts) > 1 and path.relative_to(runs).parts[0] not in ("jobs", "usage"))


def load_sessions(project: Project) -> list[SessionRecord]:
    """Every session record, re-parsing only logs that changed since the index was written."""
    runs = project.runs_dir
    index_path = runs / "usage" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    if index.get("version") != INDEX_VERSION:
        index = {"version": INDEX_VERSION, "logs": {}}
    fresh: dict[str, dict] = {}
    records: list[SessionRecord] = []
    for path in session_logs(runs):
        key = path.relative_to(runs).as_posix()
        status = path.stat()
        cached = index["logs"].get(key)
        if cached and cached["size"] == status.st_size and cached["mtime"] == status.st_mtime:
            record = SessionRecord(**cached["record"]) if cached["record"] else None
        else:
            record = read_session(path, runs)
        fresh[key] = {"size": status.st_size, "mtime": status.st_mtime, "record": asdict(record) if record else None}
        if record is not None:
            records.append(record)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps({"version": INDEX_VERSION, "logs": fresh}), encoding="utf-8")
    return sorted(records, key=lambda record: record.started)


# ----------------------------------------------------------------------------------- changes


@dataclass(frozen=True)
class Change:
    """A commit that changed how sessions run: the skill's own, or the project's configuration."""

    when: float
    source: str
    commit: str
    subject: str


def git_changes(repository: Path, source: str, paths: tuple[str, ...] = ()) -> list[Change]:
    completed = subprocess.run(["git", "-C", str(repository), "log", "--format=%H%x09%ct%x09%s", "--", *paths],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    changes = []
    for line in completed.stdout.splitlines():
        commit, when, subject = line.split("\t", 2)
        changes.append(Change(float(when), source, commit[:10], subject))
    return changes


def tooling_changes(project: Project) -> list[Change]:
    return sorted(git_changes(SKILL_ROOT, "skill") + git_changes(project.root, "project", PROJECT_CHANGE_PATHS), key=lambda change: change.when)


# ----------------------------------------------------------------------------------- summaries


@dataclass
class Summary:
    """Aggregate figures for a group of sessions."""

    sessions: int
    parsed: int
    turns_mean: float
    context_mean: float
    prompt_per_session: float
    cache_read_share: float
    cache_write_share: float
    output_per_session: float
    cost_per_session: float | None
    tool_share: float
    wall_hours: float
    compactions: int
    subagent_turns: int


def summarize(records: list[SessionRecord]) -> Summary:
    parsed = [record for record in records if record.parsed]
    tokens = Counter()
    for record in parsed:
        tokens.update(record.tokens)
    prompt = sum(tokens[key] for key in TOKEN_KEYS[:3])
    turns = sum(record.turns for record in parsed)
    costs = [record.cost_usd for record in parsed if record.cost_usd is not None]
    model = sum(record.model_seconds for record in parsed)
    tool = sum(record.tool_seconds for record in parsed)
    return Summary(
        sessions=len(records), parsed=len(parsed),
        turns_mean=turns / len(parsed) if parsed else 0.0,
        context_mean=prompt / turns if turns else 0.0,
        prompt_per_session=prompt / len(parsed) if parsed else 0.0,
        cache_read_share=tokens["cache_read_input_tokens"] / prompt if prompt else 0.0,
        cache_write_share=tokens["cache_creation_input_tokens"] / prompt if prompt else 0.0,
        output_per_session=tokens["output_tokens"] / len(parsed) if parsed else 0.0,
        cost_per_session=statistics.fmean(costs) if costs else None,
        tool_share=tool / (model + tool) if model + tool else 0.0,
        wall_hours=sum(record.wall_seconds for record in records) / 3600,
        compactions=sum(record.compactions for record in parsed),
        subagent_turns=sum(record.subagent_turns for record in parsed),
    )


def group_key(record: SessionRecord, by: tuple[str, ...]) -> tuple[str, ...]:
    values = {"family": record.family, "role": record.role, "route": record.route, "wp": record.wp or "-",
              "day": datetime.fromtimestamp(record.started).strftime("%Y-%m-%d")}
    return tuple(values[name] for name in by)


SUMMARY_HEADER = ("sessions", "turns", "ctx/turn", "prompt/session", "cache read", "cache write", "out/session", "cost/session", "tool time", "wall h", "compactions", "subagent turns")


def summary_cells(summary: Summary) -> list[str]:
    cost = f"${summary.cost_per_session:.2f}" if summary.cost_per_session is not None else "-"
    return [f"{summary.sessions} ({summary.parsed} parsed)", f"{summary.turns_mean:.0f}", f"{summary.context_mean / 1e3:.0f}k",
            f"{summary.prompt_per_session / 1e6:.1f}M", f"{summary.cache_read_share:.1%}", f"{summary.cache_write_share:.1%}",
            f"{summary.output_per_session / 1e3:.0f}k", cost, f"{summary.tool_share:.0%}", f"{summary.wall_hours:.1f}",
            str(summary.compactions), str(summary.subagent_turns)]


def table(header: tuple[str, ...], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def grouped_table(records: list[SessionRecord], by: tuple[str, ...]) -> str:
    groups: dict[tuple[str, ...], list[SessionRecord]] = defaultdict(list)
    for record in records:
        groups[group_key(record, by)].append(record)
    rows = [[*key, *summary_cells(summarize(members))] for key, members in sorted(groups.items())]
    return table((*by, *SUMMARY_HEADER), rows)


def tool_time_table(records: list[SessionRecord], limit: int = 12) -> str:
    by_kind: Counter[str] = Counter()
    for record in records:
        by_kind.update(record.tool_seconds_by_kind)
    total = sum(by_kind.values()) or 1.0
    rows = [[kind, f"{seconds / 3600:.1f}", f"{seconds / total:.0%}"] for kind, seconds in by_kind.most_common(limit)]
    return table(("tool", "hours", "share"), rows)


def model_usage_table(records: list[SessionRecord]) -> str:
    """Per-model totals from the results' modelUsage, subagents included."""
    totals: dict[str, Counter] = defaultdict(Counter)
    for record in records:
        for model, entry in record.model_usage.items():
            totals[model].update(entry)
    rows = []
    for model, entry in sorted(totals.items(), key=lambda item: -item[1].get("costUSD", 0)):
        prompt = entry.get("inputTokens", 0) + entry.get("cacheReadInputTokens", 0) + entry.get("cacheCreationInputTokens", 0)
        rows.append([model, f"{prompt / 1e6:.1f}M", f"{entry.get('cacheReadInputTokens', 0) / prompt:.1%}" if prompt else "-",
                     f"{entry.get('outputTokens', 0) / 1e6:.2f}M", f"${entry.get('costUSD', 0):.2f}"])
    return table(("model", "prompt", "cache read", "output", "cost (CLI estimate)"), rows)


def in_window(records: list[SessionRecord], since: datetime | None, until: datetime | None) -> list[SessionRecord]:
    low = since.timestamp() if since else float("-inf")
    high = until.timestamp() if until else float("inf")
    return [record for record in records if low <= record.started < high]


NOTES = """Figures: tokens and turns cover the session's own conversation (subagent turns are counted
separately; their tokens are in the per-model table). ctx/turn is prompt tokens per turn; tool time is
the share of the session's wall time spent waiting on tools rather than the model. Costs are the
Claude CLI's list-price estimates: right for Claude models, not meaningful for models it does not
know (GPT through the proxy, the local model). Logs of other harnesses are counted by wall time only."""


def report(project: Project, since: datetime | None, until: datetime | None, by: tuple[str, ...]) -> str:
    records = in_window(load_sessions(project), since, until)
    span = f"{since:%Y-%m-%d %H:%M}" if since else "the first session"
    merged = [entry for entry in merged_wps(project, since, None) if until is None or entry.merged_at < until.astimezone()]
    parts = [f"# Usage report: {project.name}", f"Sessions started from {span} to {f'{until:%Y-%m-%d %H:%M}' if until else 'now'}; "
             f"{len(merged)} WPs merged in that time.", NOTES,
             "## By " + ", ".join(by), grouped_table(records, by),
             "## Per model (results' modelUsage)", model_usage_table(records),
             "## Where tool time goes", tool_time_table(records)]
    return "\n\n".join(parts) + "\n"


def change_segments(changes: list[Change], merge_within: timedelta) -> list[list[Change]]:
    """Changes grouped into one boundary when each follows the previous within `merge_within`."""
    groups: list[list[Change]] = []
    for change in changes:
        if groups and change.when - groups[-1][-1].when <= merge_within.total_seconds():
            groups[-1].append(change)
        else:
            groups.append([change])
    return groups


def changes_report(project: Project, since: datetime | None, by: tuple[str, ...], merge_within: timedelta) -> str:
    """Each tooling change (or burst of changes) with the sessions that ran under it, against the
    segment before; a change's effect is read from the segment that starts at it."""
    records = load_sessions(project)
    changes = [change for change in tooling_changes(project) if since is None or change.when >= since.timestamp()]
    groups = change_segments(changes, merge_within)
    boundaries = [group[0].when for group in groups] + [float("inf")]
    before_first = since.timestamp() if since else float("-inf")
    parts = [f"# Tooling changes and their effect: {project.name}", NOTES,
             "Each section lists the commits of one change (commits within "
             f"{merge_within} of each other form one) and the sessions that started under it, until the next change."]
    previous = [record for record in records if before_first <= record.started < (boundaries[0] if groups else float("inf"))]
    if groups:
        parts += [f"## Before {datetime.fromtimestamp(boundaries[0]):%Y-%m-%d %H:%M}", grouped_table(previous, by)]
    for group, start, end in zip(groups, boundaries, boundaries[1:]):
        segment = [record for record in records if start <= record.started < end]
        listing = "\n".join(f"- {datetime.fromtimestamp(change.when):%Y-%m-%d %H:%M} {change.source} {change.commit}: {change.subject}" for change in group)
        title = f"## From {datetime.fromtimestamp(start):%Y-%m-%d %H:%M}" + (f" to {datetime.fromtimestamp(end):%Y-%m-%d %H:%M}" if end != float("inf") else "")
        parts += [title, listing, grouped_table(segment, by) if segment else "No sessions started under this change."]
    return "\n\n".join(parts) + "\n"


def write_report(project: Project, name: str, text: str) -> Path:
    directory = project.runs_dir / "usage"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}-{datetime.now(timezone.utc).astimezone():%Y%m%d-%H%M%S}.md"
    path.write_text(text, encoding="utf-8")
    return path

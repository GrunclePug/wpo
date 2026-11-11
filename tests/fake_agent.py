"""A scripted stand-in for an agent harness, for the end-to-end test.

Reads its prompt from stdin, recognizes its role, does what the scenario (FAKE_SCENARIO, JSON) says
for its WP, and prints a Claude stream-json result event. Review verdicts are consumed in order;
the per-role counters live in FAKE_STATE.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ENTRY_POINT = Path(__file__).resolve().parent.parent / "bin" / "wpo.py"


def result(text: str) -> None:
    # Flushed: a streaming watcher reads the result from the log before it closes stdin.
    print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text}), flush=True)


def streamed() -> bool:
    return "--input-format" in sys.argv and sys.argv[sys.argv.index("--input-format") + 1] == "stream-json"


def read_prompt() -> str:
    """The prompt: all of stdin, or the first stream-json user message."""
    if not streamed():
        return sys.stdin.read()
    return json.loads(sys.stdin.readline())["message"]["content"]


def answer_messages(state_dir: Path) -> None:
    """Streamed sessions stay open until stdin closes: each further message gets a turn, recorded."""
    if not streamed():
        return
    for line in sys.stdin:
        if line.strip():
            content = json.loads(line)["message"]["content"]
            with (state_dir / "messages.log").open("a", encoding="utf-8") as record:
                record.write(content.replace(chr(10), " ") + chr(10))
            result("acknowledged")


def next_verdict(state_dir: Path, wp_id: str, verdicts: list[str]) -> str:
    counter = state_dir / f"{wp_id}-reviews"
    count = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(count + 1))
    return verdicts[min(count, len(verdicts) - 1)]


def wp_file(wp_id: str) -> Path:
    return next(Path("docs/plan/wp").glob(f"{wp_id}-*.md"))


def to_review(wp_id: str) -> None:
    """Sets review and writes the Try it section, as a builder or assembler does."""
    path = wp_file(wp_id)
    text = re.sub(r"(?m)^status:\s*\S+", "status: review", path.read_text(encoding="utf-8"), count=1)
    if "## Try it" not in text:
        text = text.replace("## Handoff", f"## Try it\n```\ntype {wp_id}.txt\n```\n\n## Handoff", 1)
    path.write_text(text, encoding="utf-8")


def apply(actions: dict) -> None:
    for path, content in actions.get("files", {}).items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
    for path, line in actions.get("append", {}).items():
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(line)


def main() -> None:
    prompt = read_prompt()
    scenario = json.loads(Path(os.environ["FAKE_SCENARIO"]).read_text(encoding="utf-8"))
    state_dir = Path(os.environ["FAKE_STATE"])
    respond(prompt, scenario, state_dir)
    answer_messages(state_dir)


def respond(prompt: str, scenario: dict, state_dir: Path) -> None:
    if match := re.search(r"You are the builder of work package (WP-\d+)", prompt):
        wp_id = match.group(1)
        script = scenario["builders"][wp_id]
        handoffs = state_dir / f"{wp_id}-handoffs"
        if script.get("handoff_first") and not handoffs.exists():
            # The first session hands off before finishing; the fresh one continues from its handoff.
            handoffs.write_text("1")
            subprocess.run([sys.executable, "-I", "-B", str(ENTRY_POINT), "handoff", "Done: half of the files.\nNext: the rest."], check=True)
            result("Handoff recorded.")
            return
        cut = state_dir / f"{wp_id}-cut-off"
        if script.get("cut_off_first") and not cut.exists():
            # The first session writes its files and is cut off mid-turn: the harness dies without
            # ending its turn.
            cut.write_text("1")
            apply(script)
            sys.exit(1)
        apply(script)
        # Answers the findings a rejected round names, as `wpo verify` requires.
        if answered := re.search(r"Answer each open BLOCKING finding \(([^)]*)\)", prompt):
            for finding in answered.group(1).split(", "):
                subprocess.run([sys.executable, "-I", "-B", str(ENTRY_POINT), "finding", "fixed", finding, "a.txt:1 fixed"], check=True)
        if delegated := script.get("delegate"):
            task = Path(f".orchestration/tasks/{wp_id}/{delegated['name']}.md")
            task.parent.mkdir(parents=True, exist_ok=True)
            task.write_text(delegated["task"], encoding="utf-8")
            wpo = [sys.executable, "-I", "-B", str(ENTRY_POINT), "delegate"]
            subprocess.run([*wpo, "start", "--name", delegated["name"], "--prompt-file", task.as_posix(), "--write-paths", delegated["write_paths"]], check=True)
            for _ in range(60):
                waited = subprocess.run([*wpo, "wait", "--name", delegated["name"], "--minutes", "0.2"], capture_output=True, text=True, check=True).stdout
                if "still running" not in waited:
                    break
                time.sleep(1)
        to_review(wp_id)
        result("Verify passed; the WP is in review.")
    elif match := re.search(r"Review work package (WP-\d+)", prompt):
        verdict = next_verdict(state_dir, match.group(1), scenario["reviews"].get(match.group(1), ["PASS"]))
        review_number = re.search(r"This is review (\d+) of", prompt).group(1)
        # Rules every finding the ledger lists as open still open, and raises one new finding.
        rulings = "".join(f"RULING {identifier} STILL-OPEN: still wrong\n" for identifier in re.findall(r"(?m)^- (R\d+-F\d+) BLOCKING", prompt))
        result(f"{rulings}FINDING R{review_number}-F1 BLOCKING a.txt:1\n  Basis: criterion 1 quoted\n  Problem: something\n  Fix: fix it\nVERDICT: {verdict}"
               if verdict != "PASS" else "No findings.\nVERDICT: PASS")
    elif re.search(r"You are re-evaluating work package (WP-\d+)", prompt):
        result("The findings converge." + chr(10) + "DECISION: continue")
    elif match := re.search(r"You are a repair session on work package (WP-\d+)", prompt):
        apply(scenario["repairs"][match.group(1)])
        result("repaired")
    elif match := re.search(r"You are a worker on work package (WP-\d+)", prompt):
        apply(scenario["workers"][match.group(1)])
        result("worker done")
    elif match := re.search(r"You are the planner for work package (WP-\d+)", prompt):
        plans = scenario["plans"][match.group(1)]
        counter = state_dir / f"{match.group(1)}-plans"
        count = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(count + 1))
        plan_path = Path(re.search(r"Write the plan as JSON to (\S+)", prompt).group(1))
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plans[min(count, len(plans) - 1)]), encoding="utf-8")
        result("PLAN: written")
    elif match := re.search(r"# Task (\S+): ", prompt):
        actions = scenario["executors"][match.group(1)]
        if actions.get("compact"):
            # What the Claude CLI logs when it compacts; the runner stops the session.
            print(json.dumps({"type": "system", "subtype": "compact_boundary"}), flush=True)
            time.sleep(120)
        apply(actions)
        result("task done")
    elif match := re.search(r"You are the assembler for work package (WP-\d+)", prompt):
        to_review(match.group(1))
        result("Verify passed; the WP is in review.")
    else:
        result("unrecognized prompt")


if __name__ == "__main__":
    main()

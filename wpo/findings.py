"""Review findings as a ledger both sides of a review answer to, under one protocol.

A reviewer reports each finding with a stable id (`R<review>-F<n>`; review numbers count every
review of the WP and never restart) and, for a BLOCKING one, the basis it rests on. The builder
answers each open BLOCKING finding: fixed, or vetoed on a named ground. The next reviewer rules on
every earlier open finding and every veto. The ledger (`<runs>/<WP>/findings.json`) keeps ids,
answers and rulings across rounds, so no review repeats a settled point and no builder re-fixes one.

    wpo finding fixed <id> '<what changed, with file:line>'      (in a WP session)
    wpo finding veto <id> <ground> '<evidence>'                  (in a WP session)

Findings only the orchestrator can act on (the WP file's contract, ownership, a split, another
WP's paths) are ORCHESTRATOR notes: they never block, and the runner journals them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .context import Context
from .platform import replace_file
from .session_commands import SessionCommandError, session_wp

FINDINGS_FILE = "findings.json"
FINDING_ID = re.compile(r"^R[0-9]+-F[0-9]+$")
VETO_GROUNDS = {
    "no-basis": "the finding quotes no acceptance criterion, project rule or failing check, and shows no reproduced defect",
    "misquoted": "the quoted criterion or rule does not say what the finding claims, or the quote is not in the cited file",
    "outside-wp": "the fix would change a path outside the WP's owns, or add work its Goal and criteria do not ask for",
    "not-reproduced": "the counterexample does not reproduce: the builder ran it on the real code path and data and shows the output",
    "contradicts-wp": "the request contradicts the WP file (its Goal, Context, criteria or Out of scope) or an earlier accepted veto",
}
RULINGS = ("RESOLVED", "STILL-OPEN", "VETO-ACCEPTED", "VETO-REJECTED")
# Rulings that leave a finding blocking; the others close it.
OPEN_RULINGS = ("STILL-OPEN", "VETO-REJECTED")

# Markdown around a protocol line (bullets, bold, quotes) is tolerated.
LINE_PREFIX = r"^[ \t*_`>-]*"
FINDING_LINE = re.compile(LINE_PREFIX + r"FINDING[ \t*_`]+(R[0-9]+-F[0-9]+)[ \t*_`]+(BLOCKING|NON-BLOCKING)\b[ \t*_`:]*(.*)$", re.MULTILINE)
RULING_LINE = re.compile(LINE_PREFIX + r"RULING[ \t*_`]+(R[0-9]+-F[0-9]+)[ \t*_`]+(" + "|".join(RULINGS) + r")\b[ \t*_`:]*(.*)$", re.MULTILINE)
ORCHESTRATOR_LINE = re.compile(LINE_PREFIX + r"ORCHESTRATOR:[ \t*_`]*(.+)$", re.MULTILINE)
SECTION_END = re.compile(LINE_PREFIX + r"(?:FINDING|RULING|ORCHESTRATOR:|VERDICT:)", re.MULTILINE)

GROUND_LIST = "\n".join(f"  - `{ground}`: {meaning}." for ground, meaning in VETO_GROUNDS.items())

# Both the reviewer and the builder read these rules verbatim, so each knows exactly what the other may do.
REVIEW_PROTOCOL = f"""## Review protocol (the reviewer and the builder both work under these exact rules)
1. A BLOCKING finding needs one basis, stated on its 'Basis:' line:
   - an acceptance criterion, by number, with its words quoted verbatim from the WP file;
   - a project rule (AGENTS.md, the coding standards, a review rule in this prompt), quoted verbatim with its source;
   - a Verify or Try it command that fails or does not exist, with the command;
   - a defect reproduced on the real code path and the real data: the command run, the input, and the wrong output seen.
   A hypothetical ('could', 'might later', 'a future caller'), a preference, a style choice no rule names, or a
   counterexample built on inputs the code never receives is NON-BLOCKING, however it is worded.
2. A change only the orchestrator can make (the WP file's Goal, criteria or Out of scope, its owns, a split, another
   WP's paths, a project-wide rule) is an ORCHESTRATOR note, never a finding: it does not block, and the builder ignores it.
3. Every finding has an id, R<review>-F<n>, unique across all reviews of this WP. The ledger lists earlier findings, the
   builder's answers and earlier rulings; a review rules on every finding still open in it, and does not raise a settled
   point again under a new id.
4. The builder answers every open BLOCKING finding before `wpo verify` passes, with one of:
   - `wpo finding fixed <id> '<what changed, file:line>'`;
   - `wpo finding veto <id> <ground> '<evidence>'`, on exactly one ground:
{GROUND_LIST}
   A veto is the right answer to a finding that breaks rule 1 or 2: the builder never makes a change it does not owe,
   and does not argue a veto by redoing the work. NON-BLOCKING findings and ORCHESTRATOR notes need no answer.
5. The reviewer rules on each answer. It may reject a veto only by meeting rule 1 for that finding (the verbatim quote
   that says what the finding claims, or the reproduction with its output); otherwise the veto stands and the finding
   is closed. A fix that does not remove the defect is STILL-OPEN, with what still fails.
6. A veto the reviewer rejected may be vetoed once more only with new evidence. A finding still disputed after that goes
   to the re-evaluation, which reads this ledger."""

REVIEW_OUTPUT_FORMAT = """Output, in this order:
- For each finding still open in the ledger: 'RULING <id> RESOLVED|STILL-OPEN|VETO-ACCEPTED|VETO-REJECTED: <why>'.
- Each new finding, ranked by severity:
  'FINDING R{review}-F<n> BLOCKING|NON-BLOCKING <file:line>'
  '  Basis: <rule 1's basis; for NON-BLOCKING, why it matters>'
  '  Problem: <what is wrong>'
  '  Fix: <one-line direction>'
- Each orchestrator note: 'ORCHESTRATOR: <the change and why>'.
The last line must be exactly 'VERDICT: PASS' when no BLOCKING finding is open after your rulings (new BLOCKING findings,
STILL-OPEN and VETO-REJECTED rulings), otherwise 'VERDICT: CHANGES_REQUESTED'."""


@dataclass(frozen=True)
class ParsedReview:
    """What a review reported in the protocol's format."""

    findings: list[dict]
    rulings: list[dict]
    orchestrator_notes: list[str]


def ledger_path(run_dir: Path) -> Path:
    return run_dir / FINDINGS_FILE


def load(run_dir: Path) -> dict:
    path = ledger_path(run_dir)
    if not path.exists():
        return {"reviews": 0, "findings": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save(run_dir: Path, ledger: dict) -> None:
    path = ledger_path(run_dir)
    staging = path.with_suffix(".json.tmp")
    staging.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    # Retries while a reader (the dashboard, `wpo status`) has the ledger open for an instant.
    replace_file(staging, path)


def block_after(body: str, start: int) -> str:
    """The lines after a protocol line, up to the next protocol line."""
    following = SECTION_END.search(body, start)
    return body[start:following.start() if following else len(body)].strip()


def parse_review(body: str, review_number: int) -> ParsedReview:
    """The review's findings (only ids of this review count as new), rulings and orchestrator notes."""
    findings = []
    for match in FINDING_LINE.finditer(body):
        identifier, severity, location = match.groups()
        if not identifier.startswith(f"R{review_number}-"):
            continue
        findings.append({"id": identifier, "severity": severity, "location": location.strip(),
                         "text": block_after(body, match.end())})
    rulings = [{"id": identifier, "ruling": ruling, "why": why.strip()} for identifier, ruling, why in RULING_LINE.findall(body)]
    notes = [note.strip() for note in ORCHESTRATOR_LINE.findall(body)]
    return ParsedReview(findings, rulings, notes)


def is_open(finding: dict) -> bool:
    """A BLOCKING finding no ruling has closed."""
    return finding["severity"] == "BLOCKING" and finding["status"] in ("open", *OPEN_RULINGS)


def is_answered(finding: dict) -> bool:
    """The builder answered the finding after its latest ruling."""
    answer = finding.get("answer")
    return answer is not None and answer["after_review"] >= finding["status_review"]


def record_review(run_dir: Path, review_number: int, parsed: ParsedReview, passed: bool) -> list[dict]:
    """Applies a review to the ledger: its rulings on earlier findings, then its new findings. A PASS
    closes what is left. Returns the findings still open."""
    ledger = load(run_dir)
    by_id = {finding["id"]: finding for finding in ledger["findings"]}
    for ruling in parsed.rulings:
        finding = by_id.get(ruling["id"])
        if finding is None:
            continue
        finding["status"] = ruling["ruling"]
        finding["status_review"] = review_number
        finding["rulings"] = [*finding.get("rulings", []), {"review": review_number, "ruling": ruling["ruling"], "why": ruling["why"]}]
    for new in parsed.findings:
        if new["id"] in by_id:
            continue
        finding = {**new, "review": review_number, "status": "open", "status_review": review_number, "answer": None, "rulings": []}
        ledger["findings"].append(finding)
        by_id[finding["id"]] = finding
    if passed:
        for finding in ledger["findings"]:
            if is_open(finding):
                finding["status"] = "closed-by-pass"
                finding["status_review"] = review_number
    ledger["reviews"] = max(int(ledger["reviews"]), review_number)
    save(run_dir, ledger)
    return [finding for finding in ledger["findings"] if is_open(finding)]


def next_review_number(run_dir: Path) -> int:
    return int(load(run_dir)["reviews"]) + 1


def ledger_text(run_dir: Path) -> str:
    """The findings still open, with the builder's answers and earlier rulings, for a review or a re-evaluation."""
    open_findings = [finding for finding in load(run_dir)["findings"] if is_open(finding)]
    if not open_findings:
        return "The findings ledger has no open finding."
    entries = []
    for finding in open_findings:
        answer = finding.get("answer")
        answered = (f"  Builder: {answer['kind']}{' (' + answer['ground'] + ')' if answer.get('ground') else ''}: {answer['text']}"
                    if answer else "  Builder: no answer recorded")
        rulings = "".join(f"\n  Review {ruling['review']}: {ruling['ruling']}: {ruling['why']}" for ruling in finding.get("rulings", []))
        entries.append(f"- {finding['id']} BLOCKING {finding['location']} (raised in review {finding['review']})\n"
                       f"  {finding['text'].replace(chr(10), chr(10) + '  ')}{rulings}\n{answered}")
    return "Findings still open (rule on each):\n" + "\n".join(entries)


def builder_feedback(open_findings: list[dict], body: str) -> str:
    """What the builder gets after a review: the open findings to answer, the protocol and the full review."""
    identifiers = ", ".join(finding["id"] for finding in open_findings) or "none"
    return (f"The review requested changes. Answer each open BLOCKING finding ({identifiers}) with `wpo finding fixed` or "
            f"`wpo finding veto`; `wpo verify` passes only when all are answered.\n\n{REVIEW_PROTOCOL}\n\nThe review:\n{body}")


def unanswered(run_dir: Path) -> list[dict]:
    return [finding for finding in load(run_dir)["findings"] if is_open(finding) and not is_answered(finding)]


def answer(context: Context, identifier: str, kind: str, ground: str | None, text: str) -> str:
    """Records the builder's answer to an open BLOCKING finding (`wpo finding fixed|veto`)."""
    wp, _ = session_wp(context)
    if not FINDING_ID.fullmatch(identifier):
        raise SessionCommandError(f"'{identifier}' is not a finding id (R<review>-F<n>)")
    if kind == "veto" and ground not in VETO_GROUNDS:
        raise SessionCommandError(f"a veto names one ground: {', '.join(VETO_GROUNDS)}")
    if not text.strip():
        raise SessionCommandError("say what changed (fixed) or give the evidence (veto)")
    run_dir = context.project.wp_run_dir(wp.id)
    ledger = load(run_dir)
    finding = next((finding for finding in ledger["findings"] if finding["id"] == identifier), None)
    if finding is None:
        raise SessionCommandError(f"{wp.id} has no finding {identifier}")
    if not is_open(finding):
        raise SessionCommandError(f"{identifier} is not an open BLOCKING finding ({finding['severity']}, {finding['status']}): it needs no answer")
    earlier_vetoes = sum(1 for ruling in finding.get("rulings", []) if ruling["ruling"] == "VETO-REJECTED")
    if kind == "veto" and earlier_vetoes >= 2:
        raise SessionCommandError(f"two vetoes of {identifier} were rejected; fix it, or log the dispute and set 'status: blocked': the re-evaluation decides")
    finding["answer"] = {"kind": kind, "ground": ground, "text": text.strip(), "after_review": finding["status_review"],
                         "at": f"{datetime.now():%Y-%m-%d %H:%M}"}
    save(run_dir, ledger)
    left = [other["id"] for other in ledger["findings"] if is_open(other) and not is_answered(other)]
    return f"Recorded {kind} for {identifier}. " + (f"Still unanswered: {', '.join(left)}." if left else "Every open finding is answered.")

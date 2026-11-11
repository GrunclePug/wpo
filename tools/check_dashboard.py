"""Serve synthetic dashboard data for browser checks, without a WPO project or providers.

Run with ``uv run python -I -B tools/check_dashboard.py``. In a browser, open the
printed URL and evaluate ``runDashboardChecks()`` at each viewport.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from wpo import SKILL_ROOT


def fixture() -> dict:
    """Return synthetic snapshots covering long content and every operator action."""
    long_name = "split-pane-layout-with-a-very-long-unbroken-identifier-" * 3
    rows = []
    for index, status in enumerate(["in_progress", "review", "todo", "blocked", "blocked", "blocked"], 1):
        rows.append({
            "id": f"WP-{index:04}", "title": f"Synthetic {status} work package: {long_name}",
            "lane": "core" if index == 1 else long_name, "milestone": "M1",
            "status": status, "active": index == 1, "steps_done": 1, "steps_total": 3,
            "rounds": 2, "failures": 1, "evaluations": 0, "handoffs": 0,
            "latest_progress": f"2026-11-11 10:00 [builder] Synthetic progress {long_name}",
            "session": {"role": "builder", "route": long_name, "live": True,
                        "context_tokens": 12000, "handoff_tokens": 180000,
                        "native_task": {"link": "t3://synthetic"}} if index == 1 else None,
            "unmerged_deps": ["WP-0001"] if status == "todo" else [],
            "open_findings": [{"id": "R1-F1", "answered": False, "location": long_name}],
            "last_activity": 1794402000 - index, "held_by": "orchestrator" if status == "blocked" else None,
        })
    snapshot = {
        "project": {"name": "Synthetic dashboard fixture"}, "wps": rows,
        "lanes": [{"name": name, "state": state, "live": state == "running", "pool": False,
                   "hold": long_name if state == "needs-orchestrator" else None,
                   "current_wp": "WP-0001" if state == "running" else None,
                   "open_wps": ["WP-0001", "WP-0004"], "last_line": long_name,
                   "last_line_at": 1794402000, "sleep_until": None}
                  for name, state in [("core", "running"), (long_name, "needs-orchestrator")]],
        "approvals": [{"id": "synthetic-approval", "wp": "WP-0002", "role": "reviewer",
                       "route": long_name, "requested": "2026-11-11T10:00:00",
                       "command": f"Synthetic command {long_name}", "why": "Synthetic approval; no command runs."}],
        "parse_errors": {},
        "milestones": [{"name": long_name, "counts": {"done": 1, "review": 1, "in_progress": 1,
                        "changes_requested": 0, "blocked": 3, "todo": 1}, "total": 7}],
        "merged": [{"id": "WP-0000", "title": long_name, "merged_at": 1794402000, "commit": "1234567"}],
        "usage": {"fetched_at": 1794402000, "lead_minutes": 30,
                  "families": [{"family": "gpt", "plan": "fixture", "forecast": None,
                                "windows": [{"label": "7d", "used_percent": 42, "ceiling": 95,
                                             "paced": False, "resets_at": 1894402000}]}],
                  "cooldowns": [{"key": long_name, "until": 1894402000, "reason": long_name}]},
        "native_backend": {"coordinator_thread_id": "synthetic", "pending": 1, "stalled": 0,
                           "tasks": [{"wp": "WP-0001", "role": "builder", "state": "running",
                                      "link": "t3://synthetic", "detail": long_name}]},
    }
    detail = {
        "id": "WP-0004", "title": rows[3]["title"],
        "fields": {"status": "blocked", "lane": long_name, "milestone": "M1",
                   "builder": "synthetic", "reviewer": "synthetic", "depends_on": []},
        "file": long_name, "worktree": None, "ledger": {"history": []},
        "steps": [{"done": False, "text": long_name}],
        "progress": [{"at": "2026-11-11 10:00", "actor": "builder", "text": long_name}],
        "findings": {"reviews": 0, "findings": []}, "sections": {"Goal": long_name},
        "logs": [{"name": long_name + ".log", "path": "synthetic.log", "session": False,
                  "size": 400, "modified": 1794402000}], "reads_messages": False,
    }
    events = [{"index": index, "time": "2026-11-11T10:00:00Z", "kind": "synthetic-event",
               "severity": "attention" if index == 1 else "info", "wp": "WP-0004",
               "message": long_name} for index in range(2)]
    return {"snapshot": snapshot, "detail": detail, "events": events}


class FixtureHandler(BaseHTTPRequestHandler):
    """Serve only dashboard assets and browser fixtures; there are no action endpoints."""

    baseline: ClassVar[dict[str, str]] = {}

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/fixture.js":
            content = "window.fixture = " + json.dumps(fixture()) + ";\n"
            content += (Path(__file__).with_name("dashboard_fixture.js")).read_text(encoding="utf-8")
            mime = "text/javascript"
        elif path.removeprefix("/baseline") in ("/", "/dashboard.css", "/dashboard.js", "/theme.js"):
            baseline = path.startswith("/baseline/")
            route = path.removeprefix("/baseline")
            asset = "index.html" if route == "/" else route[1:]
            if baseline and asset not in self.baseline:
                self.send_error(404)
                return
            content = self.baseline[asset] if baseline else (SKILL_ROOT / "dashboard" / asset).read_text(encoding="utf-8")
            mime = "text/html" if route == "/" else "text/css" if route.endswith(".css") else "text/javascript"
            if route == "/":
                content = content.replace('<script src="/dashboard.js"', '<script src="/fixture.js"></script><script src="/dashboard.js"')
                if baseline:
                    content = content.replace('href="/dashboard.css"', 'href="/baseline/dashboard.css"').replace('src="/dashboard.js"', 'src="/baseline/dashboard.js"')
        else:
            self.send_error(404)
            return
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def check_browser(url: str, executable: str | None, evidence: Path) -> None:
    """Check exact CSS viewports and real keyboard input in an isolated Chromium context."""
    from playwright.sync_api import sync_playwright

    evidence.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable)
        try:
            context = browser.new_context(color_scheme="light")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            viewports = [(320, 800), (360, 800), (480, 800), (640, 800), (700, 800),
                         (768, 800), (1024, 800), (1440, 800), (480, 480), (360, 480)]
            for width, height in viewports:
                page.set_viewport_size({"width": width, "height": height})
                if FixtureHandler.baseline:
                    page.goto(url + "baseline/")
                    page.wait_for_function("typeof ui !== 'undefined' && ui.snapshot !== null")
                    metrics = page.evaluate("({page: document.documentElement.scrollWidth, width: innerWidth, needs: document.querySelector('#needs-you').getBoundingClientRect().height, tables: [...document.querySelectorAll('.grid')].map(t => t.scrollWidth)})")
                    page.screenshot(path=str(evidence / f"baseline-{width}-{height}.png"))
                    results.append({"baseline": [width, height], "metrics": metrics})
                page.goto(url)
                page.wait_for_function("typeof ui !== 'undefined' && ui.snapshot !== null")
                assert page.locator(".identity h1").evaluate("e => e.clientHeight <= parseFloat(getComputedStyle(e).lineHeight) * 3"), "Header controls squeeze the project title"
                result = page.evaluate("([width, height]) => runDashboardChecks(width, height)", [width, height])
                result["metrics"] = page.evaluate("({page: document.documentElement.scrollWidth, width: innerWidth, needs: document.querySelector('#needs-you').getBoundingClientRect().height, tables: [...document.querySelectorAll('.grid')].map(t => t.scrollWidth)})")
                results.append(result)
                page.screenshot(path=str(evidence / f"layout-{width}-{height}.png"), animations="disabled")
                page.locator("#work-packages").evaluate("e => e.scrollIntoView({block: 'start'})")
                page.screenshot(path=str(evidence / f"work-packages-{width}-{height}.png"), animations="disabled")
                page.locator('#wps-body [data-open-wp="WP-0004"]').click()
                page.wait_for_function("ui.detail !== null")
                page.screenshot(path=str(evidence / f"detail-{width}-{height}.png"), animations="disabled")
                page.locator("#detail-close").focus()
                page.keyboard.press("Escape")
                assert page.locator("#detail").is_hidden(), "Keyboard Escape did not close details"
                page.keyboard.press("/")
                assert page.locator("#wp-filter").evaluate("e => e === document.activeElement"), "Filter shortcut lost focus"
                page.locator("#needs-disclosure > summary").focus()
                page.keyboard.press("Enter")
                assert page.locator("#needs-disclosure").evaluate("e => e.open"), "Keyboard disclosure did not open"
                assert page.locator("#needs-disclosure > summary").evaluate("e => getComputedStyle(e).outlineStyle !== 'none'"), "Keyboard focus is not visible"
                page.keyboard.press("Tab")
                assert page.locator('#needs-you [data-open-wp="WP-0002"]').evaluate("e => e === document.activeElement"), "Tab order skipped the approval WP"
                page.keyboard.press("Tab")
                assert page.locator('#needs-you textarea').first.evaluate("e => e === document.activeElement"), "Tab order skipped the approval form"
                if width == 1440:
                    page.locator("#work-packages .table-wrap").focus()
                    page.keyboard.press("ArrowRight")
                    page.wait_for_function("document.querySelector('#work-packages .table-wrap').scrollLeft > 0")
            page.emulate_media(color_scheme="dark", reduced_motion="reduce")
            page.set_viewport_size({"width": 480, "height": 800})
            page.goto(url)
            page.wait_for_function("typeof ui !== 'undefined' && ui.snapshot !== null")
            results.append(page.evaluate("runDashboardChecks(480, 800)"))
            page.emulate_media(color_scheme="light")
            page.locator("#theme-choice").select_option("dark")
            assert page.evaluate("getComputedStyle(document.documentElement).colorScheme") == "dark"
            page.reload()
            page.wait_for_function("typeof ui !== 'undefined' && ui.snapshot !== null")
            assert page.locator("#theme-choice").input_value() == "dark", "Saved theme was lost on reload"
            assert page.evaluate("getComputedStyle(document.documentElement).colorScheme") == "dark"
            sibling = context.new_page()
            sibling.goto(url)
            assert sibling.locator("#theme-choice").input_value() == "dark", "Saved theme was lost in a new tab"
            page.locator("#theme-choice").select_option("light")
            sibling.wait_for_function("document.documentElement.dataset.theme === 'light'")
            sibling.close()
            page.locator("#theme-choice").select_option("system")
            assert page.evaluate("getComputedStyle(document.documentElement).colorScheme") == "light"
            page.emulate_media(color_scheme="dark")
            page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
            page.evaluate("localStorage.setItem('wpo.theme', 'invalid-choice')")
            page.reload()
            assert page.locator("#theme-choice").input_value() == "system", "Invalid saved theme was not ignored"
            page.screenshot(path=str(evidence / "saved-theme-dark.png"), animations="disabled")
            results.append({"theme": "saved choice, reload, new tab, cross-tab changes, system changes, invalid storage: passed"})
            page.screenshot(path=str(evidence / "dark-480.png"))
            assert not errors, errors
        finally:
            browser.close()
    (evidence / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Dashboard browser checks passed: {len(viewports)} viewports, dark/reduced motion, keyboard; evidence {evidence}")


def main() -> None:
    """Start a loopback-only fixture server for manual or automated browser checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8471)
    parser.add_argument("--check", action="store_true", help="Run Chromium checks (requires playwright)")
    parser.add_argument("--browser-executable", help="Optional installed Chromium executable")
    parser.add_argument("--evidence-dir", type=Path, help="Screenshots/results directory; defaults to a temporary directory")
    parser.add_argument("--baseline", help="Compare rendered assets from this Git revision (source checkout only)")
    arguments = parser.parse_args()
    if arguments.baseline:
        FixtureHandler.baseline = {name: subprocess.check_output(
            ["git", "show", f"{arguments.baseline}:dashboard/{name}"], cwd=SKILL_ROOT,
        ).decode("utf-8") for name in ("index.html", "dashboard.css", "dashboard.js")}
    with ThreadingHTTPServer(("127.0.0.1", arguments.port), FixtureHandler) as server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"Synthetic dashboard only: {url}", flush=True)
        if arguments.check:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                check_browser(url, arguments.browser_executable,
                              arguments.evidence_dir or Path(tempfile.mkdtemp(prefix="wpo-dashboard-evidence-")))
            finally:
                server.shutdown()
                worker.join()
            return
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

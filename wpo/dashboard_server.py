"""The dashboard's HTTP server: static pages from the skill's `dashboard/` directory, JSON views of
the run state, and a server-sent event stream that pushes the board and new journal events.

One assembly pass serves every open page: a pump thread rebuilds the snapshot every
`PUMP_SECONDS` while at least one page streams, and does nothing while none does, so an idle
server costs no CPU. Pages receive a snapshot only when it changed, and journal events as they
are appended.

It binds to 127.0.0.1 only and answers only requests addressed to that host and port (a web page
elsewhere cannot reach it through a rebound DNS name). Nothing a GET serves changes any state.

The user's actions (user_actions.py: unblock, approve, deny, message) are POSTs to `/api/action/*`.
Each must carry the server's random token, which only the page this server served holds (a
`<meta>` in index.html, sent back in the `X-WPO-Token` header), come from the page's own origin
(`Origin`), and be JSON: a page on another site can neither read the token nor send the header
without a preflight this server never answers. Actions run one at a time; each pushes a fresh
board to every page when it is done.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
import time
import traceback
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import SKILL_ROOT, user_actions, wpfile
from .context import Context
from .dashboard import code_version, log, process_created, read_state, state_path, url_for, write_state
from .dashboard_data import DashboardData, DashboardDataError
from .machine import MachineConfig
from .orchestrator import MainCommitError
from .platform import IS_WINDOWS, NamedMutex
from .project import Project, ProjectError
from .transcript import render_session

STATIC_DIRECTORY = SKILL_ROOT / "dashboard"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
}
PUMP_SECONDS = 2.5
HEARTBEAT_SECONDS = 15
# How often the server checks that the state file still names it (a launch race can overwrite it).
STATE_CHECK_SECONDS = 30
EVENT_BACKLOG = 200
RAW_TAIL_BYTES = 1024 * 1024
BIND_RETRY_SECONDS = 5
# The page's placeholder for the action token, replaced in index.html as it is served.
TOKEN_PLACEHOLDER = b"__WPO_ACTION_TOKEN__"
TOKEN_HEADER = "X-WPO-Token"
ACTION_PREFIX = "/api/action/"
MAX_ACTION_BYTES = 64 * 1024


class ActionRequestError(Exception):
    """A malformed action request: a missing or mistyped field, a body that is not a JSON object."""


def text_field(body: dict, name: str, required: bool = True) -> str:
    value = body.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ActionRequestError(f"'{name}' must be a string")
    return value


def run_unblock(context: Context, body: dict) -> str:
    return user_actions.unblock(context, text_field(body, "wp"), text_field(body, "note", required=False),
                                text_field(body, "status", required=False) or None)


def run_approve(context: Context, body: dict) -> str:
    return user_actions.answer_approval(context, text_field(body, "id"), True, text_field(body, "note", required=False))


def run_deny(context: Context, body: dict) -> str:
    return user_actions.answer_approval(context, text_field(body, "id"), False, text_field(body, "reason"))


def run_message(context: Context, body: dict) -> str:
    wp_id = body.get("wp")
    if wp_id is not None and not isinstance(wp_id, str):
        raise ActionRequestError("'wp' must be a WP id or null")
    return user_actions.send_message(context, wp_id or None, text_field(body, "text"))


def run_t3_recover(context: Context, body: dict) -> str:
    from .t3 import Bridge, T3Error
    if context.machine.execution_mode != "t3":
        raise ActionRequestError("This project is using the standalone backend")
    try:
        count = Bridge(context).recover()
    except T3Error as error:
        raise user_actions.UserActionError(str(error)) from error
    return f"Woke coordinators for {count} outstanding requests. No held lanes or replacement tasks were started."


# Action name -> what runs it. Each calls into user_actions, which calls the code `wpo` uses.
ACTIONS = {"unblock": run_unblock, "approve": run_approve, "deny": run_deny, "message": run_message, "t3-recover": run_t3_recover}


class Hub:
    """The latest snapshot and event batches, shared by every connected page. Access to the
    DashboardData goes through `data_lock`; it is not thread-safe. A whole pass, publication
    included, holds it, so passes from the pump and from a page's request never publish out of order.
    `url` and `code` are what the state file records for this server: the code it loaded at start."""

    def __init__(self, data: DashboardData, project: Project, url: str = "", code: str = "") -> None:
        self.data = data
        self.project = project
        self.url = url
        self.code = code
        self.data_lock = threading.Lock()
        self.changed = threading.Condition()
        self.streams = 0
        self.snapshot_body = b""
        self.snapshot_version = 0
        self.snapshot_at = 0.0
        # (sequence, events) batches appended since the server started; pages track the last sequence seen.
        self.batches: list[tuple[int, list[dict]]] = []
        self.batch_sequence = 0
        self.error: str | None = None
        # Only pages this server served know it; it changes with every server start.
        self.action_token = secrets.token_urlsafe(32)
        # Actions run one at a time: two unblocks or a message and an unblock must not interleave their edits.
        self.action_lock = threading.Lock()

    def action_context(self) -> Context:
        """A context of its own for one action: the pump's is not thread-safe, and an action's
        edits must read the project as it is now."""
        return Context.load(self.project.root, self.data.machine)

    def refresh(self) -> None:
        """One assembly pass; the snapshot version moves only when the board changed. A failure
        is shown to the pages and logged once per distinct error, not once per pass."""
        with self.data_lock:
            try:
                new_events = self.data.poll_events()
                body = json.dumps(self.data.snapshot(), sort_keys=True).encode("utf-8")
                error = None
            except Exception as failure:  # noqa: BLE001  (an unexpected state must not end the server; it is logged and shown)
                error = f"{type(failure).__name__}: {failure}"
                if error != self.error:
                    log(self.project, f"assembly failed: {error}\n{traceback.format_exc()}")
                new_events, body = [], None
            with self.changed:
                self.error = error
                if body is not None and body != self.snapshot_body:
                    self.snapshot_body = body
                    self.snapshot_version += 1
                if new_events:
                    self.batch_sequence += 1
                    self.batches = [*self.batches, (self.batch_sequence, new_events)][-64:]
                self.snapshot_at = time.monotonic()
                self.changed.notify_all()

    def ensure_fresh(self) -> None:
        if time.monotonic() - self.snapshot_at > PUMP_SECONDS or not self.snapshot_body:
            self.refresh()

    def pump(self) -> None:
        """Rebuilds the board while any page streams; waits for one while none does. Nothing that
        goes wrong in one turn ends the loop: a dead pump would freeze every page while the
        process, alive, kept autostart from replacing it."""
        last_state_check = time.monotonic()
        while True:
            with self.changed:
                if self.streams == 0:
                    self.changed.wait(STATE_CHECK_SECONDS)
                streaming = self.streams > 0
            try:
                if streaming:
                    self.refresh()
                if time.monotonic() - last_state_check > STATE_CHECK_SECONDS:
                    last_state_check = time.monotonic()
                    self.reclaim_state()
            except Exception as failure:  # noqa: BLE001  (see the docstring)
                log(self.project, f"pump turn failed: {type(failure).__name__}: {failure}\n{traceback.format_exc()}")
            if streaming:
                time.sleep(PUMP_SECONDS)

    def reclaim_state(self) -> None:
        """Rewrites the state file when it no longer names this server (a racing launch wrote it)."""
        state = read_state(self.project) or {}
        if state.get("pid") != os.getpid() or state.get("phase") != "serving":
            write_state(self.project, serving_state(self.url, self.code, state.get("launched_at")))

    def events_after(self, after_sequence: int) -> tuple[int, list[dict]]:
        events = [event for sequence, batch in self.batches if sequence > after_sequence for event in batch]
        return self.batch_sequence, events


def serving_state(url: str, code: str, launched_at: float | None) -> dict:
    return {"phase": "serving", "pid": os.getpid(), "created": process_created(os.getpid()) or 0, "url": url,
            "code": code, "launched_at": launched_at or time.time(), "serving_since": time.time()}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second socket take a port another process listens on; the
    # exclusive option makes a second server fail to bind instead.
    allow_reuse_address = not IS_WINDOWS

    def __init__(self, address: tuple[str, int], hub: Hub) -> None:
        self.hub = hub
        super().__init__(address, DashboardHandler)
        self.allowed_hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}

    def server_bind(self) -> None:
        if IS_WINDOWS:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    protocol_version = "HTTP/1.1"
    # A page that stops reading (a sleeping laptop) fails the next write instead of holding a thread.
    timeout = 60

    def log_request(self, code: object = "-", size: object = "-") -> None:
        pass  # a line per request would bury the errors in dashboard.log

    def log_error(self, format: str, *args: object) -> None:
        log(self.server.hub.project, "http: " + format % args)

    # ------------------------------------------------------------------------------------ routing

    def do_GET(self) -> None:
        if self.headers.get("Host") not in self.server.allowed_hosts:
            self.send_text(HTTPStatus.FORBIDDEN, "this dashboard answers only requests to 127.0.0.1 or localhost")
            return
        parts = urlsplit(self.path)
        route = parts.path
        query = parse_qs(parts.query, keep_blank_values=True)
        try:
            if route in STATIC_FILES:
                self.send_static(*STATIC_FILES[route])
            elif route.startswith(ACTION_PREFIX):
                self.refuse_method()
            elif route == "/api/state":
                self.send_state()
            elif route == "/api/stream":
                self.stream()
            elif route.startswith("/api/wp/"):
                self.send_detail(unquote(route.removeprefix("/api/wp/")))
            elif route.startswith("/view/"):
                self.send_view(unquote(route.removeprefix("/view/")))
            elif route.startswith("/raw/"):
                self.send_raw(unquote(route.removeprefix("/raw/")), "tail" in query)
            else:
                self.send_text(HTTPStatus.NOT_FOUND, f"nothing at {route}")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            pass  # the page went away
        except (DashboardDataError, FileNotFoundError, ValueError) as error:
            # Unknown WPs, paths outside the runs directory, a log removed meanwhile, a NUL in a path.
            self.send_text(HTTPStatus.NOT_FOUND, str(error))

    def do_POST(self) -> None:
        """The user's actions. Refused unless addressed to this host, sent by this server's page
        (its origin and its token) and JSON; then run one at a time."""
        # Refused bodies are never parsed or executed, and the connection is not reused.
        self.close_connection = True
        try:
            refusal = self.action_refusal()
            if refusal is not None:
                self.discard_refused_body()
                self.send_action_result(*refusal)
                return
            name = urlsplit(self.path).path.removeprefix(ACTION_PREFIX)
            try:
                body = self.read_action_body()
                action = ACTIONS[name]
            except ActionRequestError as error:
                self.send_action_result(HTTPStatus.BAD_REQUEST, str(error))
                return
            self.send_action_result(*self.run_action(name, action, body))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            pass  # the page went away; the action, if it ran, is journaled

    def do_PUT(self) -> None:
        self.refuse_method()

    def do_PATCH(self) -> None:
        self.refuse_method()

    def do_DELETE(self) -> None:
        self.refuse_method()

    # ------------------------------------------------------------------------------------ actions

    def discard_refused_body(self) -> None:
        """Discard a small framed body so closing the socket preserves the error response."""
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return
        if not 0 < length <= MAX_ACTION_BYTES or self.headers.get("Transfer-Encoding"):
            return
        previous_timeout = self.connection.gettimeout()
        deadline = time.monotonic() + 1
        remaining = length
        try:
            while remaining:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
                self.connection.settimeout(timeout)
                chunk = self.rfile.read1(remaining)
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, TimeoutError):
            pass
        finally:
            self.connection.settimeout(previous_timeout)

    def refuse_method(self) -> None:
        """Actions are POSTs; nothing else changes state, and a GET never runs one."""
        self.close_connection = True
        self.discard_refused_body()
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        body = b"actions are POST requests from the dashboard page"
        self.send_header("Allow", "POST" if urlsplit(self.path).path.startswith(ACTION_PREFIX) else "GET")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def action_refusal(self) -> tuple[HTTPStatus, str] | None:
        """Why this POST is not run: another host, another origin, no or a wrong token, an unknown action."""
        server = self.server
        if self.headers.get("Host") not in server.allowed_hosts:
            return HTTPStatus.FORBIDDEN, "this dashboard answers only requests to 127.0.0.1 or localhost"
        if self.headers.get("Origin") not in server.allowed_origins:
            return HTTPStatus.FORBIDDEN, "actions are taken only from this dashboard's own page (Origin)"
        token = self.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(token.encode("utf-8"), server.hub.action_token.encode("utf-8")):
            return HTTPStatus.FORBIDDEN, "the action token is missing or stale; reload the page"
        path = urlsplit(self.path).path
        if not path.startswith(ACTION_PREFIX) or path.removeprefix(ACTION_PREFIX) not in ACTIONS:
            return HTTPStatus.NOT_FOUND, f"no action at {path}"
        return None

    def read_action_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ActionRequestError("an action needs a Content-Length") from None
        if not 0 < length <= MAX_ACTION_BYTES:
            raise ActionRequestError(f"an action's body is 1 to {MAX_ACTION_BYTES} bytes, not {length}")
        payload = self.rfile.read(length)
        if self.headers.get_content_type() != "application/json":
            raise ActionRequestError("an action's body is JSON (Content-Type: application/json)")
        try:
            body = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ActionRequestError(f"the body is not JSON: {error}") from None
        if not isinstance(body, dict):
            raise ActionRequestError("the body is not a JSON object")
        return body

    def run_action(self, name: str, action: Callable[[Context, dict], str], body: dict) -> tuple[HTTPStatus, str]:
        """Runs one action and returns its outcome; the board is rebuilt and pushed after it, so
        every page sees what it changed."""
        hub = self.server.hub
        try:
            with hub.action_lock:
                outcome = action(hub.action_context(), body)
        except ActionRequestError as error:
            return HTTPStatus.BAD_REQUEST, str(error)
        except user_actions.UnknownTargetError as error:
            return HTTPStatus.NOT_FOUND, str(error)
        except user_actions.ActionConflictError as error:
            return HTTPStatus.CONFLICT, str(error)
        except (user_actions.UserActionError, wpfile.WorkPackageError) as error:
            return HTTPStatus.BAD_REQUEST, str(error)
        except (MainCommitError, ProjectError, OSError, TimeoutError) as error:
            log(hub.project, f"action {name} failed: {type(error).__name__}: {error}\n{traceback.format_exc()}")
            return HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(error).__name__}: {error}"
        except Exception as error:  # noqa: BLE001  (a bug must reach the page as text and the log with its traceback, not drop the connection)
            log(hub.project, f"action {name} crashed: {type(error).__name__}: {error}\n{traceback.format_exc()}")
            return HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(error).__name__}: {error} (traceback in dashboard.log)"
        hub.refresh()
        return HTTPStatus.OK, outcome

    def send_action_result(self, status: HTTPStatus, text: str) -> None:
        body = {"ok": True, "message": text} if status == HTTPStatus.OK else {"ok": False, "error": text}
        self.send_body(status, "application/json; charset=utf-8", json.dumps(body).encode("utf-8"))

    # ---------------------------------------------------------------------------------- responses

    def send_body(self, status: HTTPStatus, content_type: str, body: bytes, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, status: HTTPStatus, text: str) -> None:
        self.send_body(status, "text/plain; charset=utf-8", text.encode("utf-8"))

    def send_json(self, value: object) -> None:
        self.send_body(HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(value).encode("utf-8"))

    def send_static(self, name: str, content_type: str) -> None:
        body = (STATIC_DIRECTORY / name).read_bytes()
        if name == "index.html":
            # The token is in the page, never in a script or JSON another origin could load.
            body = body.replace(TOKEN_PLACEHOLDER, self.server.hub.action_token.encode("ascii"))
        self.send_body(HTTPStatus.OK, content_type, body, cache="no-store" if name == "index.html" else "no-cache")

    def send_state(self) -> None:
        """The board and the recent events in one response, for pages that cannot stream."""
        hub = self.server.hub
        hub.ensure_fresh()
        with hub.data_lock:
            backlog = hub.data.recent_events(EVENT_BACKLOG)
        with hub.changed:
            body, error = hub.snapshot_body, hub.error
        self.send_body(HTTPStatus.OK, "application/json; charset=utf-8",
                       b'{"snapshot":' + (body or b"null") + b',"events":' + json.dumps(backlog).encode("utf-8")
                       + b',"error":' + json.dumps(error).encode("utf-8") + b"}")

    def send_detail(self, wp_id: str) -> None:
        hub = self.server.hub
        with hub.data_lock:
            detail = hub.data.wp_detail(wp_id)
        self.send_json(detail)

    def send_view(self, relative: str) -> None:
        hub = self.server.hub
        with hub.data_lock:
            path = hub.data.run_file(relative)
        self.send_body(HTTPStatus.OK, "text/html; charset=utf-8", render_session(path).encode("utf-8"))

    def send_raw(self, relative: str, tail_only: bool) -> None:
        """A run file as text; `?tail` sends only its last megabyte. The file is read whole and
        closed before sending: a handle held while a slow page downloads would make the runner's
        replace or delete of that file fail on Windows."""
        hub = self.server.hub
        with hub.data_lock:
            path = hub.data.run_file(relative)
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - RAW_TAIL_BYTES) if tail_only else 0)
            body = handle.read()
        self.send_body(HTTPStatus.OK, "text/plain; charset=utf-8", body)

    def stream(self) -> None:
        """Server-sent events: `snapshot` whenever the board changes, `events` as the journal grows,
        `fault` when assembly fails, and a comment line as a heartbeat."""
        hub = self.server.hub
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        with hub.changed:
            hub.streams += 1
            hub.changed.notify_all()
        try:
            hub.ensure_fresh()
            # The sequence is taken before the backlog: an event published in between arrives twice
            # (pages drop indexes they hold), never not at all.
            with hub.changed:
                version, sequence, body, error = hub.snapshot_version, hub.batch_sequence, hub.snapshot_body, hub.error
            with hub.data_lock:
                backlog = hub.data.recent_events(EVENT_BACKLOG)
            self.send_event("snapshot", body)
            self.send_event("events", json.dumps({"events": backlog, "reset": True}).encode("utf-8"))
            self.send_event("fault", json.dumps({"error": error}).encode("utf-8"))
            sent_error, last_write = error, time.monotonic()
            while True:
                with hub.changed:
                    hub.changed.wait(HEARTBEAT_SECONDS)
                    new_version, body, error = hub.snapshot_version, hub.snapshot_body, hub.error
                    sequence, events = hub.events_after(sequence)
                wrote = False
                # The backlog and the first batch can share events; pages drop indexes they already hold.
                if events:
                    self.send_event("events", json.dumps({"events": events}).encode("utf-8"))
                    wrote = True
                if new_version != version:
                    version = new_version
                    self.send_event("snapshot", body)
                    wrote = True
                if error != sent_error:
                    sent_error = error
                    self.send_event("fault", json.dumps({"error": error}).encode("utf-8"))
                    wrote = True
                if not wrote and time.monotonic() - last_write >= HEARTBEAT_SECONDS:
                    # A write to a page that has gone fails here, which ends this stream.
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    wrote = True
                if wrote:
                    last_write = time.monotonic()
        finally:
            with hub.changed:
                hub.streams -= 1

    def send_event(self, name: str, data: bytes) -> None:
        self.wfile.write(b"event: " + name.encode("ascii") + b"\ndata: " + data + b"\n\n")
        self.wfile.flush()


def bind(hub: Hub, port: int) -> DashboardServer | OSError:
    """Binds the port, retrying for a few seconds: a replaced server's socket can take a moment
    to close after its process ended."""
    deadline = time.monotonic() + BIND_RETRY_SECONDS
    while True:
        try:
            return DashboardServer(("127.0.0.1", port), hub)
        except OSError as error:
            if time.monotonic() >= deadline:
                return error
            time.sleep(0.5)


def serve(project: Project, machine: MachineConfig) -> int:
    """Runs the project's dashboard server in this process until it is stopped."""
    mutex = NamedMutex(project.mutex_name("dashboard"))
    if not mutex.acquire(blocking=False):
        log(project, "another dashboard server holds this project's mutex; this one exits")
        return 0
    launched = (read_state(project) or {}).get("launched_at")
    url = url_for(machine, project)
    code = code_version()
    try:
        hub = Hub(DashboardData(project.root, machine), project, url, code)
        server = bind(hub, machine.dashboard_port(project.root))
        if isinstance(server, OSError):
            write_state(project, {"phase": "failed", "pid": os.getpid(), "error": f"cannot bind {url}: {server}", "failed_at": time.time(),
                                  "launched_at": launched, "code": code, "url": url})
            log(project, f"cannot bind {url}: {server}")
            return 1
        write_state(project, serving_state(url, code, launched))
        log(project, f"serving {url} (pid {os.getpid()})")
        threading.Thread(target=hub.pump, name="dashboard-pump", daemon=True).start()
        server.serve_forever(poll_interval=1.0)
        return 0
    finally:
        state = read_state(project)
        if state and state.get("pid") == os.getpid() and state.get("phase") == "serving":
            state_path(project).unlink(missing_ok=True)
        mutex.release()

"""Opt-in LAN binding keeps host, origin and action-token checks in force."""

from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_units import DashboardProject

from wpo.dashboard import ensure_running, read_state
from wpo.dashboard_data import DashboardData
from wpo.dashboard_server import DashboardServer, Hub, bind
from wpo.machine import DEFAULTS, MachineConfig
from wpo.project import Project


class DashboardNetworkConfigTests(unittest.TestCase):
    def machine(self, **network) -> MachineConfig:
        return MachineConfig({**DEFAULTS, "dashboard": {**DEFAULTS["dashboard"], **network}})

    def test_loopback_is_default_and_wildcard_needs_approved_addresses(self) -> None:
        self.assertEqual(self.machine().dashboard_host, "127.0.0.1")
        self.assertEqual(self.machine().dashboard_allowed_hosts, [])
        self.assertEqual(self.machine(host="0.0.0.0", allowed_hosts=["192.168.1.20"]).dashboard_host, "0.0.0.0")
        for settings in [{"host": "0.0.0.0"}, {"host": "attacker.example"},
                         {"allowed_hosts": ["attacker.example"]}, {"allowed_hosts": "192.168.1.20"}]:
            with self.subTest(settings=settings), self.assertRaisesRegex(ValueError, "dashboard"):
                machine = self.machine(**settings)
                _ = machine.dashboard_host, machine.dashboard_allowed_hosts

    def test_bind_uses_the_configured_address(self) -> None:
        with mock.patch("wpo.dashboard_server.DashboardServer") as server:
            bind(mock.sentinel.hub, 12345, "0.0.0.0")
        server.assert_called_once_with(("0.0.0.0", 12345), mock.sentinel.hub)

    def test_network_changes_replace_the_server(self) -> None:
        scratch = DashboardProject()
        project = Project.locate(scratch.root)
        default = MachineConfig({**scratch.machine.raw, "dashboard": {**DEFAULTS["dashboard"], "autostart": True}})
        lan = MachineConfig({**default.raw, "dashboard": {**default.raw["dashboard"], "host": "0.0.0.0", "allowed_hosts": ["192.168.1.20"]}})
        with (mock.patch("wpo.dashboard.code_version", return_value="same-code"),
              mock.patch("wpo.dashboard.server_alive", side_effect=lambda state: bool(state)),
              mock.patch("wpo.dashboard.process_created", return_value=1),
              mock.patch("wpo.dashboard.spawn_detached", return_value=100) as spawn,
              mock.patch("wpo.dashboard.stop_recorded_server") as stop):
            self.assertEqual(ensure_running(project, default, explicit=True), "started")
            self.assertEqual(ensure_running(project, default, explicit=True), "running")
            self.assertEqual(ensure_running(project, lan, explicit=True), "started")
            self.assertEqual(read_state(project)["network"]["host"], "0.0.0.0")
            self.assertEqual(ensure_running(project, lan, explicit=True), "running")
            other = MachineConfig({**lan.raw, "dashboard": {**lan.raw["dashboard"], "allowed_hosts": ["192.168.1.21"]}})
            self.assertEqual(ensure_running(project, other, explicit=True), "started")
            self.assertEqual(ensure_running(project, default, explicit=True), "started")
        self.assertEqual(spawn.call_count, 4)
        self.assertEqual(stop.call_count, 3)


class DashboardLANHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = DashboardProject()
        machine = MachineConfig({**scratch.machine.raw, "dashboard": {**DEFAULTS["dashboard"], "autostart": False,
                                                                      "host": "0.0.0.0", "allowed_hosts": ["192.168.1.20"]}})
        self.hub = Hub(DashboardData(scratch.root, machine), Project.locate(scratch.root))
        # Exercise real HTTP host headers without exposing a test server outside loopback.
        self.server = DashboardServer(("127.0.0.1", 0), self.hub)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method: str, *, host: str, origin: str | None = None, token: str | None = None) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        headers = {"Host": host}
        if origin is not None:
            headers["Origin"] = origin
        if token is not None:
            headers["X-WPO-Token"] = token
        body = None
        if method == "POST":
            body = json.dumps({"wp": None, "text": "LAN test"})
            headers["Content-Type"] = "application/json"
        connection.request(method, "/" if method == "GET" else "/api/action/message", body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_lan_and_loopback_pages_work_but_unknown_hosts_do_not(self) -> None:
        port = self.server.server_port
        for host in [f"192.168.1.20:{port}", f"127.0.0.1:{port}", f"localhost:{port}"]:
            status, body = self.request("GET", host=host)
            self.assertEqual(status, 200)
            self.assertIn(self.hub.action_token.encode(), body)
        for host in [f"attacker.example:{port}", f"192.168.1.99:{port}", "192.168.1.20:1"]:
            self.assertEqual(self.request("GET", host=host)[0], 403)

    def test_lan_actions_require_matching_origin_and_token(self) -> None:
        host = f"192.168.1.20:{self.server.server_port}"
        origin = f"http://{host}"
        with mock.patch("wpo.user_actions.send_message", return_value="received") as send:
            for sent_origin, token in [(None, self.hub.action_token), ("http://attacker.example", self.hub.action_token),
                                       (f"http://127.0.0.1:{self.server.server_port}", self.hub.action_token), (origin, None), (origin, "wrong")]:
                self.assertEqual(self.request("POST", host=host, origin=sent_origin, token=token)[0], 403)
            send.assert_not_called()
            self.assertEqual(self.request("POST", host=host, origin=origin, token=self.hub.action_token)[0], 200)
            send.assert_called_once()

    def test_reclaimed_state_preserves_the_actual_network_settings(self) -> None:
        self.hub.reclaim_state()
        self.assertEqual(read_state(self.hub.project)["network"], {"host": "0.0.0.0", "allowed_hosts": ["192.168.1.20"]})

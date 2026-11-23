from __future__ import annotations

import gzip
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from kelma.kelma_sync_v2 import client as client_module
from kelma.kelma_sync_v2.client import V2Client, V2Conflict, V2Error


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    connections: set[int] = set()
    close_after_reply = False

    def log_message(self, *_args) -> None:
        pass

    def _reply(self, status: int, payload: object, *, gzip_body: bool = False) -> None:
        body = json.dumps(payload).encode()
        if gzip_body:
            body = gzip.compress(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if gzip_body:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if type(self).close_after_reply:
            # Simulate an idle keep-alive connection the server has dropped.
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        type(self).connections.add(self.client_address[1])
        if self.path == "/v2/sync/manifest":
            self._reply(200, {"notes": [], "server_time": "t"}, gzip_body=True)
        elif self.path == "/v2/decks/missing":
            self._reply(404, {"error": "not_found", "message": "no such deck"})
        else:
            self._reply(200, {"path": self.path})

    def do_POST(self) -> None:  # noqa: N802
        type(self).connections.add(self.client_address[1])
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/v2/batch/push":
            self._reply(409, {"server": {"guid": "g"}, "client": request})
        else:
            self._reply(200, {"echo": request})


class KeepAliveClientTest(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.connections = set()
        _Handler.close_after_reply = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.client = V2Client(f"http://{host}:{port}", token="t", timeout=5)
        client_module._connections.pool = {}
        self.no_proxy = patch.object(client_module.urllib.request, "getproxies", return_value={})
        self.no_proxy.start()

    def tearDown(self) -> None:
        self.no_proxy.stop()
        for connection in getattr(client_module._connections, "pool", {}).values():
            connection.close()
        self.server.shutdown()
        self.server.server_close()

    def test_requests_share_one_connection_and_decode_gzip(self) -> None:
        self.assertEqual(self.client.manifest()["server_time"], "t")
        self.assertEqual(self.client.batch_pull(decks=["A"])["echo"]["decks"], ["A"])
        self.assertEqual(self.client.manifest()["notes"], [])
        self.assertEqual(len(_Handler.connections), 1)

    def test_a_dropped_idle_connection_is_retried_on_a_fresh_one(self) -> None:
        _Handler.close_after_reply = True
        self.assertEqual(self.client.manifest()["server_time"], "t")
        self.assertEqual(self.client.batch_pull(notes=["g"])["echo"]["notes"], ["g"])
        self.assertEqual(len(_Handler.connections), 2)

    def test_error_statuses_keep_their_exception_types(self) -> None:
        with self.assertRaises(V2Conflict) as conflict:
            self.client.batch_push({"notes": [{"guid": "g"}]})
        self.assertEqual(conflict.exception.server, {"guid": "g"})
        with self.assertRaises(V2Error) as missing:
            self.client.get_deck("missing")
        self.assertEqual(missing.exception.status, 404)
        self.assertEqual(str(missing.exception), "no such deck")

    def test_unreachable_server_is_a_connection_error(self) -> None:
        dead = V2Client("http://127.0.0.1:9", token="t", timeout=2)
        with self.assertRaises(V2Error) as failure:
            dead.manifest()
        self.assertEqual(failure.exception.status, 0)

    def test_a_proxy_uses_urllib(self) -> None:
        self.no_proxy.stop()
        with patch.object(
            client_module.urllib.request, "getproxies", return_value={"http": "http://proxy:3128"}
        ), patch.object(client_module.urllib.request, "proxy_bypass", return_value=False):
            self.assertFalse(self.client._can_keep_alive())
        self.no_proxy.start()


if __name__ == "__main__":
    unittest.main()

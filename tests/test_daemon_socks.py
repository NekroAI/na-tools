from __future__ import annotations

import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from na_tools.daemon.socks import SOCKS_REPLY_SUCCEEDED, Socks5Server, _send_reply


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, _format: str, *args: object) -> None:
        return


class _FakeSocket:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.chunks.append(data)


class DaemonSocksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
        self.http_thread = threading.Thread(
            target=self.http.serve_forever,
            name="test-http",
            daemon=True,
        )
        self.http_thread.start()
        self.socks = Socks5Server(
            bind_host="127.0.0.1",
            bind_port=0,
            http_host="127.0.0.1",
            http_port=self.http.server_port,
        )
        self.socks.start()
        assert self.socks.server_address is not None
        self.socks_address = self.socks.server_address

    def tearDown(self) -> None:
        self.socks.stop()
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(timeout=2)
        self.tmp.cleanup()

    def test_connect_na_tools_local_forwards_to_local_http_server(self) -> None:
        sock, reply_code = self._connect_domain("na-tools.local", 80)
        self.addCleanup(sock.close)

        self.assertEqual(reply_code, 0)
        sock.sendall(
            b"GET /v1/health HTTP/1.1\r\n"
            b"Host: na-tools.local\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        payload = self._read_all(sock)
        self.assertIn(b"HTTP/1.0 200 OK", payload)
        self.assertIn(b'{"ok":true}', payload)

    def test_public_domain_is_rejected(self) -> None:
        sock, reply_code = self._connect_domain("example.com", 80)
        self.addCleanup(sock.close)

        self.assertEqual(reply_code, 2)

    def test_loopback_wrong_port_is_rejected(self) -> None:
        sock, reply_code = self._connect_ipv4("127.0.0.1", 22)
        self.addCleanup(sock.close)

        self.assertEqual(reply_code, 2)

    def test_non_connect_command_is_rejected(self) -> None:
        sock, reply_code = self._connect_domain("na-tools.local", 80, command=2)
        self.addCleanup(sock.close)

        self.assertEqual(reply_code, 7)

    def _connect_domain(
        self,
        host: str,
        port: int,
        *,
        command: int = 1,
    ) -> tuple[socket.socket, int]:
        sock = self._negotiate()
        host_bytes = host.encode("idna")
        request = (
            bytes([5, command, 0, 3, len(host_bytes)])
            + host_bytes
            + port.to_bytes(2, "big")
        )
        sock.sendall(request)
        reply = self._read_exact(sock, 10)
        return sock, reply[1]

    def _connect_ipv4(self, host: str, port: int) -> tuple[socket.socket, int]:
        sock = self._negotiate()
        request = (
            bytes([5, 1, 0, 1])
            + socket.inet_aton(host)
            + port.to_bytes(2, "big")
        )
        sock.sendall(request)
        reply = self._read_exact(sock, 10)
        return sock, reply[1]

    def _negotiate(self) -> socket.socket:
        sock = socket.create_connection(self.socks_address, timeout=3)
        sock.sendall(bytes([5, 1, 0]))
        self.assertEqual(self._read_exact(sock, 2), bytes([5, 0]))
        return sock

    def _read_exact(self, sock: socket.socket, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = sock.recv(remaining)
            if not chunk:
                raise AssertionError("socket closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_all(self, sock: socket.socket) -> bytes:
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


class DaemonSocksReplyTest(unittest.TestCase):
    def test_send_reply_writes_complete_socks5_reply_once(self) -> None:
        sock = _FakeSocket()

        _send_reply(sock, SOCKS_REPLY_SUCCEEDED)  # type: ignore[arg-type]

        self.assertEqual(sock.chunks, [bytes([5, 0, 0, 1, 0, 0, 0, 0, 0, 0])])


if __name__ == "__main__":
    unittest.main()

"""SOCKS5 control-channel server for container-to-host daemon access."""

from __future__ import annotations

import ipaddress
import logging
import platform
import re
import select
import shutil
import socket
import socketserver
import subprocess
import threading
from dataclasses import dataclass
from typing import cast

from . import DEFAULT_SOCKS_BIND_HOST

SOCKS_VERSION = 0x05
SOCKS_CMD_CONNECT = 0x01
SOCKS_ATYP_IPV4 = 0x01
SOCKS_ATYP_DOMAIN = 0x03
SOCKS_ATYP_IPV6 = 0x04
SOCKS_REPLY_SUCCEEDED = 0x00
SOCKS_REPLY_RULESET_DENIED = 0x02
SOCKS_REPLY_NETWORK_UNREACHABLE = 0x03
SOCKS_REPLY_COMMAND_NOT_SUPPORTED = 0x07
SOCKS_REPLY_ADDRESS_TYPE_NOT_SUPPORTED = 0x08


@dataclass(frozen=True)
class SocksTarget:
    """A whitelist-approved SOCKS CONNECT destination."""

    requested_host: str
    requested_port: int
    connect_host: str
    connect_port: int


class SocksAccessPolicy:
    """Resolve only daemon-control targets and reject everything else."""

    def __init__(self, *, http_host: str, http_port: int) -> None:
        self.http_host = http_host
        self.http_port = http_port
        if http_host in {"0.0.0.0", "::", ""}:
            self._connect_host = "127.0.0.1"
        elif http_host == "localhost":
            self._connect_host = "127.0.0.1"
        else:
            self._connect_host = http_host

    def resolve(self, host: str, port: int) -> SocksTarget | None:
        """Return the approved connect target or None when denied."""

        normalized = host.strip().rstrip(".").lower()
        if normalized == "na-tools.local" and port == 80:
            return SocksTarget(
                requested_host=host,
                requested_port=port,
                connect_host=self._connect_host,
                connect_port=self.http_port,
            )
        if normalized in {"127.0.0.1", "localhost"} and port == self.http_port:
            return SocksTarget(
                requested_host=host,
                requested_port=port,
                connect_host=self._connect_host,
                connect_port=self.http_port,
            )
        return None


class Socks5Server:
    """Small threaded SOCKS5 CONNECT server with a daemon-only whitelist."""

    def __init__(
        self,
        *,
        bind_host: str,
        bind_port: int,
        http_host: str,
        http_port: int,
        logger: logging.Logger | None = None,
    ) -> None:
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.policy = SocksAccessPolicy(http_host=http_host, http_port=http_port)
        self.logger = logger or logging.getLogger(__name__)
        self._server: _ThreadingSocksServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def server_address(self) -> tuple[str, int] | None:
        """Return the actual socket bind address after start."""

        if self._server is None:
            return None
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        """Bind and serve in a background thread."""

        if self._server is not None:
            return
        server = _ThreadingSocksServer(
            (self.bind_host, self.bind_port),
            _SocksRequestHandler,
        )
        server.policy = self.policy
        server.logger = self.logger
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="na-tools-socks",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the background SOCKS server."""

        server = self._server
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None


class _ThreadingSocksServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    policy: SocksAccessPolicy
    logger: logging.Logger


class _SocksRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = cast(_ThreadingSocksServer, self.server)
        client = cast(socket.socket, self.request)
        client.settimeout(10)
        try:
            _handle_client(client, policy=server.policy, logger=server.logger)
        except (OSError, EOFError, ValueError) as exc:
            server.logger.debug("SOCKS client closed during handshake: %s", exc)


def _handle_client(
    client: socket.socket,
    *,
    policy: SocksAccessPolicy,
    logger: logging.Logger,
) -> None:
    version, nmethods = _read_exact(client, 2)
    if version != SOCKS_VERSION:
        return
    methods = _read_exact(client, nmethods)
    if 0x00 not in methods:
        client.sendall(bytes([SOCKS_VERSION, 0xFF]))
        return
    client.sendall(bytes([SOCKS_VERSION, 0x00]))

    version, command, _reserved, address_type = _read_exact(client, 4)
    if version != SOCKS_VERSION:
        return

    if address_type == SOCKS_ATYP_IPV4:
        host = str(ipaddress.IPv4Address(_read_exact(client, 4)))
    elif address_type == SOCKS_ATYP_DOMAIN:
        length = _read_exact(client, 1)[0]
        if length <= 0:
            _send_reply(client, SOCKS_REPLY_RULESET_DENIED)
            return
        host = _read_exact(client, length).decode("idna")
    elif address_type == SOCKS_ATYP_IPV6:
        host = str(ipaddress.IPv6Address(_read_exact(client, 16)))
    else:
        _send_reply(client, SOCKS_REPLY_ADDRESS_TYPE_NOT_SUPPORTED)
        return

    port = int.from_bytes(_read_exact(client, 2), "big")

    if command != SOCKS_CMD_CONNECT:
        logger.info("SOCKS denied non-CONNECT command=%s target=%s:%s", command, host, port)
        _send_reply(client, SOCKS_REPLY_COMMAND_NOT_SUPPORTED)
        return

    target = policy.resolve(host, port)
    if target is None:
        logger.info("SOCKS denied target=%s:%s reason=not_whitelisted", host, port)
        _send_reply(client, SOCKS_REPLY_RULESET_DENIED)
        return

    try:
        upstream = socket.create_connection(
            (target.connect_host, target.connect_port),
            timeout=10,
        )
    except OSError as exc:
        logger.info(
            "SOCKS failed target=%s:%s connect=%s:%s error=%s",
            target.requested_host,
            target.requested_port,
            target.connect_host,
            target.connect_port,
            exc.__class__.__name__,
        )
        _send_reply(client, SOCKS_REPLY_NETWORK_UNREACHABLE)
        return

    with upstream:
        _send_reply(client, SOCKS_REPLY_SUCCEEDED)
        _relay(client, upstream)


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    sockets = [client, upstream]
    for sock in sockets:
        sock.settimeout(None)
    while True:
        readable, _, _ = select.select(sockets, [], [], 60)
        if not readable:
            return
        for source in readable:
            chunk = source.recv(65536)
            if not chunk:
                return
            target = upstream if source is client else client
            target.sendall(chunk)


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError("connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_reply(sock: socket.socket, code: int) -> None:
    sock.sendall(bytes([SOCKS_VERSION, code, 0x00, SOCKS_ATYP_IPV4]))
    sock.sendall(socket.inet_aton("0.0.0.0"))
    sock.sendall((0).to_bytes(2, "big"))


def resolve_default_socks_bind_host() -> str:
    """Prefer the Docker bridge gateway on Linux, otherwise use 0.0.0.0."""

    detected = detect_docker_bridge_gateway()
    return detected or DEFAULT_SOCKS_BIND_HOST


def detect_docker_bridge_gateway() -> str | None:
    """Return the docker0 IPv4 address when it can be detected safely."""

    if platform.system().lower() != "linux":
        return None
    ip_cmd = shutil.which("ip")
    if not ip_cmd:
        return None
    try:
        result = subprocess.run(
            [ip_cmd, "-4", "addr", "show", "docker0"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/", result.stdout)
    if not match:
        return None
    address = ipaddress.ip_address(match.group(1))
    if not address.is_private and not address.is_loopback:
        return None
    return str(address)

"""HMAC request authentication for the daemon API."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections.abc import Callable

from fastapi import Request

from .errors import DaemonAPIError, auth_failed

TIMESTAMP_WINDOW_MS = 60_000
NONCE_TTL_SECONDS = 300


class NonceStore:
    """In-memory nonce replay cache with a fixed TTL."""

    def __init__(
        self,
        *,
        ttl_seconds: int = NONCE_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def mark_once(self, nonce: str) -> bool:
        """Return True when the nonce was not seen in the TTL window."""

        now = self._clock()
        cutoff = now - self._ttl_seconds
        with self._lock:
            expired = [key for key, ts in self._seen.items() if ts < cutoff]
            for key in expired:
                del self._seen[key]
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


class HMACAuthenticator:
    """Verify daemon API HMAC headers for the currently bound instance."""

    def __init__(
        self,
        *,
        instance_id: str,
        token_getter: Callable[[], bytes],
        nonce_store: NonceStore | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._instance_id = instance_id
        self._token_getter = token_getter
        self._nonce_store = nonce_store or NonceStore()
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    def verify(self, request: Request, body: bytes) -> None:
        """Verify the request or raise a structured API error."""

        instance = request.headers.get("X-NA-Instance")
        timestamp = request.headers.get("X-NA-Timestamp")
        nonce = request.headers.get("X-NA-Nonce")
        signature = request.headers.get("X-NA-Signature")

        if not instance or not timestamp or not nonce or not signature:
            raise auth_failed("missing HMAC authentication headers")

        if instance != self._instance_id:
            raise DaemonAPIError(
                403,
                "instance_mismatch",
                "request instance does not match the bound instance",
                details={"instance_id": instance},
            )

        try:
            timestamp_ms = int(timestamp)
        except ValueError as exc:
            raise auth_failed("invalid timestamp") from exc

        if abs(self._now_ms() - timestamp_ms) > TIMESTAMP_WINDOW_MS:
            raise auth_failed("timestamp is outside the allowed window")

        expected = self.sign(
            method=request.method,
            path_with_query=_path_with_query(request),
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        if not hmac.compare_digest(signature, expected):
            raise auth_failed("signature mismatch")

        if not self._nonce_store.mark_once(f"{instance}:{nonce}"):
            raise DaemonAPIError(401, "request_replayed", "nonce was already used")

    def sign(
        self,
        *,
        method: str,
        path_with_query: str,
        timestamp: str,
        nonce: str,
        body: bytes,
    ) -> str:
        """Return the v1 HMAC signature for tests and future clients."""

        body_hash = hashlib.sha256(body).hexdigest()
        plaintext = "\n".join(
            [method.upper(), path_with_query, timestamp, nonce, body_hash]
        )
        digest = hmac.new(
            self._token_getter(),
            plaintext.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"v1={digest}"


def _path_with_query(request: Request) -> str:
    path = request.url.path
    if request.url.query:
        return f"{path}?{request.url.query}"
    return path

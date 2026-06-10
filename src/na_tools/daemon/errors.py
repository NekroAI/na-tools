"""Daemon API error helpers."""

from __future__ import annotations

from typing import Any


class DaemonAPIError(Exception):
    """Structured daemon API error rendered as the protocol error shape."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}

    def payload(self) -> dict[str, dict[str, Any]]:
        """Return the frozen daemon protocol error payload."""

        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


def auth_failed(message: str = "authentication failed") -> DaemonAPIError:
    """Return a 401 auth error."""

    return DaemonAPIError(401, "auth_failed", message)


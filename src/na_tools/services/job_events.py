"""Structured update job events shared by CLI and daemon code."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

UpdatePhase = Literal[
    "validate_instance",
    "backup",
    "switch_channel",
    "pull_images",
    "restart_services",
    "pull_sandbox",
    "verify",
    "finished",
]

UpdateEventType = Literal["phase", "progress", "log", "warning", "result"]
UpdateLogLevel = Literal["info", "warning", "error", "success"]


@dataclass(frozen=True)
class UpdateEvent:
    """One structured event emitted by UpdateService."""

    type: UpdateEventType
    phase: UpdatePhase | None = None
    message: str | None = None
    level: UpdateLogLevel = "info"
    current: int | None = None
    total: int | None = None
    data: dict[str, Any] | None = None


class EventSink(Protocol):
    """Callable sink for update events."""

    def __call__(self, event: UpdateEvent) -> None:
        """Consume one event."""


def null_event_sink(_: UpdateEvent) -> None:
    """Default event sink used when callers do not need events."""


EventSinkFunc = Callable[[UpdateEvent], None]


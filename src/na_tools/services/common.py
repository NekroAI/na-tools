"""Shared service-layer primitives."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

ServiceEventLevel = Literal["info", "success", "warning", "error"]


@dataclass(frozen=True)
class ServiceEvent:
    """Human-facing progress event emitted by command services."""

    level: ServiceEventLevel
    message: str


EventSink = Callable[[ServiceEvent], None]


def null_event_sink(_event: ServiceEvent) -> None:
    """Ignore service events."""


@dataclass(frozen=True)
class ServiceError(RuntimeError):
    """Structured command-service failure."""

    code: str
    message: str
    details: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

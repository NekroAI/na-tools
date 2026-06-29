"""Reusable global configuration service."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.platform import get_global_mirror, set_global_mirror


@dataclass
class ConfigService:
    """Manage global na-tools configuration values."""

    def get_mirror(self) -> str:
        return get_global_mirror()

    def set_mirror(self, value: str) -> None:
        set_global_mirror(value)

"""Reusable service layer for na-tools commands and daemon integrations."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

_EXPORT_MODULES = {
    "BackupRequest": ".backup_service",
    "BackupResult": ".backup_service",
    "BackupService": ".backup_service",
    "BackupServiceError": ".backup_service",
    "ConfigService": ".config_service",
    "DaemonRootServiceManager": ".daemon_service",
    "DaemonRootServiceResult": ".daemon_service",
    "DaemonService": ".daemon_service",
    "DaemonServiceError": ".daemon_service",
    "DaemonStatus": ".daemon_service",
    "InstallRequest": ".install_service",
    "InstallResult": ".install_service",
    "InstallService": ".install_service",
    "InstallServiceError": ".install_service",
    "InstanceService": ".instance_service",
    "InstanceServiceError": ".instance_service",
    "NapcatService": ".napcat_service",
    "NapcatServiceError": ".napcat_service",
    "OrchestrationRequest": ".orchestration_service",
    "OrchestrationResult": ".orchestration_service",
    "OrchestrationService": ".orchestration_service",
    "OrchestrationServiceError": ".orchestration_service",
    "RemoveRequest": ".remove_service",
    "RemoveResult": ".remove_service",
    "RemoveService": ".remove_service",
    "RemoveServiceError": ".remove_service",
    "RestoreRequest": ".restore_service",
    "RestoreResult": ".restore_service",
    "RestoreService": ".restore_service",
    "RestoreServiceError": ".restore_service",
    "UpdateRequest": ".update_service",
    "UpdateResult": ".update_service",
    "UpdateService": ".update_service",
    "UpdateServiceError": ".update_service",
    "UpgradeCheckResult": ".upgrade_service",
    "UpgradeResult": ".upgrade_service",
    "UpgradeService": ".upgrade_service",
    "UpgradeServiceError": ".upgrade_service",
}

__all__ = list(_EXPORT_MODULES)


def __getattr__(name: str) -> Any:
    if name not in _EXPORT_MODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(_EXPORT_MODULES[name], __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


if TYPE_CHECKING:
    from .backup_service import BackupRequest, BackupResult, BackupService, BackupServiceError
    from .config_service import ConfigService
    from .daemon_service import (
        DaemonRootServiceManager,
        DaemonRootServiceResult,
        DaemonService,
        DaemonServiceError,
        DaemonStatus,
    )
    from .install_service import InstallRequest, InstallResult, InstallService, InstallServiceError
    from .instance_service import InstanceService, InstanceServiceError
    from .napcat_service import NapcatService, NapcatServiceError
    from .orchestration_service import (
        OrchestrationRequest,
        OrchestrationResult,
        OrchestrationService,
        OrchestrationServiceError,
    )
    from .remove_service import RemoveRequest, RemoveResult, RemoveService, RemoveServiceError
    from .restore_service import RestoreRequest, RestoreResult, RestoreService, RestoreServiceError
    from .update_service import UpdateRequest, UpdateResult, UpdateService, UpdateServiceError
    from .upgrade_service import (
        UpgradeCheckResult,
        UpgradeResult,
        UpgradeService,
        UpgradeServiceError,
    )

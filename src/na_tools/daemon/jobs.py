"""Daemon job management and UpdateService execution."""

from __future__ import annotations

import importlib
import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import click

from ..commands.backup import backup as backup_command
from ..commands.backup import parse_backup_name
from ..core.platform import get_global_config_dir
from ..services.job_events import UpdateEvent
from ..services.update_service import (
    BackupRequest,
    RestoreRequest,
    UpdateRequest,
    UpdateResult,
    UpdateService,
    UpdateServiceError,
)
from .errors import DaemonAPIError
from .instances import InstanceRegistry, unavailable_reason_message
from .logs import LogStore

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
ACTIVE_STATUSES = {"queued", "running", "cancel_requested"}
BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class UpdateExecutor(Protocol):
    """Subset of UpdateService used by JobManager."""

    def run(self, request: UpdateRequest, sink: Any) -> UpdateResult:
        """Run an update request and emit events."""


class JobManager:
    """Create, persist, and execute daemon jobs."""

    def __init__(
        self,
        *,
        registry: InstanceRegistry,
        log_store: LogStore,
        update_service_factory: Any | None = None,
    ) -> None:
        self._registry = registry
        self._log_store = log_store
        self._update_service_factory = update_service_factory or create_update_service
        self._jobs: dict[str, dict[str, Any]] = {}
        self._client_request_index: dict[str, str] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="na-daemon")
        self._load_jobs()

    @property
    def log_store(self) -> LogStore:
        return self._log_store

    def shutdown(self) -> None:
        """Stop the background executor."""

        self._executor.shutdown(wait=True, cancel_futures=True)

    def create_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a new update job or return an idempotent existing job."""

        self._validate_update_payload(payload)
        return self._create_job(
            "update",
            self._request_from_payload(payload),
            payload,
            runner=self._run_update_job,
        )

    def list_backups(
        self,
        *,
        name: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List safe backup summaries for the bound instance."""

        if name is not None:
            self._validate_backup_name(name)
        backup_dir = _backup_dir_for(self._registry.data_dir)
        backups = sorted(
            backup_dir.glob("*.tar.gz") if backup_dir.exists() else [],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if name is not None:
            backups = [path for path in backups if parse_backup_name(path.name) == name]
        return {
            "backups": [_backup_summary(path) for path in backups[: max(1, min(limit, 100))]]
        }

    def create_backup(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a manual backup job."""

        self._validate_common_payload(payload)
        name = payload.get("name")
        if name in ("", None):
            name = None
        if name is not None:
            if not isinstance(name, str):
                raise DaemonAPIError(400, "invalid_backup_name", "backup name must be a string")
            self._validate_backup_name(name)
        return self._create_job(
            "backup",
            {"name": name},
            payload,
            runner=self._run_backup_job,
        )

    def create_restore(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a restore job from a selected backup."""

        self._validate_common_payload(payload)
        backup_id = payload.get("backup_id")
        if not isinstance(backup_id, str) or not backup_id:
            raise DaemonAPIError(400, "invalid_backup_id", "backup_id is required")
        backup_file = self._backup_file_from_id(backup_id)
        return self._create_job(
            "restore",
            {"backup_id": backup_file.name},
            payload,
            runner=self._run_restore_job,
        )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def get_job_or_error(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            raise DaemonAPIError(404, "job_not_found", "job was not found")
        return job

    def summary(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": job["job_id"],
            "type": job.get("type", "update"),
            "status": job["status"],
            "phase": job["phase"],
            "message": _status_message(job.get("type", "update"), job["status"]),
        }

    def _create_job(
        self,
        job_type: str,
        request: dict[str, Any],
        payload: dict[str, Any],
        *,
        runner: Any,
    ) -> dict[str, Any]:
        client_request_id = payload.get("client_request_id")
        if isinstance(client_request_id, str) and client_request_id:
            with self._lock:
                existing_id = self._client_request_index.get(client_request_id)
                if existing_id and existing_id in self._jobs:
                    return self.summary(self._jobs[existing_id])

        unavailable_reason = self._registry.unavailable_reason()
        if unavailable_reason is not None:
            raise DaemonAPIError(
                503,
                unavailable_reason,
                unavailable_reason_message(unavailable_reason),
            )

        with self._lock:
            conflict = self._active_job_locked()
            if conflict is not None:
                raise DaemonAPIError(
                    409,
                    "job_conflict",
                    "another deployment job is already running",
                    details={"job_id": conflict["job_id"], "type": conflict.get("type")},
                )

            job_id = self._new_job_id()
            now = _utc_now()
            job = {
                "job_id": job_id,
                "type": job_type,
                "instance_id": self._registry.instance_id,
                "status": "queued",
                "phase": "validate_instance",
                "request": request,
                "progress": None,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "error": None,
                "result": None,
                "client_request_id": client_request_id,
                "requested_by": payload.get("requested_by"),
            }
            self._jobs[job_id] = job
            if isinstance(client_request_id, str) and client_request_id:
                self._client_request_index[client_request_id] = job_id
            self._persist_job_locked(job)
            self._log_store.append(
                job_id,
                level="info",
                stream="system",
                line=f"{job_type} job queued",
                event="job",
                data={"status": "queued", "phase": "validate_instance"},
            )
            self._executor.submit(runner, job_id)
            return self.summary(job)

    def _run_update_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["status"] = "running"
            job["started_at"] = _utc_now()
            self._persist_job_locked(job)
        self._log_store.append(
            job_id,
            level="info",
            stream="system",
            line="update job started",
            event="job",
            data={"status": "running", "phase": "validate_instance"},
        )

        request = self._update_request_for(job_id)

        try:
            service: UpdateExecutor = self._update_service_factory()
            result = service.run(request, self._event_sink(job_id))
        except UpdateServiceError as exc:
            self._fail_job(
                job_id,
                {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
                phase=exc.phase,
            )
        except Exception as exc:
            self._fail_job(
                job_id,
                {
                    "code": "daemon_unavailable",
                    "message": str(exc),
                    "details": {},
                },
            )
        else:
            result_payload = _serialize_update_result(result)
            with self._lock:
                job = self._jobs[job_id]
                job["status"] = "succeeded"
                job["phase"] = "finished"
                job["progress"] = {
                    "current": 8,
                    "total": 8,
                    "label": "update workflow finished",
                }
                job["finished_at"] = _utc_now()
                job["exit_code"] = 0
                job["result"] = result_payload
                self._persist_job_locked(job)
            self._log_store.append(
                job_id,
                level="info",
                stream="system",
                line="update job succeeded",
                event="result",
                data={"status": "succeeded", "result": result_payload},
            )

    def _run_backup_job(self, job_id: str) -> None:
        self._start_job(job_id)
        try:
            request = self._backup_request_for(job_id)
            self._emit_simple_progress(job_id, "backup", "正在创建手动备份...", 1, 2)
            backup_file = _daemon_backup_runner(request)
            if backup_file is None:
                raise UpdateServiceError(
                    "backup_failed",
                    "backup command did not create a backup file",
                    phase="backup",
                    details={"name": request.name or ""},
                )
            result_payload = {"backup": _backup_summary(backup_file)}
            self._emit_simple_progress(job_id, "finished", "备份流程已完成。", 2, 2)
            self._succeed_job(job_id, result_payload, "backup job succeeded")
        except UpdateServiceError as exc:
            self._fail_job(
                job_id,
                {"code": exc.code, "message": exc.message, "details": exc.details},
                phase=exc.phase,
            )
        except Exception as exc:
            self._fail_job(
                job_id,
                {"code": "daemon_unavailable", "message": str(exc), "details": {}},
            )

    def _run_restore_job(self, job_id: str) -> None:
        self._start_job(job_id)
        try:
            request = self._restore_request_for(job_id)
            self._emit_simple_progress(job_id, "backup", "正在恢复备份数据...", 1, 3)
            _daemon_restore_runner(request)
            self._emit_simple_progress(job_id, "restart_services", "恢复流程已处理服务启动。", 2, 3)
            self._emit_simple_progress(job_id, "finished", "恢复流程已完成。", 3, 3)
            self._succeed_job(
                job_id,
                {"backup": _backup_summary(request.backup_file)},
                "restore job succeeded",
            )
        except UpdateServiceError as exc:
            self._fail_job(
                job_id,
                {"code": exc.code, "message": exc.message, "details": exc.details},
                phase=exc.phase,
            )
        except Exception as exc:
            self._fail_job(
                job_id,
                {"code": "daemon_unavailable", "message": str(exc), "details": {}},
            )

    def _start_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["status"] = "running"
            job["started_at"] = _utc_now()
            self._persist_job_locked(job)
        self._log_store.append(
            job_id,
            level="info",
            stream="system",
            line=f"{self._jobs[job_id].get('type', 'deployment')} job started",
            event="job",
            data={"status": "running", "phase": "validate_instance"},
        )

    def _emit_simple_progress(
        self,
        job_id: str,
        phase: str,
        message: str,
        current: int,
        total: int,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["phase"] = phase
            job["progress"] = {"current": current, "total": total, "label": message}
            self._persist_job_locked(job)
        self._log_store.append(
            job_id,
            level="info",
            stream="system",
            line=message,
            event="job",
            data={"status": "running", "phase": phase},
        )
        self._log_store.append(
            job_id,
            level="info",
            stream="progress",
            line=message,
            event="progress",
            data={"current": current, "total": total, "label": message},
        )
        self._log_store.append(
            job_id,
            level="info",
            stream="system",
            line=message,
            event="log",
        )

    def _succeed_job(self, job_id: str, result_payload: dict[str, Any], line: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["status"] = "succeeded"
            job["phase"] = "finished"
            job["finished_at"] = _utc_now()
            job["exit_code"] = 0
            job["result"] = result_payload
            self._persist_job_locked(job)
        self._log_store.append(
            job_id,
            level="info",
            stream="system",
            line=line,
            event="result",
            data={"status": "succeeded", "result": result_payload},
        )

    def _event_sink(self, job_id: str) -> Any:
        def sink(event: UpdateEvent) -> None:
            message = event.message or event.type
            if event.type == "phase":
                with self._lock:
                    job = self._jobs[job_id]
                    job["phase"] = event.phase
                    self._persist_job_locked(job)
                self._log_store.append(
                    job_id,
                    level=event.level,
                    stream="system",
                    line=message,
                    event="job",
                    data={"status": "running", "phase": event.phase},
                )
            elif event.type == "progress":
                progress = {
                    "current": event.current,
                    "total": event.total,
                    "label": message,
                }
                with self._lock:
                    job = self._jobs[job_id]
                    job["progress"] = progress
                    self._persist_job_locked(job)
                self._log_store.append(
                    job_id,
                    level=event.level,
                    stream="progress",
                    line=message,
                    event="progress",
                    data=progress,
                )
            elif event.type in {"log", "warning"}:
                self._log_store.append(
                    job_id,
                    level=event.level,
                    stream="system",
                    line=message,
                    event="log",
                )
            elif event.type == "result":
                return

        return sink

    def _fail_job(
        self,
        job_id: str,
        error: dict[str, Any],
        *,
        phase: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["status"] = "failed"
            if phase is not None:
                job["phase"] = phase
            job["finished_at"] = _utc_now()
            job["exit_code"] = 1
            job["error"] = error
            self._persist_job_locked(job)
        self._log_store.append(
            job_id,
            level="error",
            stream="system",
            line=error["message"],
            event="result",
            data={"status": "failed", "error": error},
        )

    def _update_request_for(self, job_id: str) -> UpdateRequest:
        with self._lock:
            request = dict(self._jobs[job_id]["request"])
        return UpdateRequest(
            data_dir=self._registry.data_dir,
            channel=request["channel"],
            backup=bool(request["backup"]),
            update_sandbox=bool(request["update_sandbox"]),
            update_cc_sandbox=bool(request["update_cc_sandbox"]),
            restore_pre_preview=bool(request["restore_pre_preview"]),
        )

    def _backup_request_for(self, job_id: str) -> BackupRequest:
        with self._lock:
            request = dict(self._jobs[job_id]["request"])
        return BackupRequest(
            data_dir=self._registry.data_dir,
            name=request.get("name") if isinstance(request.get("name"), str) else None,
            no_restart=False,
        )

    def _restore_request_for(self, job_id: str) -> RestoreRequest:
        with self._lock:
            request = dict(self._jobs[job_id]["request"])
        backup_id = request.get("backup_id")
        if not isinstance(backup_id, str):
            raise UpdateServiceError(
                "backup_not_found",
                "invalid backup id",
                phase="backup",
            )
        return RestoreRequest(
            data_dir=self._registry.data_dir,
            backup_file=self._backup_file_from_id(backup_id),
        )

    def _request_from_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "channel": payload.get("channel", "stable"),
            "backup": bool(payload.get("backup", True)),
            "update_sandbox": bool(payload.get("update_sandbox", True)),
            "update_cc_sandbox": bool(payload.get("update_cc_sandbox", False)),
            "restore_pre_preview": bool(payload.get("restore_pre_preview", False)),
        }

    def _validate_update_payload(self, payload: dict[str, Any]) -> None:
        self._validate_common_payload(payload)
        channel = payload.get("channel", "stable")
        if channel not in {"stable", "preview", "rollback"}:
            raise DaemonAPIError(
                400,
                "invalid_channel",
                "unsupported update channel",
                details={"channel": channel},
            )

    def _validate_common_payload(self, payload: dict[str, Any]) -> None:
        if payload.get("instance_id") != self._registry.instance_id:
            raise DaemonAPIError(
                403,
                "instance_mismatch",
                "request instance does not match the bound instance",
                details={"instance_id": payload.get("instance_id")},
            )

    def _validate_backup_name(self, name: str) -> None:
        if not BACKUP_NAME_RE.fullmatch(name):
            raise DaemonAPIError(
                400,
                "invalid_backup_name",
                "backup name may only contain letters, numbers, dot, underscore, and dash",
            )

    def _backup_file_from_id(self, backup_id: str) -> Path:
        if (
            "/" in backup_id
            or "\\" in backup_id
            or backup_id in {".", ".."}
            or Path(backup_id).is_absolute()
            or not backup_id.endswith(".tar.gz")
        ):
            raise DaemonAPIError(400, "invalid_backup_id", "invalid backup_id")

        backup_dir = _backup_dir_for(self._registry.data_dir).resolve()
        backup_file = (backup_dir / backup_id).resolve()
        if not backup_file.is_relative_to(backup_dir):
            raise DaemonAPIError(400, "invalid_backup_id", "invalid backup_id")
        if not backup_file.is_file():
            raise DaemonAPIError(404, "backup_not_found", "backup was not found")
        return backup_file

    def _load_jobs(self) -> None:
        self._registry.paths.jobs_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(self._registry.paths.jobs_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get("job_id"), str):
                continue
            if data.get("status") in ACTIVE_STATUSES:
                data["status"] = "failed"
                data["finished_at"] = _utc_now()
                data["exit_code"] = 1
                data["error"] = {
                    "code": "daemon_unavailable",
                    "message": "daemon restarted before the job finished",
                    "details": {},
                }
            self._jobs[data["job_id"]] = data
            client_request_id = data.get("client_request_id")
            if isinstance(client_request_id, str) and client_request_id:
                self._client_request_index[client_request_id] = data["job_id"]
            self._persist_job_locked(data)

    def _active_job_locked(self) -> dict[str, Any] | None:
        for job in self._jobs.values():
            if job.get("status") in ACTIVE_STATUSES:
                return job
        return None

    def _persist_job_locked(self, job: dict[str, Any]) -> None:
        path = self._job_path(job["job_id"])
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(job, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(path)

    def _job_path(self, job_id: str) -> Path:
        return self._registry.paths.jobs_dir / f"{job_id}.json"

    def _new_job_id(self) -> str:
        return "upd_" + uuid.uuid4().hex.upper()


def create_update_service() -> UpdateService:
    """Create the real daemon update service adapter."""

    return UpdateService(
        backup_runner=_daemon_backup_runner,
        restore_runner=_daemon_restore_runner,
        restore_runner_restarts_service=True,
    )


def _daemon_backup_runner(request: BackupRequest) -> Path | None:
    backup_dir = get_global_config_dir() / "backup" / request.data_dir.name
    before = set(backup_dir.glob("*.tar.gz")) if backup_dir.exists() else set()
    args = ["--data-dir", str(request.data_dir)]
    if request.no_restart:
        args.append("--no-restart")
    if request.name:
        args.extend(["--name", request.name])
    try:
        backup_command.main(
            args=args,
            prog_name="na-tools backup",
            standalone_mode=False,
        )
    except click.Abort as exc:
        raise UpdateServiceError(
            "backup_failed",
            "backup command was aborted",
            phase="backup",
            details={"name": request.name or ""},
        ) from exc

    backups = sorted(
        backup_dir.glob("*.tar.gz") if backup_dir.exists() else [],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for backup_file in backups:
        if request.name and parse_backup_name(backup_file.name) != request.name:
            continue
        if backup_file not in before:
            return backup_file
    return backups[0] if backups else None


def _daemon_restore_runner(request: RestoreRequest) -> None:
    restore_module = importlib.import_module("na_tools.commands.restore")
    old_confirm = restore_module.confirm
    restore_module.confirm = lambda *_args, **_kwargs: True
    try:
        restore_module.restore.main(
            args=[str(request.backup_file), "--data-dir", str(request.data_dir)],
            prog_name="na-tools restore",
            standalone_mode=False,
        )
    except click.Abort as exc:
        raise UpdateServiceError(
            "backup_failed",
            "restore command was aborted",
            phase="backup",
            details={"backup_file": str(request.backup_file)},
        ) from exc
    finally:
        restore_module.confirm = old_confirm


def _serialize_update_result(result: UpdateResult) -> dict[str, Any]:
    return {
        "channel": result.channel,
        "image": result.image,
        "image_tag": result.image_tag,
        "backup_file": str(result.backup_file) if result.backup_file else None,
        "app_health": "ok" if result.app_health.ok else "failed",
        "warnings": list(result.warnings),
    }


def _backup_dir_for(data_dir: Path) -> Path:
    return get_global_config_dir() / "backup" / data_dir.name


def _backup_summary(backup_file: Path) -> dict[str, Any]:
    stat = backup_file.stat()
    return {
        "backup_id": backup_file.name,
        "filename": backup_file.name,
        "name": parse_backup_name(backup_file.name),
        "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "size_bytes": stat.st_size,
    }


def _status_message(job_type: str, status: str) -> str:
    label = job_type if job_type in {"update", "backup", "restore"} else "deployment"
    return {
        "queued": f"{label} job queued",
        "running": f"{label} job running",
        "succeeded": f"{label} job succeeded",
        "failed": f"{label} job failed",
        "cancel_requested": "cancel requested",
        "cancelled": f"{label} job cancelled",
    }.get(status, status)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

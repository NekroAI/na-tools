"""FastAPI application factory for the na-tools daemon."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import __version__
from ..core.docker import DockerEnv
from . import (
    DEFAULT_BIND_HOST,
    DEFAULT_BIND_PORT,
    DEFAULT_SOCKS_BIND_HOST,
    DEFAULT_SOCKS_BIND_PORT,
    PROTOCOL_VERSION,
)
from .auth import HMACAuthenticator
from .errors import DaemonAPIError
from .instances import InstanceRegistry
from .jobs import JobManager, TERMINAL_STATUSES
from .logs import LogStore
from .socks import Socks5Server


def create_app(
    data_dir: str | Path,
    *,
    host: str = DEFAULT_BIND_HOST,
    port: int = DEFAULT_BIND_PORT,
    socks_host: str = DEFAULT_SOCKS_BIND_HOST,
    socks_port: int = DEFAULT_SOCKS_BIND_PORT,
    enable_socks: bool = False,
    docker_factory: Callable[[], Any] = DockerEnv,
    update_service_factory: Callable[[], Any] | None = None,
) -> FastAPI:
    """Create the daemon FastAPI app for one bound instance."""

    registry = InstanceRegistry(Path(data_dir), docker_factory=docker_factory)
    registry.prepare(http_bind=f"{host}:{port}", socks_bind=f"{socks_host}:{socks_port}")
    log_store = LogStore(registry.paths.jobs_dir)
    job_manager = JobManager(
        registry=registry,
        log_store=log_store,
        update_service_factory=update_service_factory,
    )
    authenticator = HMACAuthenticator(
        instance_id=registry.instance_id,
        token_getter=registry.token,
    )
    socks_server = (
        Socks5Server(
            bind_host=socks_host,
            bind_port=socks_port,
            http_host=host,
            http_port=port,
        )
        if enable_socks
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if socks_server is not None:
            socks_server.start()
        try:
            yield
        finally:
            if socks_server is not None:
                socks_server.stop()
            job_manager.shutdown()

    app = FastAPI(title="NA-Tools Daemon", version=__version__, lifespan=lifespan)
    app.state.registry = registry
    app.state.log_store = log_store
    app.state.job_manager = job_manager
    app.state.authenticator = authenticator
    app.state.socks_server = socks_server

    @app.exception_handler(DaemonAPIError)
    async def daemon_api_error_handler(
        _request: Request,
        exc: DaemonAPIError,
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.payload())

    async def require_auth(request: Request) -> None:
        body = await request.body()
        authenticator.verify(request, body)

    @app.get("/v1/health", dependencies=[Depends(require_auth)])
    async def health() -> dict[str, object]:
        return {
            "ok": True,
            "daemon_version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "platform": registry.capabilities()["platform"],
            "started_at": registry.started_at,
        }

    @app.get("/v1/capabilities", dependencies=[Depends(require_auth)])
    async def capabilities() -> dict[str, object]:
        return registry.capabilities()

    @app.get("/v1/instances/current", dependencies=[Depends(require_auth)])
    async def current_instance() -> dict[str, object]:
        return registry.current_instance()

    @app.get("/v1/backups", dependencies=[Depends(require_auth)])
    async def list_backups(
        name: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        return job_manager.list_backups(name=name, limit=limit)

    @app.post("/v1/jobs/update", dependencies=[Depends(require_auth)])
    async def create_update_job(request: Request) -> dict[str, object]:
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise DaemonAPIError(400, "auth_failed", "request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise DaemonAPIError(400, "auth_failed", "request body must be an object")
        return job_manager.create_update(payload)

    @app.post("/v1/jobs/backup", dependencies=[Depends(require_auth)])
    async def create_backup_job(request: Request) -> dict[str, object]:
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise DaemonAPIError(400, "auth_failed", "request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise DaemonAPIError(400, "auth_failed", "request body must be an object")
        return job_manager.create_backup(payload)

    @app.post("/v1/jobs/restore", dependencies=[Depends(require_auth)])
    async def create_restore_job(request: Request) -> dict[str, object]:
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise DaemonAPIError(400, "auth_failed", "request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise DaemonAPIError(400, "auth_failed", "request body must be an object")
        return job_manager.create_restore(payload)

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_auth)])
    async def get_job(job_id: str) -> dict[str, object]:
        return job_manager.get_job_or_error(job_id)

    @app.get("/v1/jobs/{job_id}/logs", dependencies=[Depends(require_auth)])
    async def get_job_logs(
        job_id: str,
        after_seq: int | None = None,
        limit: int = 200,
    ) -> dict[str, object]:
        _ = job_manager.get_job_or_error(job_id)
        logs = log_store.public_logs(job_id, after_seq=after_seq, limit=limit)
        next_after_seq = logs[-1]["seq"] if logs else after_seq
        return {"job_id": job_id, "logs": logs, "next_after_seq": next_after_seq}

    @app.get("/v1/jobs/{job_id}/events", dependencies=[Depends(require_auth)])
    async def get_job_events(
        job_id: str,
        after_seq: int | None = None,
    ) -> StreamingResponse:
        _ = job_manager.get_job_or_error(job_id)
        generator = _event_stream(job_manager, job_id, after_seq=after_seq or 0)
        return StreamingResponse(generator, media_type="text/event-stream")

    return app


async def _event_stream(
    job_manager: JobManager,
    job_id: str,
    *,
    after_seq: int,
) -> AsyncIterator[str]:
    last_seq = after_seq
    while True:
        records = job_manager.log_store.read(job_id, after_seq=last_seq, limit=1000)
        for record in records:
            last_seq = int(record["seq"])
            yield _format_sse(record)

        job = job_manager.get_job(job_id)
        if job is None or job.get("status") in TERMINAL_STATUSES:
            break

        await asyncio.to_thread(
            job_manager.log_store.wait_for_new,
            job_id,
            after_seq=last_seq,
            timeout=1.0,
        )


def _format_sse(record: dict[str, Any]) -> str:
    event = str(record.get("event") or "log")
    data = record.get("data")
    if data is None:
        data = {
            "seq": record.get("seq"),
            "ts": record.get("ts"),
            "level": record.get("level"),
            "stream": record.get("stream"),
            "line": record.get("line"),
        }
    elif isinstance(data, dict) and "seq" not in data:
        data = {"seq": record.get("seq"), **data}
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from na_tools.daemon import jobs as jobs_module
from na_tools.daemon.app import _event_stream, create_app
from na_tools.services.job_events import UpdateEvent
from na_tools.services.update_service import (
    HealthCheckResult,
    UpdateRequest,
    UpdateResult,
    UpdateServiceError,
)


class FakeDocker:
    docker_installed = True
    compose_installed = True
    compose_cmd = ["docker", "compose"]
    access_error: str | None = None

    def check_access(self) -> str | None:
        return self.access_error


class PermissionDeniedDocker(FakeDocker):
    access_error = "docker_permission_denied"


class FakeUpdateService:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[UpdateRequest] = []

    def run(self, request: UpdateRequest, sink: Any) -> UpdateResult:
        self.requests.append(request)
        sink(
            UpdateEvent(
                type="phase",
                phase="validate_instance",
                message="checking instance",
            )
        )
        sink(
            UpdateEvent(
                type="progress",
                phase="pull_images",
                message="pulling images",
                current=4,
                total=8,
            )
        )
        sink(
            UpdateEvent(
                type="log",
                phase="pull_images",
                message="docker compose pull",
            )
        )
        if self.fail:
            raise UpdateServiceError(
                "pull_failed",
                "pull failed",
                phase="pull_images",
                details={"command": "docker compose pull"},
            )
        sink(
            UpdateEvent(
                type="result",
                phase="finished",
                message="done",
                data={"app_health": "ok"},
            )
        )
        return UpdateResult(
            channel=request.channel,
            image="kromiose/nekro-agent:latest",
            image_tag="latest",
            backup_file=None,
            app_health=HealthCheckResult(
                ok=True,
                url="http://127.0.0.1:8021/api/health",
            ),
        )


class DaemonAPITest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "nekro_agent"
        self.data_dir.mkdir()
        self._write_instance()
        self.service = FakeUpdateService()
        self.app = create_app(
            self.data_dir,
            docker_factory=FakeDocker,
            update_service_factory=lambda: self.service,
        )

    def tearDown(self) -> None:
        self.app.state.job_manager.shutdown()
        self.tmp.cleanup()

    def test_health_and_capabilities_return_success_with_valid_hmac(self) -> None:
        health = self._request("GET", "/v1/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["protocol_version"], "na-tools.daemon.v1")

        capabilities = self._request("GET", "/v1/capabilities")
        self.assertEqual(capabilities.status_code, 200)
        payload = capabilities.json()
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["provider"], "na-tools")
        self.assertTrue(payload["supports"]["log_stream"])
        self.assertFalse(payload["supports"]["daemon_update"])
        self.assertFalse(payload["supports"]["cancel"])
        self.assertTrue(payload["supports"]["backup"])
        self.assertTrue(payload["supports"]["restore"])

    def test_hmac_rejects_missing_wrong_expired_and_replayed_requests(self) -> None:
        missing = self._raw_request("GET", "/v1/health")
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json()["error"]["code"], "auth_failed")

        wrong = self._request("GET", "/v1/health", signature="v1=bad")
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.json()["error"]["code"], "auth_failed")

        expired = self._request(
            "GET",
            "/v1/health",
            timestamp=str(int(time.time() * 1000) - 61_000),
        )
        self.assertEqual(expired.status_code, 401)
        self.assertEqual(expired.json()["error"]["code"], "auth_failed")

        nonce = uuid.uuid4().hex
        headers = self._headers("GET", "/v1/health", nonce=nonce)
        first = self._raw_request("GET", "/v1/health", headers=headers)
        second = self._raw_request("GET", "/v1/health", headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 401)
        self.assertEqual(second.json()["error"]["code"], "request_replayed")

    def test_update_job_succeeds_and_client_request_id_is_idempotent(self) -> None:
        payload = self._update_payload(client_request_id="req-1")
        first = self._request("POST", "/v1/jobs/update", json_body=payload)
        second = self._request("POST", "/v1/jobs/update", json_body=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["job_id"], second.json()["job_id"])

        job = self._wait_for_job(first.json()["job_id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["result"]["app_health"], "ok")
        self.assertTrue(
            (self.data_dir / ".na-tools" / "jobs" / f"{job['job_id']}.json").exists()
        )

    def test_backup_list_returns_safe_sorted_summaries(self) -> None:
        config_dir = self.root / "config"
        backup_dir = config_dir / "backup" / self.data_dir.name
        backup_dir.mkdir(parents=True)
        old_backup = backup_dir / f"{self.data_dir.name}_backup_manual_20260610_010203.tar.gz"
        new_backup = backup_dir / f"{self.data_dir.name}_backup_webui_20260611_010203.tar.gz"
        old_backup.write_text("old", encoding="utf-8")
        new_backup.write_text("new", encoding="utf-8")
        old_time = time.time() - 60
        new_time = time.time()
        old_backup.touch()
        new_backup.touch()
        import os

        os.utime(old_backup, (old_time, old_time))
        os.utime(new_backup, (new_time, new_time))

        with patch.object(jobs_module, "get_global_config_dir", return_value=config_dir):
            response = self._request("GET", "/v1/backups?limit=10")

        self.assertEqual(response.status_code, 200)
        backups = response.json()["backups"]
        self.assertEqual([item["filename"] for item in backups], [new_backup.name, old_backup.name])
        serialized = json.dumps(backups)
        self.assertNotIn(str(config_dir), serialized)
        self.assertEqual(backups[0]["backup_id"], new_backup.name)
        self.assertEqual(backups[0]["name"], "webui")

    def test_backup_job_succeeds_and_client_request_id_is_idempotent(self) -> None:
        config_dir = self.root / "config"
        backup_dir = config_dir / "backup" / self.data_dir.name
        backup_dir.mkdir(parents=True)
        backup_file = backup_dir / f"{self.data_dir.name}_backup_webui_20260611_010203.tar.gz"

        def fake_backup_runner(request: Any) -> Path:
            self.assertEqual(request.name, "webui")
            self.assertFalse(request.no_restart)
            backup_file.write_text("backup", encoding="utf-8")
            return backup_file

        payload = self._backup_payload(client_request_id="backup-1", name="webui")
        with (
            patch.object(jobs_module, "get_global_config_dir", return_value=config_dir),
            patch.object(jobs_module, "_daemon_backup_runner", side_effect=fake_backup_runner),
        ):
            first = self._request("POST", "/v1/jobs/backup", json_body=payload)
            second = self._request("POST", "/v1/jobs/backup", json_body=payload)
            job = self._wait_for_job(first.json()["job_id"])

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["job_id"], second.json()["job_id"])
        self.assertEqual(job["type"], "backup")
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["result"]["backup"]["filename"], backup_file.name)

    def test_restore_job_rejects_invalid_or_missing_backup_id(self) -> None:
        for backup_id in ("../bad.tar.gz", "/tmp/bad.tar.gz", "bad.zip"):
            response = self._request(
                "POST",
                "/v1/jobs/restore",
                json_body=self._restore_payload(backup_id),
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "invalid_backup_id")

        missing = self._request(
            "POST",
            "/v1/jobs/restore",
            json_body=self._restore_payload("missing.tar.gz"),
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "backup_not_found")

    def test_restore_job_uses_selected_backup_and_conflicts_with_active_job(self) -> None:
        config_dir = self.root / "config"
        backup_dir = config_dir / "backup" / self.data_dir.name
        backup_dir.mkdir(parents=True)
        backup_file = backup_dir / f"{self.data_dir.name}_backup_webui_20260611_010203.tar.gz"
        backup_file.write_text("backup", encoding="utf-8")
        restored: list[Path] = []

        def slow_backup_runner(request: Any) -> Path:
            time.sleep(0.2)
            created = backup_dir / f"{request.data_dir.name}_backup_slow_20260611_010203.tar.gz"
            created.write_text("backup", encoding="utf-8")
            return created

        def fake_restore_runner(request: Any) -> None:
            restored.append(request.backup_file)

        with (
            patch.object(jobs_module, "get_global_config_dir", return_value=config_dir),
            patch.object(jobs_module, "_daemon_backup_runner", side_effect=slow_backup_runner),
            patch.object(jobs_module, "_daemon_restore_runner", side_effect=fake_restore_runner),
        ):
            running = self._request("POST", "/v1/jobs/backup", json_body=self._backup_payload())
            conflict = self._request(
                "POST",
                "/v1/jobs/restore",
                json_body=self._restore_payload(backup_file.name),
            )
            self._wait_for_job(running.json()["job_id"])
            response = self._request(
                "POST",
                "/v1/jobs/restore",
                json_body=self._restore_payload(backup_file.name),
            )
            job = self._wait_for_job(response.json()["job_id"])

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "job_conflict")
        self.assertEqual(job["type"], "restore")
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(restored, [backup_file.resolve()])

    def test_update_service_failure_marks_job_failed_and_keeps_logs(self) -> None:
        service = FakeUpdateService(fail=True)
        self.app.state.job_manager.shutdown()
        app = create_app(
            self.data_dir,
            docker_factory=FakeDocker,
            update_service_factory=lambda: service,
        )
        self.app = app

        response = self._request("POST", "/v1/jobs/update", json_body=self._update_payload())
        job = self._wait_for_job(response.json()["job_id"])

        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "pull_failed")
        logs = self._request("GET", f"/v1/jobs/{job['job_id']}/logs").json()["logs"]
        self.assertTrue(any("pull failed" in item["line"] for item in logs))

    def test_logs_support_after_seq_and_limit(self) -> None:
        response = self._request("POST", "/v1/jobs/update", json_body=self._update_payload())
        job = self._wait_for_job(response.json()["job_id"])

        logs_response = self._request("GET", f"/v1/jobs/{job['job_id']}/logs?limit=10")
        logs = logs_response.json()["logs"]
        self.assertGreaterEqual(len(logs), 2)
        after_seq = logs[0]["seq"]

        next_response = self._request(
            "GET",
            f"/v1/jobs/{job['job_id']}/logs?after_seq={after_seq}&limit=10",
        )
        next_logs = next_response.json()["logs"]
        self.assertTrue(next_logs)
        self.assertTrue(all(item["seq"] > after_seq for item in next_logs))

    def test_events_resume_from_after_seq_and_emit_result(self) -> None:
        response = self._request("POST", "/v1/jobs/update", json_body=self._update_payload())
        job = self._wait_for_job(response.json()["job_id"])
        text = asyncio.run(self._collect_events(job["job_id"], after_seq=0))

        self.assertIn("event: job", text)
        self.assertIn("event: progress", text)
        self.assertIn("event: log", text)
        self.assertIn("event: result", text)
        self.assertIn('"status": "succeeded"', text)
        for event in self._parse_sse_events(text):
            if event["event"] == "log":
                self.assertIn("line", event["data"])
                self.assertNotIn("app_health", event["data"])

    async def _collect_events(self, job_id: str, *, after_seq: int) -> str:
        chunks: list[str] = []
        async for chunk in _event_stream(
            self.app.state.job_manager,
            job_id,
            after_seq=after_seq,
        ):
            chunks.append(chunk)
        return "".join(chunks)

    def _parse_sse_events(self, text: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for block in text.strip().split("\n\n"):
            event_name = "message"
            data = ""
            for line in block.splitlines():
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
            if data:
                events.append({"event": event_name, "data": json.loads(data)})
        return events

    def test_capabilities_report_unavailable_reason_when_compose_is_missing(self) -> None:
        missing_dir = self.root / "missing_compose"
        missing_dir.mkdir()
        (missing_dir / ".env").write_text("NEKRO_EXPOSE_PORT=8021\n", encoding="utf-8")
        self.app.state.job_manager.shutdown()
        self.app = create_app(
            missing_dir,
            docker_factory=FakeDocker,
            update_service_factory=lambda: self.service,
        )

        response = self._request("GET", "/v1/capabilities")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enabled"])
        self.assertEqual(response.json()["unavailable_reason"], "compose_missing")

    def test_docker_permission_preflight_disables_capabilities_and_rejects_update(self) -> None:
        self.app.state.job_manager.shutdown()
        service = FakeUpdateService()
        self.app = create_app(
            self.data_dir,
            docker_factory=PermissionDeniedDocker,
            update_service_factory=lambda: service,
        )

        capabilities = self._request("GET", "/v1/capabilities")
        self.assertEqual(capabilities.status_code, 200)
        self.assertFalse(capabilities.json()["enabled"])
        self.assertEqual(
            capabilities.json()["unavailable_reason"],
            "docker_permission_denied",
        )

        response = self._request("POST", "/v1/jobs/update", json_body=self._update_payload())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "docker_permission_denied")
        self.assertEqual(service.requests, [])

    def _update_payload(self, *, client_request_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instance_id": self.app.state.registry.instance_id,
            "channel": "stable",
            "backup": False,
            "update_sandbox": False,
            "update_cc_sandbox": False,
            "restore_pre_preview": False,
            "requested_by": {"source": "test", "username": "admin"},
        }
        if client_request_id is not None:
            payload["client_request_id"] = client_request_id
        return payload

    def _backup_payload(
        self,
        *,
        client_request_id: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instance_id": self.app.state.registry.instance_id,
            "requested_by": {"source": "test", "username": "admin"},
        }
        if client_request_id is not None:
            payload["client_request_id"] = client_request_id
        if name is not None:
            payload["name"] = name
        return payload

    def _restore_payload(self, backup_id: str) -> dict[str, Any]:
        return {
            "instance_id": self.app.state.registry.instance_id,
            "backup_id": backup_id,
            "requested_by": {"source": "test", "username": "admin"},
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timestamp: str | None = None,
        signature: str | None = None,
    ) -> Any:
        body = b""
        headers = {"Accept": "application/json"}
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        headers.update(
            self._headers(method, path, body=body, timestamp=timestamp, signature=signature)
        )
        return self._raw_request(method, path, content=body, headers=headers)

    def _raw_request(
        self,
        method: str,
        path: str,
        *,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.request(
                    method,
                    path,
                    content=content,
                    headers=headers,
                )

        return asyncio.run(send())

    def _headers(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        nonce: str | None = None,
        timestamp: str | None = None,
        signature: str | None = None,
    ) -> dict[str, str]:
        timestamp = timestamp or str(int(time.time() * 1000))
        nonce = nonce or uuid.uuid4().hex
        signature = signature or self.app.state.authenticator.sign(
            method=method,
            path_with_query=path,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        return {
            "X-NA-Instance": self.app.state.registry.instance_id,
            "X-NA-Timestamp": timestamp,
            "X-NA-Nonce": nonce,
            "X-NA-Signature": signature,
        }

    def _wait_for_job(self, job_id: str) -> dict[str, Any]:
        deadline = time.time() + 3
        while time.time() < deadline:
            response = self._request("GET", f"/v1/jobs/{job_id}")
            job = response.json()
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                return job
            time.sleep(0.02)
        self.fail(f"job did not finish: {job_id}")

    def _write_instance(self) -> None:
        (self.data_dir / "docker-compose.yml").write_text(
            "\n".join(
                [
                    "services:",
                    "  nekro_agent:",
                    "    image: kromiose/nekro-agent:latest",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (self.data_dir / ".env").write_text(
            "NEKRO_EXPOSE_PORT=8021\nINSTANCE_NAME=na_\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()

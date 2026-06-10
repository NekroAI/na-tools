from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

import httpx

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

    async def _collect_events(self, job_id: str, *, after_seq: int) -> str:
        chunks: list[str] = []
        async for chunk in _event_stream(
            self.app.state.job_manager,
            job_id,
            after_seq=after_seq,
        ):
            chunks.append(chunk)
        return "".join(chunks)

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

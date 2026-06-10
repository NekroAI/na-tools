"""Persistent JSONL job logs and SSE event storage."""

from __future__ import annotations

import json
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class LogStore:
    """Append-only per-job JSONL logs with a small in-memory ring buffer."""

    def __init__(self, jobs_dir: Path, *, max_lines: int = 1000) -> None:
        self._jobs_dir = jobs_dir
        self._max_lines = max_lines
        self._buffers: dict[str, deque[dict[str, Any]]] = {}
        self._seq: dict[str, int] = defaultdict(int)
        self._conditions: dict[str, threading.Condition] = defaultdict(
            threading.Condition
        )
        self._lock = threading.Lock()

    def append(
        self,
        job_id: str,
        *,
        level: str,
        stream: str,
        line: str,
        event: str = "log",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one JSONL record and return it."""

        with self._lock:
            self._ensure_loaded_locked(job_id)
            seq = self._seq[job_id] + 1
            self._seq[job_id] = seq
            record: dict[str, Any] = {
                "seq": seq,
                "ts": _utc_now(),
                "level": level,
                "stream": stream,
                "line": line,
                "event": event,
            }
            if data is not None:
                record["data"] = data
            self._buffer_for(job_id).append(record)

        log_path = self.log_path(job_id)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        condition = self._conditions[job_id]
        with condition:
            condition.notify_all()
        return record

    def read(
        self,
        job_id: str,
        *,
        after_seq: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Read records, optionally only records after a sequence number."""

        limit = max(1, min(limit, self._max_lines))
        with self._lock:
            self._ensure_loaded_locked(job_id)
            records = list(self._buffer_for(job_id))

        if after_seq is not None:
            records = [record for record in records if int(record["seq"]) > after_seq]
            return records[:limit]
        return records[-limit:]

    def public_logs(
        self,
        job_id: str,
        *,
        after_seq: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Read log records in the public protocol shape."""

        return [
            {
                "seq": record["seq"],
                "ts": record["ts"],
                "level": record["level"],
                "stream": record["stream"],
                "line": record["line"],
            }
            for record in self.read(job_id, after_seq=after_seq, limit=limit)
        ]

    def wait_for_new(self, job_id: str, *, after_seq: int, timeout: float) -> None:
        """Block until new records may exist or timeout expires."""

        condition = self._conditions[job_id]
        with condition:
            condition.wait(timeout=timeout)

    def log_path(self, job_id: str) -> Path:
        """Return the JSONL path for a job."""

        return self._jobs_dir / f"{job_id}.log"

    def _ensure_loaded_locked(self, job_id: str) -> None:
        if job_id in self._buffers:
            return
        buffer = self._buffer_for(job_id)
        log_path = self.log_path(job_id)
        if not log_path.exists():
            return
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or "seq" not in record:
                continue
            buffer.append(record)
            self._seq[job_id] = max(self._seq[job_id], int(record["seq"]))

    def _buffer_for(self, job_id: str) -> deque[dict[str, Any]]:
        if job_id not in self._buffers:
            self._buffers[job_id] = deque(maxlen=self._max_lines)
        return self._buffers[job_id]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


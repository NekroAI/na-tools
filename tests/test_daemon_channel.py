from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

import yaml

from na_tools.core.config import load_env
from na_tools.daemon import (
    CONTAINER_DAEMON_TOKEN_FILE,
    DEFAULT_DAEMON_API_BASE,
    DEFAULT_DAEMON_SOCKS_URL,
)
from na_tools.daemon.channel import ensure_daemon_channel


class DaemonChannelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "nekro_agent"
        self.data_dir.mkdir()
        self._write_compose()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_install_style_channel_writes_env_compose_token_and_metadata(self) -> None:
        (self.data_dir / ".env").write_text("NEKRO_EXPOSE_PORT=8021\n", encoding="utf-8")

        result = ensure_daemon_channel(self.data_dir, overwrite_env=True)
        env = load_env(self.data_dir / ".env")
        compose = self._read_compose()
        service = cast(dict[str, Any], compose["services"]["nekro_agent"])
        metadata = json.loads(result.daemon_json.read_text(encoding="utf-8"))

        self.assertEqual(env["NA_TOOLS_DAEMON_ENABLED"], "true")
        self.assertEqual(env["NA_TOOLS_DAEMON_API_BASE"], DEFAULT_DAEMON_API_BASE)
        self.assertEqual(env["NA_TOOLS_DAEMON_SOCKS"], DEFAULT_DAEMON_SOCKS_URL)
        self.assertEqual(env["NA_TOOLS_DAEMON_INSTANCE_ID"], result.instance_id)
        self.assertNotIn("NA_TOOLS_DAEMON_TOKEN", env)
        self.assertNotIn("NA_TOOLS_DAEMON_TOKEN_FILE", env)
        self.assertTrue(result.token_file.exists())
        self.assertEqual(metadata["api_base"], DEFAULT_DAEMON_API_BASE)
        self.assertEqual(metadata["socks_url"], DEFAULT_DAEMON_SOCKS_URL)
        self.assertEqual(metadata["http_bind"], "127.0.0.1:18081")
        self.assertEqual(metadata["socks_bind"], "0.0.0.0:18082")
        self.assertIn("daemon_pid", metadata)

        environment = cast(list[str], service["environment"])
        self.assertIn(
            "NA_TOOLS_DAEMON_API_BASE=${NA_TOOLS_DAEMON_API_BASE:-http://na-tools.local/v1}",
            environment,
        )
        self.assertIn(
            f"NA_TOOLS_DAEMON_TOKEN_FILE={CONTAINER_DAEMON_TOKEN_FILE}",
            environment,
        )
        self.assertIn("host.docker.internal:host-gateway", service["extra_hosts"])

    def test_bind_style_channel_preserves_existing_token_env_and_unrelated_config(self) -> None:
        meta_dir = self.data_dir / ".na-tools"
        meta_dir.mkdir()
        token_file = meta_dir / "daemon.token"
        token_file.write_text("existing-token\n", encoding="utf-8")
        (self.data_dir / ".env").write_text(
            "\n".join(
                [
                    "UNRELATED=value",
                    "NA_TOOLS_DAEMON_ENABLED=false",
                    "NA_TOOLS_DAEMON_SOCKS=",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        result = ensure_daemon_channel(self.data_dir, overwrite_env=False)
        env = load_env(self.data_dir / ".env")

        self.assertEqual(token_file.read_text(encoding="utf-8"), "existing-token\n")
        self.assertEqual(env["UNRELATED"], "value")
        self.assertEqual(env["NA_TOOLS_DAEMON_ENABLED"], "false")
        self.assertEqual(env["NA_TOOLS_DAEMON_SOCKS"], "")
        self.assertEqual(env["NA_TOOLS_DAEMON_API_BASE"], DEFAULT_DAEMON_API_BASE)
        self.assertEqual(env["NA_TOOLS_DAEMON_INSTANCE_ID"], result.instance_id)

    def _write_compose(self) -> None:
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

    def _read_compose(self) -> dict[str, Any]:
        with open(self.data_dir / "docker-compose.yml", encoding="utf-8") as f:
            content = yaml.safe_load(f)
        self.assertIsInstance(content, dict)
        return cast(dict[str, Any], content)


if __name__ == "__main__":
    unittest.main()

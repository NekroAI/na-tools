from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from na_tools.commands.update import update


class UpdateCommandTest(unittest.TestCase):
    def test_rollback_defaults_to_not_restoring_pre_preview_backup(self) -> None:
        request = self._invoke_rollback(input_text="\n")

        self.assertFalse(request.restore_pre_preview)

    def test_rollback_restores_pre_preview_backup_when_user_confirms(self) -> None:
        request = self._invoke_rollback(input_text="y\n")

        self.assertTrue(request.restore_pre_preview)

    def _invoke_rollback(self, *, input_text: str):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "nekro_agent"
            data_dir.mkdir()
            backup_file = data_dir / "na_backup_pre-preview_20260704_010203.tar.gz"
            requests = []

            class FakeUpdateService:
                def __init__(self, **_kwargs: object) -> None:
                    return None

                def run(self, request, _sink):
                    requests.append(request)
                    return None

            runner = CliRunner()
            with (
                patch(
                    "na_tools.commands.update.find_latest_named_backup",
                    return_value=backup_file,
                ),
                patch("na_tools.commands.update.UpdateService", FakeUpdateService),
            ):
                result = runner.invoke(
                    update,
                    ["--data-dir", str(data_dir), "--rollback"],
                    input=input_text,
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(requests), 1)
        return requests[0]


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import unittest
from unittest import mock

from ai_auth_switch import cli


class PoolEntrypointTests(unittest.TestCase):
    def test_app_server_entrypoint_accepts_codex_extension_arguments(self) -> None:
        with (
            mock.patch.object(cli, "main", return_value=7) as main,
            mock.patch.object(
                sys,
                "argv",
                [
                    "ais-pool-app-server",
                    "app-server",
                    "--analytics-default-enabled",
                    "--listen",
                    "stdio://",
                    "--enable",
                    "foo",
                ],
            ),
        ):
            self.assertEqual(cli.pool_app_server_main(), 7)
        main.assert_called_once_with(
            ["pool", "app-server"],
            program_name="ai-auth-switch",
        )


if __name__ == "__main__":
    unittest.main()

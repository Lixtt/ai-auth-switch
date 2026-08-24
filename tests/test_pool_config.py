from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import tomllib

from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.pool_config import install_pool_provider, restore_codex_config


class PoolConfigTests(unittest.TestCase):
    def test_install_pool_provider_preserves_other_config_and_backs_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / "config.toml"
            config.write_text(
                'model_provider = "custom"\nmodel = "gpt-5"\n\n'
                '[model_providers.custom]\nname = "OpenAI"\n\n'
                '[projects."/tmp/project"]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )
            result = install_pool_provider(
                home,
                base_url="http://127.0.0.1:8765/v1",
            )
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(parsed["model_provider"], "custom")
            self.assertEqual(
                parsed["model_providers"]["custom"]["wire_api"],
                "responses",
            )
            self.assertEqual(parsed["model"], "gpt-5")
            self.assertEqual(
                parsed["projects"]["/tmp/project"]["trust_level"], "trusted"
            )
            self.assertTrue(result.changed)
            self.assertIsNotNone(result.backup_path)
            self.assertEqual(
                result.backup_path.read_text(encoding="utf-8").splitlines()[0],
                'model_provider = "custom"',
            )
            restore_codex_config(home, result.backup_path)
            self.assertEqual(
                tomllib.loads(config.read_text(encoding="utf-8"))["model_provider"],
                "custom",
            )

    def test_invalid_provider_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('model_provider = "custom"\n', encoding="utf-8")
            with self.assertRaisesRegex(AiAuthSwitchError, "invalid custom provider"):
                install_pool_provider(
                    Path(tmp),
                    base_url="http://127.0.0.1:8765/v1",
                    provider_id="bad id",
                )


if __name__ == "__main__":
    unittest.main()

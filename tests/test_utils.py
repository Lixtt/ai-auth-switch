from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_auth_switch.utils import (
    atomic_write,
    decode_jwt_payload,
    extract_account_id_from_jwt,
    extract_email_from_jwt,
)


def _jwt(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{encoded}.signature"


class JwtDecodeTests(unittest.TestCase):
    def test_decodes_standard_payload(self) -> None:
        payload = {"sub": "u-1", "email": "a@example.com"}
        self.assertEqual(decode_jwt_payload(_jwt(payload)), payload)

    def test_handles_missing_padding(self) -> None:
        payload = {"email": "padded@example.com"}
        token = _jwt(payload)
        # Strip trailing '=' to force padding reconstruction in the decoder.
        self.assertEqual(decode_jwt_payload(token), payload)

    def test_rejects_malformed_tokens(self) -> None:
        self.assertEqual(decode_jwt_payload(""), {})
        self.assertEqual(decode_jwt_payload("only-one-part"), {})
        self.assertEqual(decode_jwt_payload("a.%%%.c"), {})
        self.assertEqual(decode_jwt_payload("a.b.c.d.e"), {})

    def test_extract_email_checks_profile_then_top_level(self) -> None:
        token = _jwt(
            {
                "email": "top@example.com",
                "https://api.openai.com/profile": {"email": "profile@example.com"},
            }
        )
        self.assertEqual(extract_email_from_jwt(token), "profile@example.com")
        self.assertEqual(
            extract_email_from_jwt(_jwt({"email": "top@example.com"})),
            "top@example.com",
        )
        self.assertIsNone(extract_email_from_jwt(_jwt({"sub": "u"})))

    def test_extract_account_id_prefers_chatgpt_account_id(self) -> None:
        token = _jwt(
            {
                "account_id": "legacy-acc",
                "chatgpt_account_id": "chatgpt-acc",
                "https://api.openai.com/auth": {"account_id": "nested-acc"},
            }
        )
        self.assertEqual(extract_account_id_from_jwt(token), "chatgpt-acc")
        self.assertEqual(
            extract_account_id_from_jwt(
                _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "n-1"}})
            ),
            "n-1",
        )
        self.assertEqual(extract_account_id_from_jwt(_jwt({"sub": "s-1"})), "s-1")
        self.assertIsNone(extract_account_id_from_jwt(_jwt({"email": "x@y.z"})))


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_creates_private_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "out.txt"
            atomic_write(target, "hello\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

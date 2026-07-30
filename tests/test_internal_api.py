import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


module_path = Path(__file__).parents[1] / "utils" / "internal_api.py"
spec = importlib.util.spec_from_file_location("internal_api", module_path)
internal_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(internal_api)
InternalSigner = internal_api.InternalSigner


class InternalSignerTests(unittest.TestCase):
    def test_valid_signature_and_tampering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            signer = InternalSigner(os.path.join(temp_dir, "secret.txt"))
            body = b'{"prompt_id":"one"}'
            headers = signer.headers("POST", "/interrupt", body)
            self.assertTrue(
                signer.verify(
                    "POST",
                    "/interrupt",
                    body,
                    headers[signer.HEADER_TIMESTAMP],
                    headers[signer.HEADER_SIGNATURE],
                )
            )
            self.assertFalse(
                signer.verify(
                    "POST",
                    "/interrupt",
                    body + b"x",
                    headers[signer.HEADER_TIMESTAMP],
                    headers[signer.HEADER_SIGNATURE],
                )
            )

    def test_expired_signature_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            signer = InternalSigner(
                os.path.join(temp_dir, "secret.txt"), max_age_seconds=5
            )
            body = b"{}"
            with mock.patch.object(internal_api.time, "time", return_value=100):
                headers = signer.headers("POST", "/internal", body)
            with mock.patch.object(internal_api.time, "time", return_value=200):
                self.assertFalse(
                    signer.verify(
                        "POST",
                        "/internal",
                        body,
                        headers[signer.HEADER_TIMESTAMP],
                        headers[signer.HEADER_SIGNATURE],
                    )
                )


if __name__ == "__main__":
    unittest.main()

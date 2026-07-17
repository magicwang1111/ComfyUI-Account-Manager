import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path


module_path = Path(__file__).parents[1] / "utils" / "history_assets.py"
spec = importlib.util.spec_from_file_location("history_assets", module_path)
history_assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history_assets)
persist_temp_assets = history_assets.persist_temp_assets


class HistoryAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.temp_root = self.root / "temp"
        self.output_root = self.root / "output"
        self.source_dir = self.temp_root / "alice" / "2026-07-17" / "alice"
        self.source_dir.mkdir(parents=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_temp_reference_is_made_durable_and_rewritten(self):
        source = self.source_dir / "preview.png"
        source.write_bytes(b"preview-data")
        item = {
            "outputs": {
                "1": {
                    "images": [
                        {
                            "filename": "preview.png",
                            "subfolder": "2026-07-17/alice",
                            "type": "temp",
                        }
                    ]
                }
            }
        }

        count = persist_temp_assets(
            item,
            str(self.temp_root),
            str(self.output_root),
            "alice",
            "2026-07-17/alice/history_assets/prompt-1",
        )

        reference = item["outputs"]["1"]["images"][0]
        destination = self.output_root / reference["subfolder"] / reference["filename"]
        self.assertEqual(1, count)
        self.assertEqual("output", reference["type"])
        self.assertTrue(destination.is_file())
        self.assertEqual(b"preview-data", destination.read_bytes())

        shutil.rmtree(self.temp_root)
        self.assertEqual(b"preview-data", destination.read_bytes())

    def test_missing_temp_file_keeps_original_reference(self):
        reference = {
            "filename": "missing.mp4",
            "subfolder": "2026-07-17/alice",
            "type": "temp",
        }
        item = {"outputs": {"1": {"videos": [reference]}}}

        count = persist_temp_assets(
            item,
            str(self.temp_root),
            str(self.output_root),
            "alice",
            "2026-07-17/alice/history_assets/prompt-2",
        )

        self.assertEqual(0, count)
        self.assertEqual("temp", reference["type"])

    def test_unowned_base_temp_path_is_not_copied(self):
        foreign_dir = self.temp_root / "2026-07-17" / "bob"
        foreign_dir.mkdir(parents=True)
        (foreign_dir / "foreign.png").write_bytes(b"foreign")
        reference = {
            "filename": "foreign.png",
            "subfolder": "2026-07-17/bob",
            "type": "temp",
        }
        item = {"outputs": {"1": {"images": [reference]}}}

        count = persist_temp_assets(
            item,
            str(self.temp_root),
            str(self.output_root),
            "alice",
            "2026-07-17/alice/history_assets/prompt-3",
        )

        self.assertEqual(0, count)
        self.assertEqual("temp", reference["type"])


if __name__ == "__main__":
    unittest.main()

import gzip
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cloud = load_module("test_cloud_archive_module", ROOT / "utils" / "cloud_archive.py")
scheduler_module = load_module(
    "test_cloud_scheduler_store", ROOT / "utils" / "scheduler_store.py"
)
history_module = load_module(
    "test_cloud_history_store", ROOT / "utils" / "history_store.py"
)


class FakeBackend:
    def __init__(self, fail=False):
        self.fail = fail
        self.objects = {}
        self.calls = []

    def upload_file(
        self,
        key,
        path,
        content_type,
        content_encoding=None,
        cache_control=None,
        sha256=None,
        private=False,
    ):
        self.calls.append(
            (key, content_type, content_encoding, cache_control, sha256, private)
        )
        if self.fail:
            raise RuntimeError("simulated OSS outage")
        with open(path, "rb") as source:
            data = source.read()
        self.objects[key] = data
        return {"etag": "fake-etag", "oss_crc64": "123", "content_length": len(data)}


class CloudArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "output"
        self.temp_output = self.root / "temp"
        self.output.mkdir()
        self.temp_output.mkdir()
        self.scheduler_db = self.root / "scheduler.sqlite3"
        self.history_db = self.root / "history.sqlite3"
        self.scheduler = scheduler_module.SchedulerStore(os.fspath(self.scheduler_db))
        self.history = history_module.HistoryStore(os.fspath(self.history_db))
        self.config = cloud.CloudArchiveConfig(
            enabled=True,
            server_id="seetacloud-5090x2",
            region="cn-hangzhou",
            endpoint="oss-cn-hangzhou.aliyuncs.com",
            bucket="goumee-coze",
            prefix="Goumee-ComfyUI-Server-Data",
            public_base_url="https://goumee-coze.oss-cn-hangzhou.aliyuncs.com",
            staging_dir=os.fspath(self.root / "staging"),
            checkpoint_dir=os.fspath(self.root / "checkpoints"),
        )

    def tearDown(self):
        self.temp.cleanup()

    def add_job(self, prompt_id="prompt-1", status="completed", worker_port=6009):
        item = (0.0, prompt_id, {"1": {"class_type": "Test"}}, {}, ["1"], {})
        self.scheduler.enqueue(item, "owner-1", 6006)
        with closing(sqlite3.connect(self.scheduler_db)) as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE scheduler_jobs
                    SET status=?, worker_port=?, started_at=100, finished_at=200,
                        payload=?, error=?
                    WHERE prompt_id=?
                    """,
                    (
                        status,
                        worker_port,
                        '{"raw":"payload"}',
                        "failure detail" if status == "failed" else None,
                        prompt_id,
                    ),
                )

    def history_item(self, filename=None, asset_id=None):
        output = {}
        if filename:
            output.update({"filename": filename, "subfolder": "", "type": "output"})
        if asset_id:
            output["id"] = asset_id
        return {
            "outputs": {"1": {"images": [output] if output else []}},
            "status": {"status_str": "success", "completed": True},
            "user_id": "owner-1",
        }

    def manager(self, backend=None):
        return cloud.CloudArchiveManager(
            self.config,
            os.fspath(self.scheduler_db),
            os.fspath(self.history_db),
            os.fspath(self.output),
            os.fspath(self.temp_output),
            os.fspath(ROOT),
            backend=backend or FakeBackend(),
            start_worker=False,
        )

    def test_migration_is_idempotent_and_duplicate_enqueue_is_idempotent(self):
        self.add_job()
        store = cloud.CloudArchiveStore(os.fspath(self.scheduler_db))
        cloud.CloudArchiveStore(os.fspath(self.scheduler_db))
        artifact = {
            "source_kind": "local_output",
            "source_path": os.fspath(self.output / "a.png"),
            "source_url": None,
            "filename": "a.png",
            "mime_type": "image/png",
            "reference": {},
            "output_path": "outputs/1",
        }
        store.enqueue_task(self.config, "prompt-1", "owner-1", 6009, "completed", 200, [artifact])
        store.enqueue_task(self.config, "prompt-1", "owner-1", 6009, "completed", 200, [artifact])
        self.assertEqual(1, len(store.artifacts("prompt-1")))

    def test_discovery_only_walks_outputs_and_supports_fullpath_and_remote_url(self):
        local = self.output / "result.mp4"
        local.write_bytes(b"video")
        history = {
            "prompt": {"input": "https://example.com/input.mp4"},
            "outputs": {
                "1": {"videos": [{"fullpath": os.fspath(local)}]},
                "2": {"url": "https://cdn.example.com/final.mp4"},
            },
        }
        found = cloud.discover_artifacts(
            history, os.fspath(self.output), os.fspath(self.temp_output)
        )
        self.assertEqual(["local_output", "remote_url"], [row["source_kind"] for row in found])
        self.assertNotIn("input.mp4", json.dumps(found))

    def test_public_and_private_object_keys_are_strictly_separated(self):
        media = self.config.media_key("abc", 1, "a b.mp4", 1787639123)
        manifest = self.config.manifest_key("abc")
        self.assertIn("/public/servers/seetacloud-5090x2/", media)
        self.assertEqual(
            "Goumee-ComfyUI-Server-Data/private/jobs/abc/manifest.json.gz", manifest
        )
        self.assertNotIn("private", self.config.public_url(media))

    def test_local_media_and_gzip_manifest_upload_complete_without_deleting_source(self):
        self.add_job()
        source = self.output / "result.png"
        source.write_bytes(b"png-data")
        item = self.history_item("result.png")
        self.history.save("prompt-1", item, 100)
        backend = FakeBackend()
        manager = self.manager(backend)
        self.assertEqual(1, manager.enqueue_completion("prompt-1", "owner-1", 6009, "completed", item, 200))

        artifact = manager.store.claim_artifact("worker", 2)
        manager._upload_artifact(artifact)
        task = manager.store.claim_manifest("worker", 3, 2)
        manager._upload_manifest(task)

        current = manager.store.task("prompt-1")
        self.assertEqual(cloud.CLOUD_COMPLETED, current["cloud_status"])
        self.assertEqual(cloud.UPLOADED, current["manifest_upload_status"])
        self.assertTrue(source.exists())
        manifest_bytes = backend.objects[self.config.manifest_key("prompt-1")]
        manifest = json.loads(gzip.decompress(manifest_bytes))
        self.assertEqual("prompt-1", manifest["export"]["prompt_id"])
        self.assertEqual("uploaded", manifest["artifacts"][0]["upload_status"])
        self.assertEqual("application/gzip", backend.calls[-1][1])
        self.assertIsNone(backend.calls[-1][2])
        self.assertEqual("no-store", backend.calls[-1][3])
        self.assertTrue(backend.calls[-1][5])
        decoded_text = gzip.decompress(manifest_bytes).decode("utf-8")
        self.assertIn('\n  "artifacts":', decoded_text)
        self.assertTrue(decoded_text.endswith("\n"))
        self.assertTrue(manifest_bytes[3] & 0x08)
        embedded_name = manifest_bytes[10:].split(b"\0", 1)[0]
        self.assertEqual(b"manifest.json", embedded_name)

    def test_empty_failed_task_still_uploads_private_manifest(self):
        self.add_job(status="failed")
        item = self.history_item()
        self.history.save("prompt-1", item, 100)
        backend = FakeBackend()
        manager = self.manager(backend)
        manager.enqueue_completion("prompt-1", "owner-1", 6009, "failed", item, 200)
        task = manager.store.claim_manifest("worker", 3, 2)
        manager._upload_manifest(task)
        self.assertEqual(cloud.NO_ARTIFACTS, manager.store.task("prompt-1")["cloud_status"])
        self.assertIn(self.config.manifest_key("prompt-1"), backend.objects)

    def test_missing_historical_output_becomes_source_missing(self):
        self.add_job()
        manager = self.manager(FakeBackend())
        item = self.history_item("missing.png")
        manager.enqueue_completion("prompt-1", "owner-1", 6009, "completed", item, 200)
        artifact = manager.store.claim_artifact("worker", 2)
        manager._upload_artifact(artifact)
        row = manager.store.artifacts("prompt-1")[0]
        self.assertEqual(cloud.SOURCE_MISSING, row["upload_status"])

    def test_exactly_three_outer_failures_are_terminal(self):
        self.add_job()
        source = self.output / "result.png"
        source.write_bytes(b"png")
        manager = self.manager(FakeBackend(fail=True))
        manager.enqueue_completion(
            "prompt-1", "owner-1", 6009, "completed", self.history_item("result.png"), 200
        )
        for attempt in range(3):
            with closing(sqlite3.connect(self.scheduler_db)) as connection:
                with connection:
                    connection.execute(
                        "UPDATE scheduler_cloud_artifacts SET next_attempt_at=0 WHERE prompt_id='prompt-1'"
                    )
            artifact = manager.store.claim_artifact("worker", 2)
            self.assertIsNotNone(artifact)
            manager._upload_artifact(artifact)
        row = manager.store.artifacts("prompt-1")[0]
        self.assertEqual(3, row["attempt_count"])
        self.assertEqual(cloud.CLOUD_FAILED, row["upload_status"])

    def test_exactly_three_manifest_failures_make_task_cloud_failed(self):
        self.add_job(status="failed")
        manager = self.manager(FakeBackend(fail=True))
        manager.enqueue_completion("prompt-1", "owner-1", 6009, "failed", {}, 200)
        for _ in range(3):
            with closing(sqlite3.connect(self.scheduler_db)) as connection:
                with connection:
                    connection.execute(
                        "UPDATE scheduler_cloud_tasks SET manifest_next_attempt_at=0 WHERE prompt_id='prompt-1'"
                    )
            task = manager.store.claim_manifest("worker", 3, 2)
            self.assertIsNotNone(task)
            manager._upload_manifest(task)
        task = manager.store.task("prompt-1")
        self.assertEqual(3, task["manifest_attempt_count"])
        self.assertEqual("failed", task["manifest_upload_status"])
        self.assertEqual(cloud.CLOUD_FAILED, task["cloud_status"])

    def test_expired_artifact_lease_can_be_taken_over(self):
        self.add_job()
        source = self.output / "result.png"
        source.write_bytes(b"png")
        manager = self.manager(FakeBackend())
        manager.enqueue_completion(
            "prompt-1", "owner-1", 6009, "completed", self.history_item("result.png"), 200
        )
        first = manager.store.claim_artifact("worker-a", 2, lease_seconds=0)
        second = manager.store.claim_artifact("worker-b", 2)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(2, second["attempt_count"])

    def test_same_artifact_reference_can_be_archived_for_two_prompts(self):
        self.add_job("prompt-1")
        self.add_job("prompt-2")
        source = self.output / "shared.png"
        source.write_bytes(b"shared")
        item = self.history_item("shared.png", asset_id="asset-shared")
        manager = self.manager(FakeBackend())
        manager.enqueue_completion("prompt-1", "owner-1", 6009, "completed", item, 200)
        manager.enqueue_completion("prompt-2", "owner-1", 6009, "completed", item, 200)
        first = manager.store.artifacts("prompt-1")[0]
        second = manager.store.artifacts("prompt-2")[0]
        self.assertNotEqual(first["oss_object_key"], second["oss_object_key"])
        self.assertIn("/prompt-1/", first["oss_object_key"])
        self.assertIn("/prompt-2/", second["oss_object_key"])

    def test_manifest_preserves_blob_raw_history_and_overwritten_asset_relation(self):
        self.add_job()
        item = self.history_item(asset_id="asset-shared")
        self.history.save("prompt-1", item, 100)
        raw_blob = b"\x00\xffapi-body"
        with closing(sqlite3.connect(self.scheduler_db)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO scheduler_api_logs(
                        prompt_id, recorded_at, client, method, url, request_headers,
                        request_body, response_status, response_headers, response_body, error
                    ) VALUES (?, 1, 'test', 'POST', 'https://api.example', '{}', ?, 200, '{}', ?, NULL)
                    """,
                    ("prompt-1", raw_blob, sqlite3.Binary(b"response")),
                )
                connection.execute(
                    """
                    INSERT INTO scheduler_assets(
                        asset_id, owner_id, prompt_id, worker_port, file_path, data, updated_at
                    ) VALUES ('asset-shared', 'owner-1', 'different-prompt', 6009, '', '{}', 1)
                    """
                )
        manager = self.manager(FakeBackend())
        manager.enqueue_completion("prompt-1", "owner-1", 6009, "completed", item, 200)
        manifest = manager.exporter.build("prompt-1", self.config.server_id, cloud.NO_ARTIFACTS)
        body = manifest["records"]["scheduler_api_logs"][0]["request_body"]
        self.assertEqual("blob", body["$sqlite_type"])
        self.assertEqual(hashlib.sha256(raw_blob).hexdigest(), body["sha256"])
        self.assertEqual(
            ["history.outputs.asset_id"],
            manifest["records"]["scheduler_assets"][0]["relation_basis"],
        )
        history_record = manifest["records"]["history"]
        self.assertIsInstance(history_record["row"]["data"], str)
        self.assertEqual("asset-shared", history_record["parsed_data"]["outputs"]["1"]["images"][0]["id"])
        self.assertNotIn("sqlite_sequence", manifest["database_schema"])

    def test_manifest_canonical_content_hash_is_reproducible(self):
        self.add_job()
        manager = self.manager(FakeBackend())
        manager.enqueue_completion("prompt-1", "owner-1", 6009, "completed", {}, 200)
        manifest = manager.exporter.build("prompt-1", self.config.server_id, cloud.NO_ARTIFACTS)
        expected = manifest["integrity"]["manifest_content_sha256"]
        manifest["integrity"]["manifest_content_sha256"] = None
        canonical = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(expected, hashlib.sha256(canonical).hexdigest())

    def test_manifest_does_not_export_oss_environment_credentials(self):
        self.add_job()
        os.environ["OSS_ACCESS_KEY_ID"] = "SHOULD_NOT_APPEAR"
        os.environ["OSS_ACCESS_KEY_SECRET"] = "SHOULD_NOT_APPEAR_EITHER"
        try:
            manager = self.manager(FakeBackend())
            manager.enqueue_completion("prompt-1", "owner-1", 6009, "completed", {}, 200)
            encoded = json.dumps(
                manager.exporter.build(
                    "prompt-1", self.config.server_id, cloud.NO_ARTIFACTS
                )
            )
        finally:
            os.environ.pop("OSS_ACCESS_KEY_ID", None)
            os.environ.pop("OSS_ACCESS_KEY_SECRET", None)
        self.assertNotIn("SHOULD_NOT_APPEAR", encoded)


if __name__ == "__main__":
    unittest.main()

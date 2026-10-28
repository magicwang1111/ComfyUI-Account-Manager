import importlib
import json
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx

package = types.ModuleType("recovery_test_utils")
package.__path__ = [str(Path(__file__).parents[1] / "utils")]
sys.modules[package.__name__] = package
recovery = importlib.import_module("recovery_test_utils.api_recovery")
SchedulerStore = importlib.import_module("recovery_test_utils.scheduler_store").SchedulerStore
HistoryStore = importlib.import_module("recovery_test_utils.history_store").HistoryStore


class ApiRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scheduler = SchedulerStore(str(self.root / "scheduler.db"))
        self.history = HistoryStore(str(self.root / "history.db"))
        self.scheduler.enqueue((0, "job-1", {}, {}, [], {}), "user-a", 8180)
        self.set_job_status("running")
        self.manager = self.make_manager()

    def tearDown(self):
        self.temp.cleanup()

    def make_manager(self):
        return recovery.ApiRecoveryManager(self.scheduler, self.history, b"test-secret", str(self.root / "output"))

    def set_job_status(self, status):
        with closing(self.scheduler._connect()) as db:
            db.execute("UPDATE scheduler_jobs SET status=? WHERE prompt_id='job-1'", (status,))

    def capture(self):
        request = types.SimpleNamespace(
            method="POST", url="https://api.example/api/v3/contents/generations/tasks",
            headers={"Authorization": "Bearer private-key"},
        )
        response = types.SimpleNamespace(status_code=200, content=b'{"id":"task-1"}')
        self.manager.observe("job-1", "7", request, response)

    def rows(self):
        with closing(self.scheduler._connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM scheduler_api_recovery")]

    def download(self, url, path, max_bytes):
        Path(path).write_bytes(b"video-data")
        return "video/mp4", 10

    def test_restart_recovers_only_original_task_and_is_idempotent(self):
        self.capture()
        self.assertNotIn("private-key", str(self.rows()))
        self.set_job_status("worker_lost")
        restarted = self.make_manager()
        with patch.object(restarted, "_query", return_value={"status": "succeeded", "content": {"video_url": "https://cdn.example/a.mp4"}}) as query, patch.object(recovery, "download_remote", side_effect=self.download) as download:
            self.assertTrue(restarted.run_once())
            self.assertFalse(restarted.run_once())
        self.assertEqual(1, query.call_count)
        self.assertEqual(1, download.call_count)
        self.assertEqual("recovered", self.rows()[0]["state"])
        self.assertIsNone(self.rows()[0]["credentials"])
        visible = self.history.query(owner_id="user-a")["job-1"]
        self.assertEqual("error", visible["status"]["status_str"])
        self.assertFalse(visible["status"]["completed"])
        self.assertEqual({}, self.history.query(owner_id="user-b"))
        videos = [ref for output in visible["outputs"].values() for ref in output.get("video", [])]
        self.assertEqual(1, len(videos))
        self.assertTrue((self.root / "output" / videos[0]["subfolder"] / videos[0]["filename"]).is_file())

    def test_running_completed_cancelled_tasks_never_requery(self):
        for status in ("running", "completed", "cancelled", "failed"):
            with self.subTest(status=status):
                self.set_job_status(status)
                self.capture()
                with patch.object(self.manager, "_query") as query:
                    self.manager.run_once()
                query.assert_not_called()

    def test_pending_and_network_failure_retry_without_duplicate_submit(self):
        self.capture()
        self.set_job_status("worker_lost")
        with patch.object(self.manager, "_query", return_value={"status": "running"}):
            self.manager.run_once()
        self.assertEqual("pending", self.rows()[0]["state"])
        with closing(self.scheduler._connect()) as db:
            db.execute("UPDATE scheduler_api_recovery SET next_check=0")
        with patch.object(self.manager, "_query", side_effect=TimeoutError("private-key must not be logged")):
            self.manager.run_once()
        self.assertEqual("pending", self.rows()[0]["state"])
        self.assertNotIn("private-key", str(self.rows()))

    def test_provider_failure_is_visible_and_stops_polling(self):
        self.capture()
        self.set_job_status("worker_lost")
        with patch.object(self.manager, "_query", return_value={"status": "failed"}):
            self.manager.run_once()
        self.assertEqual("failed", self.rows()[0]["state"])
        item = self.history.query(owner_id="user-a")["job-1"]
        self.assertIn("第三方任务失败", str(item))

    def test_cross_worker_lock_prevents_duplicate_queries(self):
        self.capture()
        self.set_job_status("worker_lost")
        sibling = self.make_manager()
        with self.manager.lock:
            with patch.object(sibling, "_query") as query:
                self.assertFalse(sibling.run_once())
            query.assert_not_called()

    def test_duplicate_capture_does_not_reset_recovery(self):
        self.capture()
        self.capture()
        self.assertEqual(1, len(self.rows()))

    def test_audit_boundary_checkpoints_submission_without_logging_credentials(self):
        audit = importlib.import_module("recovery_test_utils.api_audit")
        records = []
        with patch.object(audit, "_recorder", lambda **record: records.append(record)), \
             patch.object(audit, "_observer", lambda prompt, request, response: self.manager.observe(prompt, "7", request, response)), \
             patch.object(audit, "current_job", return_value="job-1"):
            # Use the same HTTP response type consumed by the real audit boundary.
            with httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"id": "task-1"})
            )) as client:
                response = client.post("https://api.example/api/v3/contents/generations/tasks",
                                       headers={"Authorization": "Bearer private-key"})
                audit._record("httpx", response.request, response)
        self.assertEqual("task-1", self.rows()[0]["task_id"])
        self.assertNotIn("private-key", str(records))

    def test_recovered_video_is_previewable_in_installed_jobs_api(self):
        sys.path.insert(0, str(Path(__file__).parents[3]))
        from comfy_execution.jobs import normalize_history_item

        self.capture()
        self.set_job_status("worker_lost")
        with patch.object(self.manager, "_query", return_value={"status": "succeeded", "content": {"video_url": "https://cdn.example/a.mp4"}}), patch.object(recovery, "download_remote", side_effect=self.download):
            self.manager.run_once()
        history = self.history.query(owner_id="user-a", for_jobs=True)["job-1"]
        job = normalize_history_item("job-1", history, include_outputs=True)
        self.assertEqual("failed", job["status"])
        self.assertEqual(1, job["previewable_outputs_count"])
        self.assertEqual("000.mp4", job["preview_output"]["filename"])

    def test_crash_after_history_commit_reuses_file_and_history(self):
        self.capture()
        self.set_job_status("worker_lost")
        body = {"status": "succeeded", "content": {"video_url": "https://cdn.example/a.mp4"}}
        self.manager.notify = lambda: (_ for _ in ()).throw(RuntimeError("Simulated exit"))
        with patch.object(self.manager, "_query", return_value=body), patch.object(recovery, "download_remote", side_effect=self.download) as download:
            with self.assertRaises(RuntimeError):
                self.manager.run_once()
            self.manager.notify = None
            self.manager.run_once()
        self.assertEqual(1, download.call_count)
        item = self.history.query(owner_id="user-a")["job-1"]
        self.assertEqual(1, len(item["api_recovery"]))
        self.assertEqual(1, len(item["outputs"]))

    def test_history_preserves_previous_outputs(self):
        self.history.save("job-1", {"user_id": "user-a", "outputs": {"3": {"text": ["original"]}},
                                   "status": {"status_str": "error", "completed": False}}, 100)
        self.capture()
        self.set_job_status("worker_lost")
        with patch.object(self.manager, "_query", return_value={"status": "running"}):
            self.manager.run_once()
        item = self.history.query(owner_id="user-a")["job-1"]
        self.assertEqual(["original"], item["outputs"]["3"]["text"])

    def test_query_uses_original_id_and_never_follows_auth_redirect(self):
        self.capture()
        row = self.rows()[0]
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.json.return_value = {"status": "running"}
        with patch.object(recovery, "_validate_public_http_url"), patch.object(recovery.requests, "request", return_value=response) as request:
            self.manager._query(row)
        args, kwargs = request.call_args
        self.assertEqual(("GET", "https://api.example/api/v3/contents/generations/tasks/task-1"), args)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual("Bearer private-key", kwargs["headers"]["Authorization"])

    def test_auth_failure_stops_and_retains_task_identity_without_credentials(self):
        self.capture()
        self.set_job_status("worker_lost")
        with patch.object(self.manager, "_query", side_effect=recovery.RecoveryQueryError("恢复查询凭证已失效")):
            self.manager.run_once()
        row = self.rows()[0]
        self.assertEqual("failed", row["state"])
        self.assertEqual("task-1", row["task_id"])
        self.assertIsNone(row["credentials"])

    def test_partial_download_is_removed_and_retried(self):
        self.capture()
        self.set_job_status("worker_lost")
        body = {"status": "succeeded", "content": {"video_url": "https://cdn.example/a.mp4"}}
        def interrupted(url, destination, maximum):
            Path(destination).write_bytes(b"incomplete")
            raise TimeoutError()
        with patch.object(self.manager, "_query", return_value=body), patch.object(recovery, "download_remote", side_effect=interrupted):
            self.manager.run_once()
        self.assertEqual([], list((self.root / "output").rglob("*.mp4")))
        self.assertEqual([], list((self.root / "output").rglob("*.part")))
        self.assertEqual("pending", self.rows()[0]["state"])

    def test_captured_body_without_auth_or_with_invalid_json_is_ignored(self):
        request = types.SimpleNamespace(method="POST", url="https://api.example/api/v3/contents/generations/tasks", headers={})
        for content in (b'{"id":"x"}', b'not json'):
            self.manager.observe("job-1", "7", request, types.SimpleNamespace(status_code=200, content=content))
        self.assertEqual([], self.rows())

    def test_original_worker_finishing_during_query_cancels_recovery(self):
        self.capture()
        self.set_job_status("worker_lost")
        def complete(row):
            self.set_job_status("completed")
            return {"status": "succeeded", "content": {"video_url": "https://cdn.example/a.mp4"}}
        with patch.object(self.manager, "_query", side_effect=complete), patch.object(recovery, "download_remote") as download:
            self.manager.run_once()
        download.assert_not_called()
        self.assertEqual("ignored", self.rows()[0]["state"])
        self.assertEqual({}, self.history.query())

    def test_successful_history_is_not_overwritten_before_scheduler_completion(self):
        self.capture()
        self.set_job_status("worker_lost")
        self.history.save("job-1", {"user_id": "user-a", "outputs": {"3": {"text": ["success"]}},
                                   "status": {"status_str": "success", "completed": True}}, 100)
        with patch.object(self.manager, "_query", return_value={"status": "running"}):
            self.manager.run_once()
        item = self.history.query()["job-1"]
        self.assertNotIn("api_recovery", item)
        self.assertEqual("ignored", self.rows()[0]["state"])


if __name__ == "__main__":
    unittest.main()

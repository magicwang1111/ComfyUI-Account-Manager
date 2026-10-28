import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "recovery_providers", Path(__file__).parents[1] / "utils/recovery_providers.py"
)
providers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(providers)


class RecoveryProviderTests(unittest.TestCase):
    def test_submissions_map_to_query_only_endpoints(self):
        cases = [
            ("/api/v3/contents/generations/tasks", {"id": "task-1"}, "ark", "GET", "/api/v3/contents/generations/tasks/task-1", None),
            ("/v1/videos/generations", {"output": {"task_id": "task-1"}}, "wan", "GET", "/v1/tasks/task-1", None),
            ("/qwen/api/v1/services/aigc/video-generation/generation", {"output": {"task_id": "task-1"}}, "wan", "GET", "/qwen/api/v1/tasks/video/task-1", None),
            ("/v1/videos/image2video", {"code": 0, "data": {"task_id": "task-1"}}, "kling", "GET", "/v1/videos/image2video/task-1", None),
            ("/v1/kling/text-to-video/submit", {"Response": {"JobId": "task-1"}}, "vapeur_kling", "POST", "/v1/kling/text-to-video/describe", {"JobId": "task-1"}),
            ("/v1/videos/generations", {"data": {"task_id": "task-1"}}, "wan", "GET", "/v1/tasks/task-1", None),
            ("/v1/videos/generations", {"data": {"id": "task-1"}}, "wan", "GET", "/v1/tasks/task-1", None),
        ]
        for path, response, provider, method, query, body in cases:
            with self.subTest(path=path):
                result = providers.submission("https://api.example" + path, response)
                self.assertEqual((provider, "task-1", method, "https://api.example" + query, body), result)

    def test_unknown_invalid_and_failed_submissions_are_not_replayed(self):
        for path, response in [
            ("/unknown/tasks", {"id": "task-1"}),
            ("/api/v3/contents/generations/tasks", {"id": "../delete"}),
            ("/v1/videos/image2video", {"code": 12, "data": {"task_id": "task-1"}}),
        ]:
            self.assertIsNone(providers.submission("https://api.example" + path, response))

    def test_results_extract_only_generated_media(self):
        cases = [
            ("ark", {"status": "succeeded", "content": {"video_url": "https://cdn.example/a.mp4"}}),
            ("wan", {"output": {"task_status": "SUCCEEDED", "video_url": "https://cdn.example/a.mp4"}}),
            ("kling", {"code": 0, "data": {"task_status": "succeed", "task_result": {"videos": [{"url": "https://cdn.example/a.mp4"}]}}}),
            ("vapeur_kling", {"Response": {"Status": "DONE", "ResultVideoUrl": "https://cdn.example/a.mp4"}}),
        ]
        for provider, body in cases:
            body["input"] = {"video_url": "https://cdn.example/input.mp4"}
            self.assertEqual(("succeeded", ["https://cdn.example/a.mp4"]), providers.result(provider, body))

    def test_pending_failed_and_missing_output_are_distinct(self):
        self.assertEqual(("pending", []), providers.result("ark", {"status": "running"}))
        self.assertEqual(("failed", []), providers.result("ark", {"status": "expired"}))
        with self.assertRaises(ValueError):
            providers.result("ark", {"status": "succeeded"})


if __name__ == "__main__":
    unittest.main()

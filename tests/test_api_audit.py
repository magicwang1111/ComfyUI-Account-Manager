import importlib.util
import json
import threading
import unittest
from pathlib import Path


module_path = Path(__file__).parents[1] / "utils" / "api_audit.py"
spec = importlib.util.spec_from_file_location("api_audit", module_path)
api_audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api_audit)


class ApiAuditTests(unittest.TestCase):
    def tearDown(self):
        api_audit.clear_current_job()
        api_audit._recorder = None

    def test_records_original_request_and_response_bodies(self):
        records = []
        api_audit._recorder = lambda **record: records.append(record)
        api_audit.set_current_job("job-1")

        request = type(
            "Request",
            (),
            {
                "method": "POST",
                "url": "https://api.example/tasks?token=secret&mode=video",
                "headers": {"Authorization": "Bearer secret", "Content-Type": "application/json"},
                "body": b'{"prompt":"original"}',
            },
        )()
        response = type(
            "Response",
            (),
            {
                "status_code": 200,
                "headers": {"Content-Type": "application/json"},
                "content": b'{"task_id":"task-123","status":"running"}',
            },
        )()

        api_audit._record("requests", request, response)

        self.assertEqual(1, len(records))
        self.assertEqual("job-1", records[0]["prompt_id"])
        self.assertEqual(b'{"prompt":"original"}', records[0]["request_body"])
        self.assertEqual(
            b'{"task_id":"task-123","status":"running"}',
            records[0]["response_body"],
        )
        self.assertIn("token=%5BREDACTED%5D", records[0]["url"])
        self.assertEqual("[REDACTED]", json.loads(records[0]["request_headers"])["Authorization"])

    def test_background_thread_uses_single_worker_fallback_job(self):
        seen = []
        api_audit.set_current_job("job-thread")
        thread = threading.Thread(target=lambda: seen.append(api_audit.current_job()))
        thread.start()
        thread.join()
        self.assertEqual(["job-thread"], seen)

    def test_installed_hooks_capture_requests_and_httpx(self):
        import httpx
        import requests
        from requests.adapters import BaseAdapter

        records = []
        api_audit.install_api_audit(lambda **record: records.append(record))
        api_audit.set_current_job("hook-job")

        class Adapter(BaseAdapter):
            def send(self, request, **kwargs):
                response = requests.Response()
                response.status_code = 200
                response.headers["Content-Type"] = "application/json"
                response._content = b'{"task_id":"requests-task"}'
                response.request = request
                return response

            def close(self):
                pass

        session = requests.Session()
        session.mount("https://", Adapter())
        session.post("https://requests.example/tasks", json={"prompt": "one"})

        def handler(request):
            return httpx.Response(200, json={"task_id": "httpx-task"})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            client.post("https://httpx.example/tasks", json={"prompt": "two"})

        self.assertEqual(["requests", "httpx"], [record["client"] for record in records])
        self.assertIn(b"requests-task", records[0]["response_body"])
        self.assertIn(b"httpx-task", records[1]["response_body"])


if __name__ == "__main__":
    unittest.main()

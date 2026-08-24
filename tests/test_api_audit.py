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

    def test_binary_media_response_is_replaced_with_small_reference(self):
        records = []
        api_audit._recorder = lambda **record: records.append(record)
        api_audit.set_current_job("job-video")
        video = b"\x00\x00\x00\x18ftypmp42" + (b"x" * 100000)
        request = type(
            "Request",
            (),
            {"method": "GET", "url": "https://oss.example/output.mp4", "headers": {}, "body": None},
        )()
        response = type(
            "Response",
            (),
            {"status_code": 200, "headers": {"Content-Type": "video/mp4"}, "content": video},
        )()

        api_audit._record("requests", request, response)

        reference = json.loads(records[0]["response_body"])["$account_manager_body_ref"]
        self.assertFalse(reference["stored"])
        self.assertEqual("video/mp4", reference["content_type"])
        self.assertEqual(len(video), reference["content_bytes"])
        self.assertEqual(api_audit.hashlib.sha256(video).hexdigest(), reference["sha256"])
        self.assertLess(len(records[0]["response_body"]), 300)

    def test_json_keeps_api_fields_but_removes_embedded_base64_media(self):
        records = []
        api_audit._recorder = lambda **record: records.append(record)
        api_audit.set_current_job("job-image")
        image_data = "a" * (api_audit.LARGE_MEDIA_STRING_BYTES + 1)
        body = json.dumps(
            {"prompt": "keep me", "first_frame_image": image_data}
        ).encode()
        request = type(
            "Request",
            (),
            {
                "method": "POST",
                "url": "https://api.example/tasks",
                "headers": {"Content-Type": "application/json"},
                "body": body,
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

        stored_request = json.loads(records[0]["request_body"])
        stored_response = json.loads(records[0]["response_body"])
        self.assertEqual("keep me", stored_request["prompt"])
        self.assertNotIn(image_data, records[0]["request_body"].decode())
        media = stored_request["first_frame_image"]["$account_manager_media_ref"]
        self.assertEqual(len(image_data), media["encoded_bytes"])
        self.assertEqual("task-123", stored_response["task_id"])
        self.assertEqual("running", stored_response["status"])

    def test_multipart_request_body_is_not_stored(self):
        raw = b"multipart media" * 10000
        stored = api_audit._audit_body(
            raw, {"Content-Type": "multipart/form-data; boundary=example"}
        )
        reference = json.loads(stored)["$account_manager_body_ref"]
        self.assertEqual("multipart_body", reference["reason"])
        self.assertEqual(len(raw), reference["content_bytes"])
        self.assertLess(len(stored), 300)

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

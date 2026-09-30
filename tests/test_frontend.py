import asyncio
import contextvars
import importlib.util
import json
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

sys.path.insert(0, str(Path(__file__).parents[3]))
spec = importlib.util.spec_from_file_location("frontend", Path(__file__).parents[1] / "utils" / "frontend.py")
frontend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frontend)


class OriginTests(unittest.IsolatedAsyncioTestCase):
    def middleware(self, resolver):
        @web.middleware
        async def origin_only_middleware(request, handler):
            raise AssertionError("Unpatched core middleware ran")

        app = web.Application(middlewares=[origin_only_middleware])
        frontend.install_origin_middleware(app, resolver)
        return app.middlewares[0]

    async def test_same_origin_skips_dns(self):
        for host, origin in [
            ("example.com:8443", "https://example.com:8443"),
            ("EXAMPLE.COM:8443", "https://example.com:8443"),
            ("127.0.0.1:8188", "http://127.0.0.1:8188"),
            ("example.com:8443", "https://example.com"),
        ]:
            with self.subTest(host=host, origin=origin):
                resolver = Mock(side_effect=AssertionError("Unexpected DNS lookup"))
                request = make_mocked_request("GET", "/", headers={"Host": host, "Origin": origin})
                handler = AsyncMock(return_value=web.Response())
                response = await self.middleware(resolver)(request, handler)
                self.assertEqual(200, response.status)
                resolver.assert_not_called()

    async def test_cross_site_rejected(self):
        resolver = Mock()
        request = make_mocked_request("POST", "/", headers={"Sec-Fetch-Site": "cross-site"})
        handler = AsyncMock()
        response = await self.middleware(resolver)(request, handler)
        self.assertEqual(403, response.status)
        handler.assert_not_awaited()
        resolver.assert_not_called()

    async def test_mismatched_origin_keeps_loopback_protection(self):
        for loopback, expected in [(True, 403), (False, 200)]:
            with self.subTest(loopback=loopback):
                resolver = Mock(return_value=loopback)
                request = make_mocked_request("GET", "/", headers={
                    "Host": "example.com:8188", "Origin": "https://other.example:8188",
                })
                response = await self.middleware(resolver)(request, AsyncMock(return_value=web.Response()))
                self.assertEqual(expected, response.status)
                resolver.assert_called_once_with("example.com")

    async def test_dns_runs_outside_event_loop(self):
        main_thread = threading.get_ident()

        def resolver(host):
            self.assertNotEqual(main_thread, threading.get_ident())
            return True

        request = make_mocked_request("GET", "/", headers={
            "Host": "example.com", "Origin": "https://other.example",
        })
        response = await self.middleware(resolver)(request, AsyncMock())
        self.assertEqual(403, response.status)

    def test_configured_cors_is_unchanged(self):
        @web.middleware
        async def cors_middleware(request, handler):
            return await handler(request)

        app = web.Application(middlewares=[cors_middleware])
        frontend.install_origin_middleware(app, Mock())
        self.assertIs(cors_middleware, app.middlewares[0])


class JobsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.queue = SimpleNamespace(get_current_queue_volatile=lambda: ([], []))

    @staticmethod
    def history(user="user-a"):
        return {str(n): {
            "prompt": [n, str(n), {}, {"create_time": n, "extra_pnginfo": {"workflow": {"id": user}}}, []],
            "outputs": {},
            "status": {"status_str": "success", "completed": True, "messages": []},
        } for n in range(3)}

    async def test_filter_sort_and_pagination(self):
        middleware = frontend.create_jobs_middleware(self.queue, self.history)
        request = make_mocked_request("GET", "/api/jobs?status=completed&workflow_id=user-a&limit=1&offset=1")
        response = await middleware(request, AsyncMock())
        data = json.loads(response.text)
        self.assertEqual(["1"], [job["id"] for job in data["jobs"]])
        self.assertEqual({"offset": 1, "limit": 1, "total": 3, "has_more": True}, data["pagination"])

    async def test_invalid_query_does_not_read_history(self):
        history = Mock(side_effect=AssertionError("History should not be read"))
        middleware = frontend.create_jobs_middleware(self.queue, history)
        for query in ("limit=0", "limit=abc", "offset=abc", "status=bad", "sort_by=bad", "sort_order=bad"):
            with self.subTest(query=query):
                response = await middleware(make_mocked_request("GET", "/api/jobs?" + query), AsyncMock())
                self.assertEqual(400, response.status)

    async def test_details_and_other_methods_use_core_handler(self):
        history = Mock(side_effect=AssertionError("Unexpected list query"))
        middleware = frontend.create_jobs_middleware(self.queue, history)
        for method, path in [("GET", "/api/jobs/job-id"), ("POST", "/api/jobs"), ("GET", "/history")]:
            with self.subTest(method=method, path=path):
                handler = AsyncMock(return_value=web.Response(status=202))
                response = await middleware(make_mocked_request(method, path), handler)
                self.assertEqual(202, response.status)
                handler.assert_awaited_once()

    async def test_auth_context_and_responsiveness_with_unpatched_core(self):
        current_user = contextvars.ContextVar("user")
        entered = threading.Event()
        release = threading.Event()

        def history():
            owner = current_user.get()
            entered.set()
            self.assertTrue(release.wait(3))
            return self.history(owner)

        @web.middleware
        async def auth(request, handler):
            if request.path == "/api/jobs" and "X-Test-User" not in request.headers:
                return web.Response(status=401)
            current_user.set(request.headers.get("X-Test-User"))
            return await handler(request)

        async def unpatched_jobs(request):
            raise AssertionError("Unpatched core jobs handler ran")

        async def login(request):
            return web.Response(text="login")

        app = web.Application(middlewares=[auth, frontend.create_jobs_middleware(self.queue, history)])
        app.router.add_get("/api/jobs", unpatched_jobs)
        app.router.add_get("/login", login)
        async with TestClient(TestServer(app)) as client:
            self.assertEqual(401, (await client.get("/api/jobs")).status)
            job = asyncio.create_task(client.get("/api/jobs", headers={"X-Test-User": "user-b"}))
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                self.assertEqual(200, (await asyncio.wait_for(client.get("/login"), 1)).status)
            finally:
                release.set()
            response = await job
            data = await response.json()
            self.assertEqual({"user-b"}, {item["workflow_id"] for item in data["jobs"]})


if __name__ == "__main__":
    unittest.main()

import importlib
import importlib.util
import sys
import types
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

spec = importlib.util.spec_from_file_location(
    "task_sessions", Path(__file__).resolve().parents[1] / "utils/task_sessions.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TaskSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.queue = SimpleNamespace(mutex=threading.Lock(), currently_running={
            0: (0, "alice-job", {}, {}, [], {}, "alice"),
            1: (1, "bob-job", {}, {}, [], {}, "bob"),
        })
        self.users = SimpleNamespace(get_user=lambda user_id: (user_id, {"username": user_id}))
        self.jwt = SimpleNamespace(create_access_token=Mock(side_effect=lambda claims, **kw: claims["id"] + "-token"))
        self.scheduler = Mock(
            get_job=lambda pid: {"owner_id": pid.split("-")[0], "status": "running"},
            worker_ports=lambda: [8180, 8181],
            idle_worker_ports=lambda resource: [8181],
            classify_item=lambda item: "api",
            resource_concurrency_limits={"api": 2}, max_concurrent_jobs_per_user=2,
            running_jobs=lambda owner=None, **kw: [{"resource_class": "api", "worker_port": 8180}],
        )
        self.sessions = module.TaskSessions(
            SimpleNamespace(prompt_queue=self.queue, port=8183), self.users, self.jwt, self.scheduler
        )

    def test_each_task_uses_its_queue_owner(self):
        for user in ("alice", "bob"):
            env = self.sessions.environment(user + "-job")
            self.assertEqual(env["COMFY_TASK_TOKEN"], user + "-token")
            self.assertEqual(env["COMFY_TASK_BASE_URL"], "http://127.0.0.1:8183")
            self.jwt.create_access_token.assert_called_with(
                {"id": user, "username": user, "am_task": user + "-job"}, expire_minutes=120
            )

    def test_unknown_task_never_falls_back_to_another_user(self):
        with self.assertRaises(ValueError):
            self.sessions.environment("unknown")
        self.jwt.create_access_token.assert_not_called()

    async def test_finished_parent_or_wrong_owner_is_denied(self):
        request = SimpleNamespace(method="GET", path="/history/video")
        with self.assertRaises(web.HTTPUnauthorized):
            await self.sessions.authorize(request, {"am_task": "alice-job", "id": "bob"})
        self.scheduler.get_job = lambda _: {"owner_id": "alice", "status": "completed"}
        with self.assertRaises(web.HTTPUnauthorized):
            await self.sessions.authorize(request, {"am_task": "alice-job", "id": "alice"})

    async def test_task_session_cannot_manage_accounts(self):
        with self.assertRaises(web.HTTPForbidden):
            await self.sessions.authorize(SimpleNamespace(method="POST", path="/users"),
                                          {"am_task": "alice-job", "id": "alice"})

    async def test_submission_preserves_concurrency_limit(self):
        request = SimpleNamespace(method="POST", path="/api/prompt", json=AsyncMock(return_value={"prompt": {}}))
        claims = {"am_task": "alice-job", "id": "alice"}
        await self.sessions.authorize(request, claims)
        self.scheduler.running_jobs = lambda _: [{"resource_class": "api"}] * 2
        with self.assertRaises(web.HTTPConflict):
            await self.sessions.authorize(request, claims)

    async def test_single_worker_child_submission_is_denied(self):
        self.sessions.scheduler = None
        with self.assertRaises(web.HTTPConflict):
            await self.sessions.authorize(SimpleNamespace(method="POST", path="/prompt"),
                                          {"am_task": "alice-job", "id": "alice"})

    async def test_all_workers_busy_rejects_child_instead_of_waiting(self):
        self.scheduler.idle_worker_ports = lambda resource: []
        request = SimpleNamespace(method="POST", path="/prompt", json=AsyncMock(return_value={"prompt": {}}))
        with self.assertRaises(web.HTTPConflict):
            await self.sessions.authorize(request, {"am_task": "alice-job", "id": "alice"})

    async def test_parent_port_cannot_count_as_available_child_worker(self):
        self.scheduler.idle_worker_ports = lambda resource: [8183]
        request = SimpleNamespace(method="POST", path="/prompt", json=AsyncMock(return_value={"prompt": {}}))
        with self.assertRaises(web.HTTPConflict):
            await self.sessions.authorize(request, {"am_task": "alice-job", "id": "alice"})


class TaskSessionHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_bearer_requests_use_actual_owner_and_expire_with_parent(self):
        package = types.ModuleType("task_session_test_utils")
        package.__path__ = [str(Path(__file__).resolve().parents[1] / "utils")]
        stub = types.ModuleType(package.__name__ + ".access_control")
        stub.AccessControl = object
        with patch.dict(sys.modules, {package.__name__: package, stub.__name__: stub}):
            jwt_module = importlib.import_module(package.__name__ + ".jwt_auth")
        users = SimpleNamespace(get_user=lambda username=None, user_id=None: (username or user_id, {"username": username or user_id}))
        access = SimpleNamespace(sync_comfy_users=Mock(), set_current_user_id=Mock())
        auth = jwt_module.JWTAuth(users, access, Mock(), "test-only-signing-key-at-least-32-bytes")
        queue = SimpleNamespace(mutex=threading.Lock(), currently_running={
            0: (0, "alice-job", {}, {}, [], {}, "alice"),
            1: (1, "bob-job", {}, {}, [], {}, "bob"),
        })
        auth.task_sessions = module.TaskSessions(SimpleNamespace(prompt_queue=queue, port=8183), users, auth, None)
        app = web.Application(middlewares=[auth.create_jwt_middleware()])
        async def echo_owner(request):
            return web.json_response({"owner": request["user_id"]})
        app.router.add_get('/object_info', echo_owner)
        app.router.add_post('/users', echo_owner)
        async with TestClient(TestServer(app)) as client:
            for user in ('alice', 'bob'):
                token = auth.task_sessions.environment(user + '-job')['COMFY_TASK_TOKEN']
                headers = {'Authorization': 'Bearer ' + token}
                response = await client.get('/object_info', headers=headers)
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())['owner'], user)
                denied = await client.post('/users', headers=headers)
                self.assertEqual(denied.status, 403)
            queue.currently_running.clear()
            response = await client.get('/object_info', headers=headers)
            self.assertEqual(response.status, 401)


if __name__ == "__main__":
    unittest.main()

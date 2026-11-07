import importlib.util
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / 'utils' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


Store = module('agent_tasks').AgentTaskStore
Routes = module('agent_task_routes').AgentTaskRoutes


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'tasks.db', concurrency=4, per_user=2)

    def create(self, owner='alice', key=None):
        return self.store.create(owner, key or str(time.time_ns()), {'job_dir': self.temp.name})

    def test_concurrent_claims_enforce_global_and_account_limits(self):
        for _ in range(6):
            self.create()
            self.create('bob')
        with ThreadPoolExecutor(max_workers=12) as pool:
            claims = [task for task in pool.map(lambda _: self.store.claim(), range(12)) if task]
        self.assertEqual(len(claims), 4)
        self.assertEqual(len({task['id'] for task in claims}), 4)
        self.assertEqual(sum(task['owner'] == 'alice' for task in claims), 2)

    def test_submission_is_idempotent_and_pending_is_bounded(self):
        first = self.create(key='one')
        self.assertEqual(self.create(key='one')['id'], first['id'])
        self.store.pending_per_user = 1
        with self.assertRaises(ValueError):
            self.create(key='two')

    def test_expired_worker_is_fenced_and_not_reexecuted(self):
        self.create()
        task = self.store.claim()
        with closing(self.store.connect()) as db, db:
            db.execute('UPDATE agent_tasks SET lease_until=0')
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.get(task['id'])['state'], 'recovery_required')
        with self.assertRaises(ValueError):
            self.store.update(task['id'], task['lease'], state='completed')

    def test_delivery_checkpoint_reclaimed_without_rerunning_agent(self):
        self.create()
        task = self.store.claim()
        self.store.update(task['id'], task['lease'], checkpoint={'text': 'done'}, state='delivering')
        with closing(self.store.connect()) as db, db:
            db.execute('UPDATE agent_tasks SET lease_until=0')
        again = self.store.claim()
        self.assertEqual(again['checkpoint'], {'text': 'done'})
        self.assertNotEqual(again['lease'], task['lease'])

    def test_cancel_completion_race_cannot_report_success(self):
        self.create()
        task = self.store.claim()
        self.store.cancel(task['id'], task['owner'])
        self.assertEqual(self.store.update(task['id'], task['lease'], state='completed'), 'cancelled')
        with self.assertRaises(ValueError):
            self.store.reserve_child(task['id'], 'video', 'hash')

    def test_event_replay_and_child_idempotency(self):
        self.create()
        task = self.store.claim()
        self.store.update(task['id'], task['lease'], events=[{'message': 'a'}, {'message': 'b'}])
        events = self.store.events(task['id'])
        self.assertEqual(self.store.events(task['id'], events[0]['seq']), events[1:])
        child, fresh = self.store.reserve_child(task['id'], 'video', 'hash')
        again, fresh_again = self.store.reserve_child(task['id'], 'video', 'hash')
        self.assertTrue(fresh)
        self.assertFalse(fresh_again)
        self.assertEqual(child['prompt_id'], again['prompt_id'])
        with self.assertRaises(ValueError):
            self.store.reserve_child(task['id'], 'video', 'different')


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'tasks.db')
        self.created = self.store.create('alice', 'one', {'job_dir': self.temp.name})
        self.task = self.store.claim()
        self.claims = {'id': 'alice', 'am_agent': self.task['id'], 'am_lease': self.task['lease']}
        self.scheduler = Mock()
        self.scheduler.get_job.return_value = None
        self.submitted = []
        sessions = SimpleNamespace(jwt_auth=Mock(), users_db=Mock())
        self.routes = Routes(self.store, sessions, Mock(verify_request=AsyncMock(return_value=False)),
                             self.scheduler, SimpleNamespace(port=8180))

        @web.middleware
        async def auth(request, handler):
            request['user_id'] = request.headers.get('X-Test-Owner', 'alice')
            if request.path in {'/prompt', '/history/foreign', '/view', '/users'}:
                return await self.routes.delegated(request, self.claims, handler)
            return await handler(request)

        async def submit(request):
            payload = await request.json()
            self.submitted.append(payload)
            return web.json_response({'prompt_id': payload['prompt_id']})

        app = web.Application(middlewares=[auth])
        routes = web.RouteTableDef()
        self.routes.install(routes)
        app.add_routes(routes)
        app.router.add_post('/prompt', submit)
        for path in ['/history/foreign', '/view', '/users']:
            app.router.add_get(path, lambda _: web.json_response({}))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_full_video_pool_still_accepts_and_deduplicates_child(self):
        self.scheduler.idle_worker_ports.return_value = []
        payload = {'prompt': {'1': {'class_type': 'Video', 'inputs': {}}}, 'client_id': 'video-1'}
        first = await self.client.post('/prompt', json=payload)
        second = await self.client.post('/prompt', json=payload)
        self.assertEqual(first.status, 200)
        self.assertEqual(await first.json(), await second.json())
        self.assertEqual(len(self.submitted), 1)
        self.scheduler.idle_worker_ports.assert_not_called()

    async def test_owner_and_task_scope_are_enforced(self):
        response = await self.client.get('/llm/agent-tasks/' + self.task['id'], headers={'X-Test-Owner': 'bob'})
        self.assertEqual(response.status, 404)
        for path in ['/history/foreign', '/view?filename=foreign.mp4', '/users']:
            self.assertEqual((await self.client.get(path)).status, 403)

    async def test_old_lease_cannot_submit_after_cancel(self):
        self.store.cancel(self.task['id'], 'alice')
        response = await self.client.post('/prompt', json={'prompt': {}, 'client_id': 'video'})
        self.assertEqual(response.status, 401)

    async def test_internal_endpoint_requires_signature(self):
        response = await self.client.post(Routes.INTERNAL, json={'action': 'claim'})
        self.assertEqual(response.status, 403)

    async def test_result_file_cannot_escape_task_directory(self):
        self.store.update(self.task['id'], self.task['lease'], state='completed',
                          result={'artifacts': [{'path': str(Path(self.temp.name).parent / 'secret')}]})
        response = await self.client.get(f"/llm/agent-tasks/{self.task['id']}/files/0")
        self.assertEqual(response.status, 404)


if __name__ == '__main__':
    unittest.main()

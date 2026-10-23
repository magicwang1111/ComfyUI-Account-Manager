import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from aiohttp.test_utils import make_mocked_request

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'utils' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AssetRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = load('scheduler_store').SchedulerStore(str(Path(self.tmp.name) / 'scheduler.db'))
        self.store.worker_ports = lambda _: [8180]
        self.routes = load('distributed_routes').DistributedRoutes(
            self.store, None, SimpleNamespace(get_user=lambda **kw: ('admin', {'admin': True})), 8181, 60)
        self.request = make_mocked_request('GET', '/api/assets')
        self.request['user_id'] = 'admin'

    async def test_browsing_does_not_replace_real_owner(self):
        self.store.upsert_asset('asset', 'alice', 'job', 8180, {})
        async def fetch(*args):
            return 200, {'assets': [{'id': 'asset'}], 'total': 1}
        self.routes._fetch_json = fetch
        response = await self.routes._aggregate_assets(self.request)
        self.assertEqual(json.loads(response.text)['total'], 1)
        self.assertEqual(self.store.get_asset('asset')['owner_id'], 'alice')
        self.assertEqual(self.store.get_asset('asset')['prompt_id'], 'job')

    async def test_unknown_asset_lookup_does_not_assign_browser_ownership(self):
        async def fetch(*args):
            return 200, {'id': 'asset'}
        self.routes._fetch_json = fetch
        self.assertEqual(await self.routes._find_asset_worker(self.request, 'asset'), 8180)
        self.assertIsNone(self.store.get_asset('asset'))

    async def test_stale_owner_does_not_block_worker_authorization(self):
        self.store.upsert_asset('asset', 'wrong-browser', '', 8180, {})
        self.routes.users_db = SimpleNamespace(get_user=lambda **kw: ('alice', {}))
        self.request['user_id'] = 'alice'
        self.assertEqual(await self.routes._find_asset_worker(self.request, 'asset'), 8180)


if __name__ == '__main__':
    unittest.main()

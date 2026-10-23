import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web, FormData
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import test_asset_access as fixtures
from app.assets.database.models import Asset, AssetContent


class AssetApiTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(fixtures.AssetAccessTests.setUpClass.__func__)
    setUp = fixtures.AssetAccessTests.setUp

    async def asyncSetUp(self):
        from app.assets.api import routes
        from app.database import db
        import folder_paths
        for name, value in [('Session', sessionmaker(bind=self.engine)),
                            ('WriteSession', sessionmaker(bind=self.engine))]:
            p = patch.object(db, name, value)
            p.start()
            self.addCleanup(p.stop)
        for method, directory in [('get_output_directory', 'output'),
                                  ('get_input_directory', 'input'),
                                  ('get_temp_directory', 'temp'),
                                  ('get_previews_directory', 'previews')]:
            p = patch.object(folder_paths, method, lambda directory=directory: str(self.root / directory))
            p.start()
            self.addCleanup(p.stop)

        @web.middleware
        async def identity(request, handler):
            request['user_id'] = request.headers.get('Test-User', '')
            return await handler(request)

        app = web.Application(middlewares=[identity, self.access.middleware()])
        routes.register_assets_routes(app, SimpleNamespace(get_request_user_id=lambda r: r['user_id']))
        async def view(request):
            if request.query.get('filename', '').startswith('blake3:'):
                from app.assets.services.asset_management import resolve_hash_to_path
                result = resolve_hash_to_path(request.query['filename'])
                return web.Response(status=200 if result else 404)
            return web.Response(text='allowed')
        app.router.add_get('/view', view)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_http_list_detail_content_and_mutations(self):
        headers = {'Test-User': 'a'}
        response = await self.client.get('/api/assets?name_contains=bob', headers=headers)
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload['total'], 0)
        self.assertEqual(payload['assets'], [])
        target = '/api/assets/' + self.ids['bob']
        for method, suffix, body in [('GET', '', None), ('GET', '/content', None),
                                     ('PUT', '', {'name': 'stolen'}),
                                     ('DELETE', '', None), ('POST', '/tags', {'tags': ['stolen']})]:
            response = await self.client.request(method, target + suffix, headers=headers, json=body)
            self.assertIn(response.status, (403, 404), (method, await response.text()))
        response = await self.client.get('/api/assets/' + self.ids['alice'] + '/content', headers=headers)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(await response.read(), b'image')
        response = await self.client.get(target, headers={'Test-User': 'admin'})
        self.assertEqual(response.status, 200)
        with Session(self.engine) as s:
            self.assertEqual(s.get(Asset, self.ids['bob']).name, 'bob')

    async def test_concurrent_requests_do_not_share_identity(self):
        async def read(user, name):
            response = await self.client.get('/api/assets?name_contains=' + name, headers={'Test-User': user})
            self.assertEqual(response.status, 200)
            return (await response.json())['total']
        values = await asyncio.gather(*(read(user, name) for user, name in
                                       [('a', 'bob'), ('b', 'alice'), ('admin', 'bob'), ('a', 'alice')] * 3))
        self.assertEqual(values, [0, 0, 1, 1] * 3)

    async def test_unknown_adapter_and_catalog_maintenance_fail_closed(self):
        response = await self.client.post('/api/assets/prune', headers={'Test-User': 'a'})
        self.assertEqual(response.status, 403)
        self.access.ready = False
        response = await self.client.get('/api/assets', headers={'Test-User': 'admin'})
        self.assertEqual(response.status, 503)

    async def test_owner_can_rename_and_tag_own_asset(self):
        path = '/api/assets/' + self.ids['alice']
        response = await self.client.put(path, headers={'Test-User': 'a'}, json={'name': 'renamed'})
        self.assertEqual(response.status, 200, await response.text())
        response = await self.client.post(path + '/tags', headers={'Test-User': 'a'}, json={'tags': ['new-tag']})
        self.assertEqual(response.status, 200, await response.text())
        response = await self.client.post(path + '/tags', headers={'Test-User': 'a'}, json={'tags': ['bob-tag']})
        self.assertEqual(response.status, 200, await response.text())

    async def test_uploaded_output_is_visible_only_to_uploader(self):
        data = FormData()
        data.add_field('tags', json.dumps(['output']))
        data.add_field('file', b'private upload', filename='private.txt', content_type='text/plain')
        response = await self.client.post('/api/assets', headers={'Test-User': 'a'}, data=data)
        self.assertIn(response.status, (200, 201), await response.text())
        asset_id = (await response.json())['id']
        path = '/api/assets/' + asset_id + '/content'
        response = await self.client.get(path, headers={'Test-User': 'a'})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(await response.read(), b'private upload')
        response = await self.client.get(path, headers={'Test-User': 'b'})
        self.assertEqual(response.status, 404)

    async def test_annotated_view_filename_cannot_bypass_output_permissions(self):
        response = await self.client.get('/view', params={
            'type': 'input', 'filename': 'b.png [output]', 'subfolder': '2026-10-22/bob',
        }, headers={'Test-User': 'a'})
        self.assertEqual(response.status, 403)
        response = await self.client.get('/view', params={
            'type': 'output', 'filename': 'a.png', 'subfolder': '2026-10-20/alice',
        }, headers={'Test-User': 'a'})
        self.assertEqual(response.status, 200)

    async def test_hash_lookup_cannot_reveal_or_reference_other_users_content(self):
        from app.assets import mode
        hash_value = 'blake3:' + 'b' * 64
        with Session(self.engine) as s:
            s.get(Asset, self.ids['bob']).content.hash = hash_value
            s.commit()
        for user, expected in [('a', 404), ('b', 200), ('admin', 200)]:
            response = await self.client.head('/api/assets/hash/' + hash_value, headers={'Test-User': user})
            self.assertEqual(response.status, expected)
            response = await self.client.get('/view', params={'filename': hash_value}, headers={'Test-User': user})
            self.assertEqual(response.status, expected)
        with patch.object(mode, 'hashing_enabled', return_value=True):
            response = await self.client.post('/api/assets/from-hash', headers={'Test-User': 'a'},
                                             json={'hash': hash_value, 'name': 'copy'})
            self.assertEqual(response.status, 404, await response.text())


if __name__ == '__main__':
    unittest.main()

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


spec = importlib.util.spec_from_file_location("frontend_reload", Path(__file__).parents[1] / "utils/frontend_reload.py")
frontend_reload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frontend_reload)


class FrontendReloadTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, revision):
        app = web.Application()
        routes = web.RouteTableDef()
        with patch.dict(os.environ, {frontend_reload.REVISION_ENV: revision}):
            frontend_reload.install_frontend_reload(app, routes)

        @web.middleware
        async def auth(request, handler):
            if not request.headers.get("X-Test-User"):
                return web.Response(status=401)
            request["user_id"] = request.headers["X-Test-User"]
            return await handler(request)

        app.middlewares.append(auth)

        @routes.get("/extensions")
        @routes.get("/api/extensions")
        async def extensions(request):
            return web.json_response(["/extensions/plugin/main.js", "/extensions/ComfyUI-Account-Manager/js/autoRefresh.js?x=&am_revision=old#part"])

        app.add_routes(routes)
        return app

    async def test_revision_is_shared_and_frozen_until_worker_restarts(self):
        old = self.make_app("100")
        new = self.make_app("200")
        with patch.dict(os.environ, {frontend_reload.REVISION_ENV: "300"}):
            async with TestClient(TestServer(old)) as first, TestClient(TestServer(new)) as second:
                for client, revision in ((first, "100"), (second, "200")):
                    response = await client.get(frontend_reload.VERSION_PATH, headers={"X-Test-User": "user-a"})
                    self.assertEqual(await response.json(), {"revision": revision, "user_id": "user-a"})
                    self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_extension_entries_are_versioned_and_auth_still_applies(self):
        async with TestClient(TestServer(self.make_app("200"))) as client:
            self.assertEqual((await client.get(frontend_reload.VERSION_PATH)).status, 401)
            for path in ("/extensions", "/api/extensions"):
                response = await client.get(path, headers={"X-Test-User": "user-a"})
                self.assertEqual(await response.json(), [
                    "/extensions/plugin/main.js", "/extensions/ComfyUI-Account-Manager/js/autoRefresh.js?x=&am_revision=200#part",
                ])
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_static_helpers_revalidate_including_not_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "helper.js").write_text("export const version = 1;", encoding="utf-8")
            app = self.make_app("200")
            app.router.add_static("/extensions/plugin/", directory)
            async with TestClient(TestServer(app)) as client:
                headers = {"X-Test-User": "user-a"}
                response = await client.get("/extensions/plugin/helper.js", headers=headers)
                self.assertEqual(response.headers["Cache-Control"], "no-cache, must-revalidate")
                await response.read()
                headers["If-None-Match"] = response.headers["ETag"]
                response = await client.get("/extensions/plugin/helper.js", headers=headers)
                self.assertEqual(response.status, 304)
                self.assertEqual(response.headers["Cache-Control"], "no-cache, must-revalidate")

    def test_direct_start_has_revision_and_invalid_override_is_rejected(self):
        app, routes = web.Application(), web.RouteTableDef()
        with patch.dict(os.environ, {frontend_reload.REVISION_ENV: ""}), patch.object(frontend_reload.time, "time_ns", return_value=123):
            frontend_reload.install_frontend_reload(app, routes)
        with self.assertRaisesRegex(ValueError, "numeric"):
            self.make_app("bad value")


if __name__ == "__main__":
    unittest.main()

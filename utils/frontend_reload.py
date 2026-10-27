import json
import os
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiohttp import web


REVISION_ENV = "ACCOUNT_MANAGER_FRONTEND_REVISION"
REVISION_QUERY = "am_revision"
VERSION_PATH = "/am/frontend-version"


def install_frontend_reload(app, routes):
    # Capture at startup: an old worker must not advertise a newer deployment.
    revision = os.environ.get(REVISION_ENV) or str(time.time_ns())
    if not revision.isascii() or not revision.isdigit():
        raise ValueError(f"{REVISION_ENV} must be a numeric deployment timestamp")

    @routes.get(VERSION_PATH)
    async def frontend_version(request):
        return web.json_response(
            {"revision": revision, "user_id": request.get("user_id")},
            headers={"Cache-Control": "no-store"},
        )

    @web.middleware
    async def frontend_reload_middleware(request, handler):
        response = await handler(request)
        path = request.path.removeprefix("/api")
        if request.method == "GET" and path == "/extensions" and response.status == 200:
            extensions = []
            for url in json.loads(response.text):
                parts = urlsplit(url)
                # Keep other plugins' import URLs canonical, including relative helpers.
                if parts.path.endswith("/ComfyUI-Account-Manager/js/autoRefresh.js"):
                    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != REVISION_QUERY]
                    query.append((REVISION_QUERY, revision))
                    url = urlunsplit(parts._replace(query=urlencode(query)))
                extensions.append(url)
            return web.json_response(extensions, headers={"Cache-Control": "no-store"})
        return response

    async def revalidate_scripts(request, response):
        path = request.path.removeprefix("/api")
        if path.startswith(("/extensions/", "/scripts/")) and path.endswith((".js", ".css")):
            # Also cover relative helper imports and conditional FileResponse replies.
            response.headers["Cache-Control"] = "no-cache, must-revalidate"

    app.middlewares.append(frontend_reload_middleware)
    app.on_response_prepare.append(revalidate_scripts)

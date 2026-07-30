import asyncio
import base64
import json
import re
from urllib.parse import urlencode

import aiohttp
from aiohttp import web


ASSET_ID_PATTERN = re.compile(
    r"^/api/assets/([0-9a-fA-F-]{36})(?:/content|/tags)?$"
)


class DistributedRoutes:
    """Route cross-worker control requests without changing ComfyUI routes."""

    def __init__(
        self,
        scheduler,
        signer,
        users_db,
        instance_port: int,
        worker_stale_seconds: int,
    ):
        self.scheduler = scheduler
        self.signer = signer
        self.users_db = users_db
        self.instance_port = int(instance_port)
        self.worker_stale_seconds = int(worker_stale_seconds)

    def _is_admin(self, request: web.Request) -> bool:
        user_id = request.get("user_id") or ""
        _, user = self.users_db.get_user(user_id=user_id)
        return bool(user and user.get("admin"))

    async def _is_internal(self, request: web.Request, body: bytes) -> bool:
        timestamp = request.headers.get(self.signer.HEADER_TIMESTAMP, "")
        signature = request.headers.get(self.signer.HEADER_SIGNATURE, "")
        if not timestamp or not signature:
            return False
        return self.signer.verify(
            request.method, request.path, body, timestamp, signature
        )

    @staticmethod
    def _forward_headers(request: web.Request) -> dict:
        headers = {}
        for name in (
            "Authorization",
            "Cookie",
            "Content-Type",
            "Accept",
            "Range",
            "Comfy-Usage-Source",
        ):
            value = request.headers.get(name)
            if value:
                headers[name] = value
        return headers

    async def _proxy(
        self,
        request: web.Request,
        port: int,
        body: bytes,
        path_qs: str = None,
    ) -> web.StreamResponse:
        target_path_qs = path_qs or request.rel_url.path_qs
        headers = self._forward_headers(request)
        signed_path = target_path_qs.split("?", 1)[0]
        headers.update(self.signer.headers(request.method, signed_path, body))
        url = f"http://127.0.0.1:{int(port)}{target_path_qs}"
        timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                request.method,
                url,
                data=body if request.method not in {"GET", "HEAD"} else None,
                headers=headers,
                allow_redirects=False,
            ) as response:
                excluded = {
                    "connection",
                    "keep-alive",
                    "proxy-authenticate",
                    "proxy-authorization",
                    "te",
                    "trailers",
                    "transfer-encoding",
                    "upgrade",
                    "content-length",
                }
                response_headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() not in excluded
                }
                outgoing = web.StreamResponse(
                    status=response.status,
                    reason=response.reason,
                    headers=response_headers,
                )
                await outgoing.prepare(request)
                async for chunk in response.content.iter_chunked(64 * 1024):
                    await outgoing.write(chunk)
                await outgoing.write_eof()
                return outgoing

    async def _fetch_json(
        self,
        request: web.Request,
        port: int,
        path_qs: str,
    ) -> tuple[int, dict]:
        body = b""
        headers = self._forward_headers(request)
        signed_path = path_qs.split("?", 1)[0]
        headers.update(self.signer.headers("GET", signed_path, body))
        url = f"http://127.0.0.1:{int(port)}{path_qs}"
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as response:
                    try:
                        payload = await response.json()
                    except (json.JSONDecodeError, aiohttp.ContentTypeError):
                        payload = {}
                    return response.status, payload
        except (aiohttp.ClientError, TimeoutError, OSError):
            return 503, {}

    async def _post_control(
        self,
        request: web.Request,
        port: int,
        path: str,
        body: bytes,
    ) -> int:
        headers = self._forward_headers(request)
        headers["Content-Type"] = "application/json"
        headers.update(self.signer.headers("POST", path, body))
        url = f"http://127.0.0.1:{int(port)}{path}"
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=body, headers=headers) as response:
                    await response.read()
                    return response.status
        except (aiohttp.ClientError, TimeoutError, OSError):
            return 503

    async def _aggregate_assets(self, request: web.Request) -> web.Response:
        query = request.rel_url.query
        try:
            limit = max(1, min(200, int(query.get("limit", 20))))
            offset = max(0, int(query.get("offset", 0)))
        except ValueError:
            return web.json_response({"error": "Invalid pagination"}, status=400)

        after = query.get("after")
        if after and after.startswith("am:"):
            try:
                offset = int(base64.urlsafe_b64decode(after[3:] + "==").decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                return web.json_response({"error": "Invalid cursor"}, status=400)

        fetch_count = min(1000, offset + limit + 1)
        worker_query = dict(query)
        worker_query.pop("after", None)
        worker_query["offset"] = "0"
        worker_query["limit"] = str(fetch_count)
        path_qs = f"/api/assets?{urlencode(worker_query, doseq=True)}"
        ports = self.scheduler.worker_ports(self.worker_stale_seconds)
        results = await asyncio.gather(
            *(self._fetch_json(request, port, path_qs) for port in ports)
        )

        assets = []
        total = 0
        successful = 0
        owner_id = request.get("user_id") or ""
        for port, (status, payload) in zip(ports, results):
            if status != 200 or not isinstance(payload, dict):
                continue
            successful += 1
            total += int(payload.get("total") or 0)
            for asset in payload.get("assets") or []:
                if not isinstance(asset, dict):
                    continue
                assets.append(asset)
                asset_id = str(asset.get("id") or asset.get("reference_id") or "")
                if asset_id:
                    self.scheduler.upsert_asset(
                        asset_id, owner_id, "", port, asset
                    )
        if ports and not successful:
            return web.json_response({"error": "No asset worker responded"}, status=503)

        sort_field = str(query.get("sort") or "created_at")
        descending = str(query.get("order") or "desc").lower() != "asc"

        def sort_key(asset):
            value = asset.get(sort_field)
            return (value is not None, str(value or ""), str(asset.get("id") or ""))

        assets.sort(key=sort_key, reverse=descending)
        page = assets[offset : offset + limit]
        has_more = offset + len(page) < total
        next_cursor = None
        if has_more:
            encoded = base64.urlsafe_b64encode(
                str(offset + len(page)).encode("ascii")
            ).decode("ascii").rstrip("=")
            next_cursor = f"am:{encoded}"
        payload = {
            "assets": page,
            "total": total,
            "has_more": has_more,
        }
        if next_cursor:
            payload["next_cursor"] = next_cursor
        return web.json_response(payload)

    async def _find_asset_worker(
        self, request: web.Request, asset_id: str
    ) -> int | None:
        indexed = self.scheduler.get_asset(asset_id)
        if indexed and indexed.get("worker_port"):
            if (
                self._is_admin(request)
                or not indexed.get("owner_id")
                or indexed.get("owner_id") == (request.get("user_id") or "")
            ):
                return int(indexed["worker_port"])
            return None

        path = f"/api/assets/{asset_id}"
        ports = self.scheduler.worker_ports(self.worker_stale_seconds)
        results = await asyncio.gather(
            *(self._fetch_json(request, port, path) for port in ports)
        )
        for port, (status, payload) in zip(ports, results):
            if status == 200:
                self.scheduler.upsert_asset(
                    asset_id,
                    request.get("user_id") or "",
                    "",
                    port,
                    payload if isinstance(payload, dict) else {},
                )
                return port
        return None

    async def _route_interrupt(
        self, request: web.Request, handler, body: bytes
    ) -> web.Response:
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        prompt_id = str(payload.get("prompt_id") or "")
        admin = self._is_admin(request)
        owner_id = request.get("user_id") or ""

        if prompt_id:
            job = self.scheduler.get_job(prompt_id)
            if not job or (not admin and job["owner_id"] != owner_id):
                return web.json_response({"error": "Prompt not found"}, status=404)
            worker_port = int(job.get("worker_port") or 0)
            if not worker_port or worker_port == self.instance_port:
                return await handler(request)
            return await self._proxy(request, worker_port, body)

        jobs = self.scheduler.running_jobs(owner_id, admin=admin)
        ports = sorted(
            {
                int(job["worker_port"])
                for job in jobs
                if job.get("worker_port")
            }
        )
        responses = []
        for port in ports:
            port_jobs = [job for job in jobs if int(job["worker_port"]) == port]
            for job in port_jobs:
                targeted_body = json.dumps(
                    {"prompt_id": job["prompt_id"]}, separators=(",", ":")
                ).encode("utf-8")
                if port == self.instance_port:
                    continue
                responses.append(
                    await self._post_control(
                        request, port, "/interrupt", targeted_body
                    )
                )
        if any(
            int(job.get("worker_port") or 0) == self.instance_port for job in jobs
        ):
            await handler(request)
        return web.json_response(
            {"interrupted": len(jobs), "worker_statuses": responses}
        )

    def middleware(self) -> web.middleware:
        @web.middleware
        async def distributed_middleware(request: web.Request, handler):
            if not self.scheduler:
                return await handler(request)

            has_internal_headers = bool(
                request.headers.get(self.signer.HEADER_TIMESTAMP)
                or request.headers.get(self.signer.HEADER_SIGNATURE)
            )
            body = b""
            if has_internal_headers:
                body = await request.read()
                if await self._is_internal(request, body):
                    return await handler(request)
                return web.json_response(
                    {"error": "Invalid internal signature"}, status=403
                )

            if request.path == "/interrupt" and request.method == "POST":
                body = await request.read()
                return await self._route_interrupt(request, handler, body)

            if request.path == "/api/assets" and request.method == "GET":
                return await self._aggregate_assets(request)

            match = ASSET_ID_PATTERN.match(request.path)
            if match:
                if request.method not in {"GET", "HEAD"}:
                    body = await request.read()
                worker_port = await self._find_asset_worker(request, match.group(1))
                if worker_port and worker_port != self.instance_port:
                    return await self._proxy(request, worker_port, body)
                if not worker_port:
                    return web.json_response({"error": "ASSET_NOT_FOUND"}, status=404)

            return await handler(request)

        return distributed_middleware

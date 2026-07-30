import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

import aiohttp
from aiohttp import web


class InternalSigner:
    HEADER_TIMESTAMP = "X-Account-Manager-Timestamp"
    HEADER_SIGNATURE = "X-Account-Manager-Signature"

    def __init__(self, secret_file: str, max_age_seconds: int = 30):
        self.secret_file = Path(secret_file)
        self.max_age_seconds = max(5, int(max_age_seconds))
        self.secret = self._load_or_create_secret()

    def _load_or_create_secret(self) -> bytes:
        if self.secret_file.exists():
            return self.secret_file.read_text(encoding="utf-8").strip().encode("utf-8")
        self.secret_file.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(64)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(self.secret_file, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(value)
        except FileExistsError:
            return self.secret_file.read_text(encoding="utf-8").strip().encode("utf-8")
        return value.encode("utf-8")

    @staticmethod
    def _canonical(method: str, path: str, timestamp: str, body: bytes) -> bytes:
        digest = hashlib.sha256(body).hexdigest()
        return f"{method.upper()}\n{path}\n{timestamp}\n{digest}".encode("utf-8")

    def headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        signature = hmac.new(
            self.secret,
            self._canonical(method, path, timestamp, body),
            hashlib.sha256,
        ).hexdigest()
        return {
            self.HEADER_TIMESTAMP: timestamp,
            self.HEADER_SIGNATURE: signature,
        }

    def verify(
        self,
        method: str,
        path: str,
        body: bytes,
        timestamp: str,
        signature: str,
    ) -> bool:
        try:
            numeric_timestamp = int(timestamp)
        except (TypeError, ValueError):
            return False
        if abs(int(time.time()) - numeric_timestamp) > self.max_age_seconds:
            return False
        expected = hmac.new(
            self.secret,
            self._canonical(method, path, timestamp, body),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature or "")

    async def verify_request(self, request: web.Request, body: bytes) -> bool:
        return self.verify(
            request.method,
            request.path,
            body,
            request.headers.get(self.HEADER_TIMESTAMP, ""),
            request.headers.get(self.HEADER_SIGNATURE, ""),
        )


class EventRelay:
    PATH = "/account-manager/internal/event"

    def __init__(self, server, scheduler, signer: InternalSigner, instance_port: int):
        self.server = server
        self.scheduler = scheduler
        self.signer = signer
        self.instance_port = int(instance_port)
        self._original_send_json = server.send_json
        self._original_send_bytes = server.send_bytes

    def install(self) -> None:
        self.server.send_json = self.send_json
        self.server.send_bytes = self.send_bytes

    async def _post(self, ingress_port: int, payload: dict) -> bool:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        headers = self.signer.headers("POST", self.PATH, body)
        headers["Content-Type"] = "application/json"
        url = f"http://127.0.0.1:{int(ingress_port)}{self.PATH}"
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=body, headers=headers) as response:
                    return 200 <= response.status < 300
        except (aiohttp.ClientError, TimeoutError, OSError):
            return False

    async def send_json(self, event, data, sid=None):
        if sid is None or sid in self.server.sockets:
            return await self._original_send_json(event, data, sid)
        ingress_port = self.scheduler.client_ingress(str(sid))
        if not ingress_port or ingress_port == self.instance_port:
            return await self._original_send_json(event, data, sid)
        relayed = await self._post(
            ingress_port,
            {
                "kind": "json",
                "event": event,
                "data": data,
                "sid": str(sid),
                "source_port": self.instance_port,
            },
        )
        if not relayed:
            return await self._original_send_json(event, data, sid)
        return None

    async def send_bytes(self, event, data, sid=None):
        if sid is None or sid in self.server.sockets:
            return await self._original_send_bytes(event, data, sid)
        ingress_port = self.scheduler.client_ingress(str(sid))
        if not ingress_port or ingress_port == self.instance_port:
            return await self._original_send_bytes(event, data, sid)
        encoded = self.server.encode_bytes(event, data)
        relayed = await self._post(
            ingress_port,
            {
                "kind": "bytes",
                "data": base64.b64encode(encoded).decode("ascii"),
                "sid": str(sid),
                "source_port": self.instance_port,
            },
        )
        if not relayed:
            return await self._original_send_bytes(event, data, sid)
        return None

    async def handle_event(self, request: web.Request) -> web.Response:
        body = await request.read()
        if not await self.signer.verify_request(request, body):
            return web.json_response({"error": "Invalid internal signature"}, status=403)
        try:
            payload = json.loads(body)
            sid = str(payload["sid"])
            if payload["kind"] == "json":
                await self._original_send_json(payload["event"], payload.get("data"), sid)
            elif payload["kind"] == "bytes":
                message = base64.b64decode(payload["data"], validate=True)
                socket = self.server.sockets.get(sid)
                if socket is not None:
                    await socket.send_bytes(message)
            else:
                raise ValueError("Unsupported event kind")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return web.json_response({"error": "Invalid internal event"}, status=400)
        return web.json_response({"ok": True})

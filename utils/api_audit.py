import contextvars
import json
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "api-key",
}
SENSITIVE_QUERY_KEYS = {
    "access_key",
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "key",
    "secret",
    "signature",
    "token",
}

_current_prompt_id = contextvars.ContextVar("account_manager_prompt_id", default=None)
_fallback_prompt_id = None
_fallback_lock = threading.Lock()
_recorder = None
_installed = False


def set_current_job(prompt_id: str) -> None:
    global _fallback_prompt_id
    prompt_id = str(prompt_id or "") or None
    _current_prompt_id.set(prompt_id)
    with _fallback_lock:
        _fallback_prompt_id = prompt_id


def clear_current_job(prompt_id: str = None) -> None:
    global _fallback_prompt_id
    current = _current_prompt_id.get()
    if prompt_id is None or current == prompt_id:
        _current_prompt_id.set(None)
    with _fallback_lock:
        if prompt_id is None or _fallback_prompt_id == prompt_id:
            _fallback_prompt_id = None


def current_job() -> str | None:
    prompt_id = _current_prompt_id.get()
    if prompt_id:
        return prompt_id
    with _fallback_lock:
        return _fallback_prompt_id


def _redact_url(url) -> str:
    text = str(url or "")
    try:
        parts = urlsplit(text)
        query = urlencode(
            [
                (key, "[REDACTED]" if key.lower() in SENSITIVE_QUERY_KEYS else value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
            ],
            doseq=True,
        )
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    except ValueError:
        return text


def _headers(value) -> str:
    if not value:
        return "{}"
    items = value.items() if hasattr(value, "items") else value
    return json.dumps(
        {
            str(key): "[REDACTED]" if str(key).lower() in SENSITIVE_HEADERS else str(item)
            for key, item in items
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _body(value) -> bytes | None:
    if value is None or value is False:
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode(
        "utf-8"
    )


def _record(client, request, response=None, error: BaseException = None) -> None:
    prompt_id = current_job()
    if not prompt_id or _recorder is None:
        return
    try:
        response_body = None
        if response is not None:
            try:
                response_body = _body(response.content)
            except Exception:
                response_body = _body(getattr(response, "_content", None))
        _recorder(
            prompt_id=prompt_id,
            recorded_at=time.time(),
            client=client,
            method=str(getattr(request, "method", "") or ""),
            url=_redact_url(getattr(request, "url", "")),
            request_headers=_headers(getattr(request, "headers", None)),
            request_body=_body(getattr(request, "body", None) or getattr(request, "content", None)),
            response_status=(getattr(response, "status_code", None) if response is not None else None),
            response_headers=_headers(getattr(response, "headers", None)),
            response_body=response_body,
            error=str(error) if error is not None else None,
        )
    except Exception:
        return


def _fallback_request(method, url, kwargs):
    class Request:
        pass

    request = Request()
    request.method = method
    request.url = url
    request.headers = kwargs.get("headers")
    request.body = kwargs.get("json", kwargs.get("data"))
    return request


def install_api_audit(recorder) -> None:
    global _installed, _recorder
    _recorder = recorder
    if _installed:
        return

    try:
        import requests

        original = requests.sessions.Session.request

        def requests_request(session, method, url, **kwargs):
            try:
                response = original(session, method, url, **kwargs)
            except Exception as error:
                _record("requests", _fallback_request(method, url, kwargs), error=error)
                raise
            _record("requests", response.request, response)
            return response

        requests.sessions.Session.request = requests_request
    except ImportError:
        pass

    try:
        import httpx

        original_sync = httpx.Client.request
        original_async = httpx.AsyncClient.request

        def httpx_request(client, method, url, *args, **kwargs):
            try:
                response = original_sync(client, method, url, *args, **kwargs)
            except Exception as error:
                _record("httpx", _fallback_request(method, url, kwargs), error=error)
                raise
            _record("httpx", response.request, response)
            return response

        async def httpx_async_request(client, method, url, *args, **kwargs):
            try:
                response = await original_async(client, method, url, *args, **kwargs)
            except Exception as error:
                _record("httpx-async", _fallback_request(method, url, kwargs), error=error)
                raise
            _record("httpx-async", response.request, response)
            return response

        httpx.Client.request = httpx_request
        httpx.AsyncClient.request = httpx_async_request
    except ImportError:
        pass

    _installed = True

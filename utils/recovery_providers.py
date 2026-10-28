"""Explicit query-only adapters for the async video protocols used by our nodes."""

import re
from urllib.parse import urlsplit, urlunsplit


KLING_VIDEO_KINDS = {
    "text2video", "image2video", "multi-image2video", "omni-video",
    "video-extend", "motion-control", "multi-elements", "lip-sync",
}
VAPEUR_VIDEO_KINDS = {
    "text-to-video", "image-to-video", "multi-image-to-video",
    "video-motion-control", "video-extend", "video-edit",
}


def submission(url: str, body: dict):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password or parts.query or parts.fragment:
        return None
    if not isinstance(body, dict) or body.get("error") or body.get("code") not in (None, 0, "0"):
        return None
    path = parts.path.rstrip("/")
    method, query_body = "GET", None
    if path.endswith("/contents/generations/tasks"):
        provider, task_id = "ark", body.get("id")
        query_path = path + "/{}"
    elif path.endswith("/v1/videos/generations"):
        provider = "wan"
        envelope = body.get("data") or body
        output = envelope.get("output") or envelope
        task_id = envelope.get("task_id") or envelope.get("id") or output.get("task_id") or output.get("id")
        query_path = path.removesuffix("/videos/generations") + "/tasks/{}"
    elif path.endswith("/qwen/api/v1/services/aigc/video-generation/generation"):
        provider, task_id = "wan", (body.get("output") or {}).get("task_id")
        query_path = path.removesuffix("/services/aigc/video-generation/generation") + "/tasks/video/{}"
    elif path.startswith("/v1/videos/") and path.removeprefix("/v1/videos/") in KLING_VIDEO_KINDS:
        provider, task_id = "kling", (body.get("data") or {}).get("task_id")
        query_path = path + "/{}"
    elif path.startswith("/v1/kling/") and path.endswith("/submit") and path.split("/")[-2] in VAPEUR_VIDEO_KINDS:
        provider, task_id = "vapeur_kling", (body.get("Response") or body).get("JobId")
        if (body.get("Response") or body).get("Error"):
            return None
        method, query_path = "POST", path.removesuffix("/submit") + "/describe"
        query_body = {"JobId": task_id}
    else:
        return None
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", task_id):
        return None
    query_url = urlunsplit((parts.scheme, parts.netloc, query_path.format(task_id), "", ""))
    return provider, task_id, method, query_url, query_body


def result(provider: str, body: dict) -> tuple[str, list[str]]:
    if not isinstance(body, dict):
        raise ValueError("Invalid provider response")
    if body.get("error") or body.get("code") not in (None, 0, "0"):
        raise ValueError("Provider query rejected")
    if provider == "ark":
        status = body.get("status")
        urls = [(body.get("content") or {}).get("video_url")]
    elif provider == "wan":
        envelope = body.get("data") or body
        output = envelope.get("output") or envelope
        status = envelope.get("task_status") or envelope.get("status") or output.get("task_status") or output.get("status")
        urls = [output.get("video_url") or output.get("url")]
    elif provider == "kling":
        data = body.get("data") or {}
        status = data.get("task_status")
        urls = [item.get("url") for item in (data.get("task_result") or {}).get("videos", [])]
    elif provider == "vapeur_kling":
        data = body.get("Response") or body
        if data.get("Error"):
            raise ValueError("Provider query rejected")
        status = "failed" if data.get("ErrorCode") else data.get("Status") or data.get("TaskStatus")
        urls = [data.get("ResultVideoUrl")]
    else:
        raise ValueError("Unsupported recovery provider")
    status = str(status or "").lower()
    if status in {"fail", "failed", "error", "aborted", "cancelled", "canceled", "expired"}:
        return "failed", []
    if status in {"succeeded", "succeed", "success", "completed", "done", "finish"}:
        urls = list(dict.fromkeys(url for url in urls if isinstance(url, str) and url.startswith("https://")))
        if not urls:
            raise ValueError("Completed task returned no downloadable media")
        return "succeeded", urls
    if status in {"submitted", "queued", "pending", "running", "processing", "in_progress", "created", "waiting"}:
        return "pending", []
    raise ValueError("Unknown provider task status")

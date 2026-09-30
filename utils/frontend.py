import asyncio
import logging
import urllib.parse

from aiohttp import web
from comfy_execution.jobs import JobStatus, get_all_jobs


def install_origin_middleware(app, is_loopback):
    @web.middleware
    async def origin_only_middleware(request, handler):
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return web.Response(status=403)

        if "Host" in request.headers and "Origin" in request.headers:
            host = urllib.parse.urlsplit("//" + request.headers["Host"].lower())
            origin = urllib.parse.urlsplit(request.headers["Origin"].lower())
            host_domain = host.netloc if origin.port is not None else host.hostname
            origin_domain = origin.netloc if host.port is not None else origin.hostname
            if host_domain and origin_domain and host_domain != origin_domain:
                if await asyncio.to_thread(is_loopback, host.hostname):
                    logging.warning("Request host and origin do not match: %s != %s", host_domain, origin_domain)
                    return web.Response(status=403)

        if request.method == "OPTIONS":
            return web.Response()
        return await handler(request)

    for index, middleware in enumerate(app.middlewares):
        if middleware.__name__ == "origin_only_middleware":
            app.middlewares[index] = origin_only_middleware


def create_jobs_middleware(prompt_queue, get_history):
    def load_page(options):
        running, queued = prompt_queue.get_current_queue_volatile()
        history = get_history()
        jobs, total = get_all_jobs(
            [item[:5] for item in running],
            [item[:5] for item in queued],
            history,
            **options,
        )
        return web.json_response({
            "jobs": jobs,
            "pagination": {
                "offset": options["offset"],
                "limit": options["limit"],
                "total": total,
                "has_more": options["offset"] + len(jobs) < total,
            },
        })

    @web.middleware
    async def jobs_middleware(request, handler):
        if request.method != "GET" or request.path != "/api/jobs":
            return await handler(request)

        query = request.rel_url.query
        status_filter = None
        if query.get("status"):
            status_filter = [value.strip().lower() for value in query["status"].split(",") if value.strip()]
            invalid = [value for value in status_filter if value not in JobStatus.ALL]
            if invalid:
                return web.json_response({
                    "error": f"Invalid status value(s): {', '.join(invalid)}. Valid values: {', '.join(JobStatus.ALL)}",
                }, status=400)

        sort_by = query.get("sort_by", "created_at").lower()
        sort_order = query.get("sort_order", "desc").lower()
        if sort_by not in {"created_at", "execution_duration"}:
            return web.json_response({"error": "sort_by must be 'created_at' or 'execution_duration'"}, status=400)
        if sort_order not in {"asc", "desc"}:
            return web.json_response({"error": "sort_order must be 'asc' or 'desc'"}, status=400)

        limit = None
        if "limit" in query:
            try:
                limit = int(query["limit"])
            except ValueError:
                return web.json_response({"error": "limit must be an integer"}, status=400)
            if limit <= 0:
                return web.json_response({"error": "limit must be a positive integer"}, status=400)
        try:
            offset = max(0, int(query.get("offset", 0)))
        except ValueError:
            return web.json_response({"error": "offset must be an integer"}, status=400)

        return await asyncio.to_thread(load_page, {
            "status_filter": status_filter,
            "workflow_id": query.get("workflow_id"),
            "sort_by": sort_by,
            "sort_order": sort_order,
            "limit": limit,
            "offset": offset,
        })

    return jobs_middleware

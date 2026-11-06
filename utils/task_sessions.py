from aiohttp import web


class TaskSessions:
    """Delegate the executing queue owner's session to a node subprocess."""

    def __init__(self, server, users_db, jwt_auth, scheduler):
        self.server = server
        self.users_db = users_db
        self.jwt_auth = jwt_auth
        self.scheduler = scheduler

    def _local_owner(self, prompt_id):
        queue = self.server.prompt_queue
        with queue.mutex:
            for item in queue.currently_running.values():
                if len(item) >= 7 and item[1] == prompt_id:
                    return item[6]
        return None

    def environment(self, prompt_id):
        owner = self._local_owner(prompt_id)
        if not owner:
            raise ValueError("无法取得当前执行任务的已认证账号，不能调用视频服务。")
        user_id, user = self.users_db.get_user(user_id=owner)
        if user_id != owner or not user:
            raise ValueError("当前任务所属账号已不存在。")
        token = self.jwt_auth.create_access_token(
            {"id": owner, "username": user["username"], "am_task": prompt_id},
            expire_minutes=120,
        )
        return {
            "COMFY_TASK_TOKEN": token,
            "COMFY_TASK_BASE_URL": f"http://127.0.0.1:{self.server.port}",
        }

    async def authorize(self, request, claims):
        prompt_id = claims["am_task"]
        if self.scheduler:
            job = self.scheduler.get_job(prompt_id)
            owner = job["owner_id"] if job and job["status"] == "running" else None
        else:
            owner = self._local_owner(prompt_id)
        if not owner or owner != claims["id"]:
            raise web.HTTPUnauthorized(reason="Parent task is no longer running")

        path = request.path.removeprefix("/api")
        allowed = (
            request.method == "GET" and (
                path in {"/object_info", "/view", "/queue"}
                or path.startswith("/history/")
            )
            or request.method == "POST" and path in {"/upload/image", "/prompt"}
        )
        if not allowed:
            raise web.HTTPForbidden(reason="Task session cannot access this endpoint")
        if request.method != "POST" or path != "/prompt":
            return
        if not self.scheduler or len(self.scheduler.worker_ports()) < 2:
            raise web.HTTPConflict(reason="Video child task requires another scheduler worker")
        payload = await request.json()
        resource = self.scheduler.classify_item((0, "", payload.get("prompt", {})))
        _, user = self.users_db.get_user(user_id=owner)
        limit = (
            self.scheduler.admin_concurrency_limit if user.get("admin") else
            self.scheduler.resource_concurrency_limits.get(
                resource, self.scheduler.max_concurrent_jobs_per_user
            )
        )
        active = self.scheduler.running_jobs(owner)
        if limit and sum(job["resource_class"] == resource for job in active) >= limit:
            raise web.HTTPConflict(reason="Account concurrency is full; parent Agent is waiting for a video child task")
        if not set(self.scheduler.idle_worker_ports(resource)) - {self.server.port}:
            raise web.HTTPConflict(reason="No free scheduler worker for the video child task")

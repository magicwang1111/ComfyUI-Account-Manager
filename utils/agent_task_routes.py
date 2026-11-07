import hashlib
import json
import time
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, web


class AgentTaskRoutes:
    INTERNAL = '/account-manager/agent-internal'
    PUBLIC = '/llm/agent-tasks'

    def __init__(self, store, sessions, signer, scheduler, server, history=None):
        self.store = store
        self.sessions = sessions
        self.signer = signer
        self.scheduler = scheduler
        self.server = server
        self.history = history

    def create_from_node(self, prompt_id, node_id, job):
        owner = self.sessions._local_owner(prompt_id)
        if not owner or not self.scheduler:
            raise ValueError('独立 Agent 需要启用 Account Manager 分布式队列并取得当前任务账号。')
        _, user = self.sessions.users_db.get_user(user_id=owner)
        if not user:
            raise ValueError('当前任务账号不存在。')
        job['identity'] = {'external_user_id': owner, 'external_username': user['username']}
        task = self.store.create(owner, f'{prompt_id}:{node_id}', job)
        return task['id']

    def token(self, task):
        _, user = self.sessions.users_db.get_user(user_id=task['owner'])
        if not user:
            raise ValueError('Agent task owner no longer exists')
        token = self.sessions.jwt_auth.create_access_token(
            {'id': task['owner'], 'username': user['username'], 'am_agent': task['id'], 'am_lease': task['lease']},
            expire_minutes=24 * 60,
        )
        return {'COMFY_TASK_TOKEN': token, 'COMFY_TASK_BASE_URL': f'http://127.0.0.1:{self.server.port}'}

    def public_task(self, task):
        children = []
        for child in self.store.children(task['id']):
            job = self.scheduler.get_job(child['prompt_id']) if self.scheduler else None
            children.append({'prompt_id': child['prompt_id'], 'state': job['status'] if job else 'submission_unknown'})
        state = task['state']
        if state in {'running', 'delivering', 'cancel_requested'} and task['lease_until'] < time.time():
            state = 'recovery_required'
        result = task['result']
        if result:
            result = dict(result)
            result['artifacts'] = [
                {k: v for k, v in item.items() if k not in {'path'}}
                | {'url': f"{self.PUBLIC}/{task['id']}/files/{index}"}
                for index, item in enumerate(result.get('artifacts', []))
            ]
        return {'id': task['id'], 'state': state, 'created': task['created'], 'updated': task['updated'],
                'recoverable': state == 'recovery_required' and bool(task['checkpoint'] or children),
                'error': task['error'], 'children': children, 'result': result}

    def owned(self, request):
        task = self.store.get(request.match_info['task_id'])
        if not task or task['owner'] != request.get('user_id'):
            raise web.HTTPNotFound()
        return task

    async def listing(self, request):
        return web.json_response([self.public_task(task) for task in self.store.list(request['user_id'])])

    async def detail(self, request):
        task = self.owned(request)
        try:
            after = max(0, int(request.query.get('after', '0')))
        except ValueError:
            raise web.HTTPBadRequest(reason='Invalid event sequence')
        return web.json_response(self.public_task(task) | {'events': self.store.events(task['id'], after)})

    async def cancel(self, request):
        task = self.owned(request)
        self.store.cancel(task['id'], task['owner'])
        # Queued children can be removed immediately. Running paid jobs remain tracked.
        for child in self.store.children(task['id']):
            self.scheduler.cancel(child['prompt_id'], owner_id=task['owner'])
            current = self.scheduler.get_job(child['prompt_id'])
            if current and current['status'] == 'running' and current.get('worker_port'):
                try:
                    token = self.sessions.jwt_auth.get_token_from_request(request)
                    async with ClientSession(timeout=ClientTimeout(total=10)) as client:
                        async with client.post(f"http://127.0.0.1:{current['worker_port']}/interrupt",
                                               json={'prompt_id': child['prompt_id']},
                                               headers={'Authorization': 'Bearer ' + token}) as response:
                            await response.read()
                except (ClientError, TimeoutError):
                    # Keep the actual child status visible if cancellation could not be delivered.
                    pass
        return web.json_response(self.public_task(self.store.get(task['id'])))

    async def recover(self, request):
        task = self.owned(request)
        try:
            self.store.recover(task['id'], task['owner'])
        except ValueError as exc:
            raise web.HTTPConflict(reason=str(exc))
        return web.json_response(self.public_task(self.store.get(task['id'])))

    async def file(self, request):
        task = self.owned(request)
        try:
            index = int(request.match_info['index'])
            if index < 0:
                raise ValueError()
            record = task['result']['artifacts'][index]
            path = Path(record['path']).resolve()
        except (ValueError, IndexError, TypeError, KeyError):
            raise web.HTTPNotFound()
        if not path.is_relative_to(Path(task['job']['job_dir']).resolve()) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def internal(self, request):
        body = await request.read()
        if not self.signer or not await self.signer.verify_request(request, body):
            raise web.HTTPForbidden(reason='Invalid internal signature')
        try:
            data = json.loads(body)
            if data['action'] == 'claim':
                task = self.store.claim()
                if task:
                    task['environment'] = self.token(task)
                    task['job']['children'] = self.store.children(task['id'])
                return web.json_response(task)
            if data['action'] == 'update':
                state = self.store.update(data['id'], data['lease'], events=data.get('events', []),
                                          checkpoint=data.get('checkpoint'), state=data.get('state'),
                                          result=data.get('result'), error=data.get('error', ''))
                return web.json_response({'state': state})
        except (ValueError, KeyError) as exc:
            raise web.HTTPConflict(reason=str(exc))
        raise web.HTTPBadRequest(reason='Unknown Agent action')

    async def delegated(self, request, claims, handler):
        task = self.store.get(claims['am_agent'])
        if (not task or task['owner'] != claims['id'] or task['lease'] != claims.get('am_lease')
                or task['lease_until'] < time.time() or task['state'] not in {'running', 'delivering'}):
            raise web.HTTPUnauthorized(reason='Agent task is no longer active')
        path = request.path.removeprefix('/api')
        if request.method == 'GET' and path.startswith('/history/'):
            if path.split('/')[-1] not in {c['prompt_id'] for c in self.store.children(task['id'])}:
                raise web.HTTPForbidden(reason='History does not belong to this Agent')
            return await handler(request)
        if request.method == 'GET' and path == '/view':
            wanted = {k: request.query.get(k, 'output' if k == 'type' else '')
                      for k in ('filename', 'subfolder', 'type')}
            allowed = False
            for child in self.store.children(task['id']):
                entries = self.history.query(owner_id=task['owner'], prompt_id=child['prompt_id']) if self.history else {}
                for entry in entries.values():
                    for output in entry.get('outputs', {}).values():
                        for value in output.values():
                            if isinstance(value, list):
                                allowed = allowed or any(isinstance(item, dict) and all(
                                    item.get(k, 'output' if k == 'type' else '') == v for k, v in wanted.items()
                                ) for item in value)
            if not allowed:
                raise web.HTTPForbidden(reason='Asset does not belong to an Agent child')
            return await handler(request)
        if request.method == 'GET' and path == '/object_info':
            return await handler(request)
        if request.method == 'POST' and path == '/upload/image':
            return await handler(request)
        if request.method != 'POST' or path != '/prompt':
            raise web.HTTPForbidden(reason='Agent task cannot access this endpoint')
        payload = await request.json()
        workflow = payload.get('prompt', {})
        if any('Agent' in str(node.get('class_type', '')) and 'ComfyUI-LLM' in str(node.get('class_type', ''))
               for node in workflow.values()):
            raise web.HTTPBadRequest(reason='Nested Agent workflows are not supported')
        key = str(payload.get('client_id') or '')
        if not key:
            raise web.HTTPBadRequest(reason='Agent child submission requires a client_id')
        fingerprint = hashlib.sha256(json.dumps(workflow, sort_keys=True).encode()).hexdigest()
        try:
            child, fresh = self.store.reserve_child(task['id'], key, fingerprint)
        except ValueError as exc:
            raise web.HTTPConflict(reason=str(exc))
        if not fresh:
            if child['response']:
                saved = json.loads(child['response'])
                return web.json_response(saved['body'], status=saved['status'])
            existing = self.scheduler.get_job(child['prompt_id'])
            if existing:
                return web.json_response({'prompt_id': child['prompt_id']})
            raise web.HTTPConflict(reason='Submission outcome unknown; do not regenerate automatically')
        payload['prompt_id'] = child['prompt_id']
        # aiohttp Request.json reads the cached body; keep downstream prompt validation intact.
        request._read_bytes = json.dumps(payload).encode()
        response = await handler(request)
        self.store.child_response(child['prompt_id'], {'status': response.status, 'body': json.loads(response.body)})
        if self.store.get(task['id'])['state'] != 'running':
            self.scheduler.cancel(child['prompt_id'], owner_id=task['owner'])
        return response

    def install(self, routes):
        routes.get(self.PUBLIC)(self.listing)
        routes.get(self.PUBLIC + '/{task_id}')(self.detail)
        routes.post(self.PUBLIC + '/{task_id}/cancel')(self.cancel)
        routes.post(self.PUBLIC + '/{task_id}/recover')(self.recover)
        routes.get(self.PUBLIC + '/{task_id}/files/{index}')(self.file)
        routes.post(self.INTERNAL)(self.internal)

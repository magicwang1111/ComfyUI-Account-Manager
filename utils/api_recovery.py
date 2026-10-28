"""Recover submitted API results, never re-execute a workflow or generation POST."""

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

import requests
from cryptography.fernet import Fernet
from filelock import FileLock, Timeout

from .api_audit import suspend_api_audit
from .cloud_archive import download_remote, _validate_public_http_url
from .recovery_providers import submission, result


logger = logging.getLogger("ComfyUI-Account-Manager")


class RecoveryQueryError(Exception):
    pass


class RecoverySuperseded(Exception):
    pass


class ApiRecoveryManager:
    def __init__(self, scheduler, history, secret: bytes, output_dir: str,
                 register_assets=None, notify=None):
        self.scheduler = scheduler
        self.history = history
        self.output_dir = Path(output_dir).resolve()
        self.register_assets = register_assets
        self.notify = notify
        key = hashlib.sha256(b"AccountManager.APIRecovery.v1\0" + secret).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(key))
        self.lock = FileLock(scheduler.database + ".recovery.lock", timeout=0)
        self._stop = threading.Event()
        self._thread = None
        with closing(scheduler._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS scheduler_api_recovery (
                    id TEXT PRIMARY KEY, prompt_id TEXT NOT NULL, node_id TEXT NOT NULL,
                    provider TEXT NOT NULL, task_id TEXT NOT NULL,
                    query_method TEXT NOT NULL, query_url TEXT NOT NULL, query_body TEXT,
                    credentials BLOB, state TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL, next_check REAL NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0, message TEXT
                );
                CREATE INDEX IF NOT EXISTS scheduler_api_recovery_due
                    ON scheduler_api_recovery(state, next_check);
            """)

    def observe(self, prompt_id, node_id, request, response):
        if str(request.method).upper() != "POST" or not 200 <= response.status_code < 300:
            return
        try:
            body = json.loads(response.content)
        except (ValueError, UnicodeDecodeError):
            return
        parsed = submission(str(request.url), body)
        if parsed is None:
            return
        provider, task_id, method, url, query_body = parsed
        headers = {str(k).lower(): str(v) for k, v in request.headers.items()}
        authorization = headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            return  # Signed requests require a provider-specific signer, not replay.
        key = hashlib.sha256(f"{prompt_id}\0{url}\0{task_id}".encode()).hexdigest()
        credentials = self.cipher.encrypt(authorization.encode())
        with closing(self.scheduler._connect()) as db:
            db.execute("PRAGMA synchronous=FULL")
            # Owner identity comes exclusively from the queue, never the response.
            db.execute("""
                INSERT OR IGNORE INTO scheduler_api_recovery
                (id,prompt_id,node_id,provider,task_id,query_method,query_url,query_body,credentials,created_at)
                SELECT ?,prompt_id,?,?,?,?,?,?,?,? FROM scheduler_jobs
                WHERE prompt_id=? AND status='running'
            """, (key, str(node_id or ""), provider, task_id, method, url,
                  json.dumps(query_body), credentials, time.time(), prompt_id))

    def start(self):
        self._thread = threading.Thread(target=self._run, name="api-result-recovery", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.wait(5):
            try:
                self.run_once()
            except Exception:
                # Do not log exception text: HTTP errors can contain signed URLs.
                logger.error("API result recovery iteration failed; retrying")

    def _query(self, row):
        _validate_public_http_url(row["query_url"])
        authorization = self.cipher.decrypt(row["credentials"]).decode()
        with requests.request(
            row["query_method"], row["query_url"],
            headers={"Authorization": authorization},
            json=json.loads(row["query_body"]), timeout=(10, 30), allow_redirects=False,
        ) as response:
            if response.status_code in (401, 403):
                raise RecoveryQueryError("恢复查询凭证已失效或无权限，请人工查询原任务 ID。")
            if response.status_code in (404, 410):
                raise RecoveryQueryError("第三方任务不存在或已过期，请检查原任务 ID。")
            response.raise_for_status()
            if not 200 <= response.status_code < 300:
                raise ValueError("Unexpected query redirect")
            return response.json()

    def _download(self, row, urls):
        owner = row["owner_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", owner):
            raise ValueError("Invalid owner path")
        relative = Path(owner) / "recovered" / row["id"]
        directory = self.output_dir / relative
        directory.mkdir(parents=True, exist_ok=True)
        references = []
        assets = []
        for index, url in enumerate(urls):
            suffix = Path(urlsplit(url).path).suffix.lower()
            if suffix not in (".mp4", ".webm", ".mov"):
                suffix = ".mp4"
            destination = directory / f"{index:03d}{suffix}"
            if not destination.is_file():
                temporary = destination.with_suffix(suffix + ".part")
                try:
                    content_type, size = download_remote(url, str(temporary), 20 * 1024**3)
                    if not size or (content_type and not content_type.startswith("video/") and content_type != "application/octet-stream"):
                        raise ValueError("Recovered result is not a video")
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            reference = {"filename": destination.name, "subfolder": relative.as_posix(),
                         "type": "output", "format": "video/" + ("webm" if suffix == ".webm" else "mp4")}
            references.append(reference)
            assets.append((reference, str(destination)))
        self._require_lost(row)
        if self.register_assets:
            self.register_assets(assets, row["prompt_id"], owner)
        return references

    def _publish(self, row, state, message, references=()):
        with closing(self.scheduler._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                job = db.execute("SELECT status FROM scheduler_jobs WHERE prompt_id=?", (row["prompt_id"],)).fetchone()
                published = bool(job and job["status"] == "worker_lost") and self.history.merge_api_recovery(row, state, message, list(references))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        if not published:
            return False
        if self.notify:
            self.notify()
        return True

    def _require_lost(self, row):
        with closing(self.scheduler._connect()) as db:
            job = db.execute("SELECT status FROM scheduler_jobs WHERE prompt_id=?", (row["prompt_id"],)).fetchone()
        if not job or job["status"] != "worker_lost":
            raise RecoverySuperseded()

    def run_once(self):
        try:
            with self.lock, suspend_api_audit():
                return self._recover_one()
        except Timeout:
            return False

    def _recover_one(self):
        now = time.time()
        with closing(self.scheduler._connect()) as db:
            db.execute("""
                UPDATE scheduler_api_recovery SET state='ignored', credentials=NULL
                WHERE state='pending' AND prompt_id IN
                  (SELECT prompt_id FROM scheduler_jobs WHERE status IN ('completed','cancelled','failed'))
            """)
            row = db.execute("""
                SELECT recovery.*, job.owner_id, job.payload, job.submitted_at
                FROM scheduler_api_recovery recovery JOIN scheduler_jobs job USING(prompt_id)
                WHERE recovery.state='pending' AND recovery.next_check<=? AND job.status='worker_lost'
                ORDER BY recovery.next_check, recovery.created_at LIMIT 1
            """, (now,)).fetchone()
        if row is None:
            return False
        row = dict(row)
        state, message, references = "pending", "服务器已恢复，正在查询原 API 任务。", []
        failures = row["failures"]
        try:
            if now - row["created_at"] > 7 * 86400:
                raise RecoveryQueryError("自动恢复已超过 7 天，请人工查询原任务 ID。")
            outcome, urls = result(row["provider"], self._query(row))
            self._require_lost(row)
            if outcome == "succeeded":
                references = self._download(row, urls)
                state, message = "recovered", "API 生成结果已恢复；原工作流已中断，后续节点未执行。"
            elif outcome == "failed":
                state, message = "failed", "第三方任务失败、取消或过期；未重新提交生成请求。"
            else:
                failures = 0
                message = "第三方仍在生成，正在等待原任务完成。"
        except RecoverySuperseded:
            state, message = "ignored", "原任务状态已更新，停止恢复。"
        except RecoveryQueryError as error:
            state, message = "failed", str(error)
        except Exception:
            failures += 1
            message = "查询或下载暂时失败，将自动重试；未重新提交生成请求。"
            if failures >= 20:
                state, message = "failed", "连续 20 次恢复失败，请人工查询原任务 ID；未重新提交生成请求。"
        # Persist history before acknowledging; a crash repeats an idempotent merge.
        if state != "ignored" and not self._publish(row, state, message, references):
            state = "ignored"
        with closing(self.scheduler._connect()) as db:
            db.execute("""
                UPDATE scheduler_api_recovery SET state=?, message=?, next_check=?, failures=?,
                    credentials=CASE WHEN ?='pending' THEN credentials ELSE NULL END WHERE id=?
            """, (state, message, now + min(1800, 30 * 2 ** min(failures, 6)), failures, state, row["id"]))
        return True

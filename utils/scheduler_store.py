import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from typing import Callable, Optional


QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
WORKER_LOST = "worker_lost"
TERMINAL_STATES = (COMPLETED, FAILED, CANCELLED, WORKER_LOST)


def is_sqlite_busy(error: BaseException) -> bool:
    """Return whether an SQLite error is a transient writer-contention error."""
    return isinstance(error, sqlite3.OperationalError) and any(
        message in str(error).lower() for message in ("database is locked", "database is busy")
    )


class SchedulerStore:
    """A small multi-process queue shared by all ComfyUI instances."""

    def __init__(
        self,
        database: str,
        max_concurrent_jobs_per_user: int = 6,
        admin_concurrency_limit: int = 0,
        resource_concurrency_limits: dict = None,
        gpu_node_types: list = None,
        busy_timeout_ms: int = 15000,
    ):
        self.database = os.fspath(database)
        self.max_concurrent_jobs_per_user = max(1, int(max_concurrent_jobs_per_user))
        self.admin_concurrency_limit = max(0, int(admin_concurrency_limit))
        self.resource_concurrency_limits = {
            str(name): max(1, int(limit))
            for name, limit in (resource_concurrency_limits or {}).items()
        }
        self.gpu_node_types = {
            str(node_type) for node_type in (gpu_node_types or []) if node_type
        }
        self.busy_timeout_ms = max(1000, int(busy_timeout_ms))
        self._log_line_cache: dict[str, tuple[int, int]] = {}
        self._log_line_cache_lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(self.database)), exist_ok=True)
        self._initialize()
        try:
            os.chmod(self.database, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scheduler_jobs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    client_id TEXT,
                    ingress_port INTEGER,
                    worker_port INTEGER,
                    priority REAL NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    concurrency_limit INTEGER NOT NULL,
                    submitted_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    heartbeat_at REAL,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS scheduler_jobs_status_priority
                    ON scheduler_jobs(status, priority, sequence);
                CREATE INDEX IF NOT EXISTS scheduler_jobs_owner_status
                    ON scheduler_jobs(owner_id, status);
                CREATE INDEX IF NOT EXISTS scheduler_jobs_client
                    ON scheduler_jobs(client_id, sequence);

                CREATE TABLE IF NOT EXISTS scheduler_workers (
                    port INTEGER PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    active_prompt_id TEXT,
                    last_heartbeat REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scheduler_assets (
                    asset_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    prompt_id TEXT,
                    worker_port INTEGER,
                    file_path TEXT,
                    data TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS scheduler_assets_owner
                    ON scheduler_assets(owner_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS scheduler_job_logs (
                    prompt_id TEXT PRIMARY KEY,
                    worker_port INTEGER,
                    log_file TEXT NOT NULL,
                    start_offset INTEGER NOT NULL,
                    end_offset INTEGER NOT NULL,
                    start_line INTEGER,
                    end_line INTEGER,
                    content BLOB NOT NULL,
                    content_bytes INTEGER NOT NULL,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    captured_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scheduler_api_logs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_id TEXT NOT NULL,
                    recorded_at REAL NOT NULL,
                    client TEXT NOT NULL,
                    method TEXT NOT NULL,
                    url TEXT NOT NULL,
                    request_headers TEXT NOT NULL,
                    request_body BLOB,
                    response_status INTEGER,
                    response_headers TEXT NOT NULL,
                    response_body BLOB,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS scheduler_api_logs_prompt
                    ON scheduler_api_logs(prompt_id, sequence);
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_column(
                    connection,
                    "scheduler_jobs",
                    "resource_class",
                    "TEXT NOT NULL DEFAULT 'default'",
                )
                self._ensure_column(
                    connection,
                    "scheduler_workers",
                    "resource_class",
                    "TEXT NOT NULL DEFAULT 'default'",
                )
                self._ensure_column(connection, "scheduler_jobs", "log_file", "TEXT")
                self._ensure_column(
                    connection, "scheduler_jobs", "log_start_offset", "INTEGER"
                )
                self._ensure_column(
                    connection, "scheduler_jobs", "log_end_offset", "INTEGER"
                )
                self._ensure_column(
                    connection, "scheduler_jobs", "log_start_line", "INTEGER"
                )
                self._ensure_column(
                    connection, "scheduler_jobs", "log_end_line", "INTEGER"
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS scheduler_jobs_resource_status
                    ON scheduler_jobs(resource_class, status, priority, sequence)
                    """
                )
                self._classify_legacy_queued_jobs(connection)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def _classify_legacy_queued_jobs(self, connection: sqlite3.Connection) -> None:
        if not self.gpu_node_types:
            return
        rows = connection.execute(
            """
            SELECT prompt_id, payload, concurrency_limit
            FROM scheduler_jobs
            WHERE status = ? AND resource_class = 'default'
            """,
            (QUEUED,),
        ).fetchall()
        for row in rows:
            resource_class = self.classify_item(self._decode_item(row["payload"]))
            limit = int(row["concurrency_limit"])
            if limit:
                limit = self.resource_concurrency_limits.get(resource_class, limit)
            connection.execute(
                """
                UPDATE scheduler_jobs
                SET resource_class = ?, concurrency_limit = ?
                WHERE prompt_id = ? AND status = ? AND resource_class = 'default'
                """,
                (resource_class, limit, row["prompt_id"], QUEUED),
            )

    def classify_item(self, item: tuple) -> str:
        if not self.gpu_node_types:
            return "default"
        prompt = item[2] if len(item) > 2 and isinstance(item[2], dict) else {}
        for node in prompt.values():
            if (
                isinstance(node, dict)
                and str(node.get("class_type") or "") in self.gpu_node_types
            ):
                return "gpu"
        return "api"

    @staticmethod
    def _encode_item(item: tuple) -> str:
        if not isinstance(item, tuple) or len(item) < 6:
            raise ValueError("ComfyUI queue item must contain at least six fields")
        return json.dumps(list(item[:6]), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode_item(payload: str) -> tuple:
        value = json.loads(payload)
        if not isinstance(value, list) or len(value) != 6:
            raise ValueError("Invalid scheduler queue payload")
        return tuple(value)

    def enqueue(
        self,
        item: tuple,
        owner_id: str,
        ingress_port: int,
        concurrency_exempt: bool = False,
        resource_class: str = None,
    ) -> None:
        prompt_id = str(item[1])
        extra_data = item[3] if isinstance(item[3], dict) else {}
        client_id = str(extra_data.get("client_id") or "") or None
        resource_class = str(resource_class or self.classify_item(item))
        limit = (
            self.admin_concurrency_limit
            if concurrency_exempt
            else self.resource_concurrency_limits.get(
                resource_class, self.max_concurrent_jobs_per_user
            )
        )
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO scheduler_jobs (
                        prompt_id, owner_id, client_id, ingress_port, priority,
                        payload, status, concurrency_limit, resource_class,
                        submitted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        prompt_id,
                        owner_id or "",
                        client_id,
                        int(ingress_port or 0),
                        float(item[0]),
                        self._encode_item(item),
                        QUEUED,
                        limit,
                        resource_class,
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def register_worker(
        self, port: int, pid: int, resource_class: str = "default"
    ) -> None:
        now = time.time()
        resource_class = str(resource_class or "default")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                old = connection.execute(
                    "SELECT pid, active_prompt_id FROM scheduler_workers WHERE port = ?",
                    (int(port),),
                ).fetchone()
                if old and old["pid"] != int(pid) and old["active_prompt_id"]:
                    active_job = connection.execute(
                        "SELECT log_file FROM scheduler_jobs WHERE prompt_id = ?",
                        (old["active_prompt_id"],),
                    ).fetchone()
                    _, log_end_offset, log_end_line = self._log_position(
                        active_job["log_file"] if active_job else ""
                    )
                    connection.execute(
                        """
                        UPDATE scheduler_jobs
                        SET status = ?, finished_at = ?, error = ?,
                            log_end_offset = ?, log_end_line = ?
                        WHERE prompt_id = ? AND status = ?
                        """,
                        (
                            WORKER_LOST,
                            now,
                            "Worker process restarted before the task completed",
                            log_end_offset,
                            log_end_line,
                            old["active_prompt_id"],
                            RUNNING,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO scheduler_workers(
                        port, pid, active_prompt_id, last_heartbeat, resource_class
                    ) VALUES (?, ?, NULL, ?, ?)
                    ON CONFLICT(port) DO UPDATE SET
                        pid = excluded.pid,
                        active_prompt_id = NULL,
                        last_heartbeat = excluded.last_heartbeat,
                        resource_class = excluded.resource_class
                    """,
                    (int(port), int(pid), now, resource_class),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def heartbeat(
        self,
        port: int,
        pid: int,
        stale_seconds: int,
        resource_class: str = "default",
    ) -> list[str]:
        now = time.time()
        resource_class = str(resource_class or "default")
        lost = []
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO scheduler_workers(
                        port, pid, active_prompt_id, last_heartbeat, resource_class
                    ) VALUES (?, ?, NULL, ?, ?)
                    ON CONFLICT(port) DO UPDATE SET
                        pid = excluded.pid,
                        last_heartbeat = excluded.last_heartbeat,
                        resource_class = excluded.resource_class
                    """,
                    (int(port), int(pid), now, resource_class),
                )
                stale = connection.execute(
                    """
                    SELECT port, active_prompt_id
                    FROM scheduler_workers
                    WHERE last_heartbeat < ? AND active_prompt_id IS NOT NULL
                    """,
                    (now - max(1, int(stale_seconds)),),
                ).fetchall()
                for worker in stale:
                    prompt_id = worker["active_prompt_id"]
                    active_job = connection.execute(
                        "SELECT log_file FROM scheduler_jobs WHERE prompt_id = ?",
                        (prompt_id,),
                    ).fetchone()
                    _, log_end_offset, log_end_line = self._log_position(
                        active_job["log_file"] if active_job else ""
                    )
                    updated = connection.execute(
                        """
                        UPDATE scheduler_jobs
                        SET status = ?, finished_at = ?, error = ?,
                            log_end_offset = ?, log_end_line = ?
                        WHERE prompt_id = ? AND status = ?
                        """,
                        (
                            WORKER_LOST,
                            now,
                            f"Worker on port {worker['port']} stopped reporting heartbeats",
                            log_end_offset,
                            log_end_line,
                            prompt_id,
                            RUNNING,
                        ),
                    ).rowcount
                    if updated:
                        lost.append(prompt_id)
                    connection.execute(
                        "UPDATE scheduler_workers SET active_prompt_id = NULL WHERE port = ?",
                        (worker["port"],),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return lost

    def _line_at_offset(self, path: str, offset: int) -> int:
        with self._log_line_cache_lock:
            cached_offset, newline_count = self._log_line_cache.get(path, (0, 0))
            if cached_offset > offset:
                cached_offset, newline_count = 0, 0
            try:
                with open(path, "rb") as log:
                    log.seek(cached_offset)
                    remaining = offset - cached_offset
                    while remaining:
                        chunk = log.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        newline_count += chunk.count(b"\n")
                        cached_offset += len(chunk)
                        remaining -= len(chunk)
            except OSError:
                return 1
            self._log_line_cache[path] = (cached_offset, newline_count)
            return newline_count + 1

    def _log_position(
        self, log_file: str
    ) -> tuple[Optional[str], Optional[int], Optional[int]]:
        if not log_file:
            return None, None, None
        path = os.path.abspath(os.fspath(log_file))
        try:
            offset = os.path.getsize(path)
            return path, offset, self._line_at_offset(path, offset)
        except OSError:
            return path, 0, 1

    def claim(
        self,
        port: int,
        pid: int,
        resource_class: str = "default",
        log_file: str = "",
    ) -> Optional[tuple]:
        """Claim the first eligible job and return its ComfyUI queue tuple."""
        now = time.time()
        resource_class = str(resource_class or "default")
        log_path, log_offset, log_line = self._log_position(log_file)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                worker = connection.execute(
                    "SELECT active_prompt_id FROM scheduler_workers WHERE port = ?",
                    (int(port),),
                ).fetchone()
                if worker and worker["active_prompt_id"]:
                    active = connection.execute(
                        "SELECT status FROM scheduler_jobs WHERE prompt_id = ?",
                        (worker["active_prompt_id"],),
                    ).fetchone()
                    if active and active["status"] == RUNNING:
                        connection.execute("COMMIT")
                        return None

                row = connection.execute(
                    """
                    SELECT candidate.*
                    FROM scheduler_jobs AS candidate
                    WHERE candidate.status = ?
                      AND (
                        ? = 'default'
                        OR candidate.resource_class = 'default'
                        OR candidate.resource_class = ?
                      )
                      AND (
                        candidate.concurrency_limit = 0
                        OR (
                            SELECT COUNT(*)
                            FROM scheduler_jobs AS active
                            WHERE active.owner_id = candidate.owner_id
                              AND active.status = ?
                              AND active.resource_class = candidate.resource_class
                        ) < candidate.concurrency_limit
                      )
                    ORDER BY candidate.priority ASC, candidate.sequence ASC
                    LIMIT 1
                    """,
                    (QUEUED, resource_class, resource_class, RUNNING),
                ).fetchone()
                if not row:
                    connection.execute(
                        """
                        INSERT INTO scheduler_workers(
                            port, pid, active_prompt_id, last_heartbeat, resource_class
                        ) VALUES (?, ?, NULL, ?, ?)
                        ON CONFLICT(port) DO UPDATE SET
                            pid = excluded.pid,
                            last_heartbeat = excluded.last_heartbeat,
                            resource_class = excluded.resource_class
                        """,
                        (int(port), int(pid), now, resource_class),
                    )
                    connection.execute("COMMIT")
                    return None

                updated = connection.execute(
                    """
                    UPDATE scheduler_jobs
                    SET status = ?, worker_port = ?, started_at = ?, heartbeat_at = ?,
                        log_file = ?, log_start_offset = ?, log_end_offset = NULL,
                        log_start_line = ?, log_end_line = NULL
                    WHERE prompt_id = ? AND status = ?
                    """,
                    (
                        RUNNING,
                        int(port),
                        now,
                        now,
                        log_path,
                        log_offset,
                        log_line,
                        row["prompt_id"],
                        QUEUED,
                    ),
                ).rowcount
                if updated != 1:
                    connection.execute("ROLLBACK")
                    return None
                connection.execute(
                    """
                    INSERT INTO scheduler_workers(
                        port, pid, active_prompt_id, last_heartbeat, resource_class
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(port) DO UPDATE SET
                        pid = excluded.pid,
                        active_prompt_id = excluded.active_prompt_id,
                        last_heartbeat = excluded.last_heartbeat,
                        resource_class = excluded.resource_class
                    """,
                    (int(port), int(pid), row["prompt_id"], now, resource_class),
                )
                connection.execute("COMMIT")
                return self._decode_item(row["payload"]) + (row["owner_id"],)
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def complete(
        self,
        prompt_id: str,
        worker_port: int,
        succeeded: bool,
        error: str = "",
        log_file: str = "",
    ) -> None:
        now = time.time()
        status = COMPLETED if succeeded else FAILED
        log_path, log_offset, log_line = self._log_position(log_file)
        excerpt = None
        start_offset = None
        start_line = None
        if log_path and log_offset is not None:
            with closing(self._connect()) as lookup:
                log_row = lookup.execute(
                    """
                    SELECT log_start_offset, log_start_line
                    FROM scheduler_jobs WHERE prompt_id = ?
                    """,
                    (prompt_id,),
                ).fetchone()
            if log_row and log_row["log_start_offset"] is not None:
                start_offset = int(log_row["log_start_offset"])
                start_line = log_row["log_start_line"]
                byte_count = max(0, int(log_offset) - start_offset)
                try:
                    with open(log_path, "rb") as log:
                        log.seek(start_offset)
                        excerpt = log.read(byte_count)
                except OSError:
                    excerpt = None
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE scheduler_jobs
                    SET status = ?, finished_at = ?, error = ?,
                        log_file = COALESCE(log_file, ?),
                        log_end_offset = ?, log_end_line = ?
                    WHERE prompt_id = ? AND status IN (?, ?)
                    """,
                    (
                        status,
                        now,
                        error or None,
                        log_path,
                        log_offset,
                        log_line,
                        prompt_id,
                        RUNNING,
                        WORKER_LOST,
                    ),
                )
                if excerpt is not None:
                    connection.execute(
                        """
                        INSERT INTO scheduler_job_logs(
                            prompt_id, worker_port, log_file,
                            start_offset, end_offset, start_line, end_line,
                            content, content_bytes, truncated, captured_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(prompt_id) DO UPDATE SET
                            worker_port = excluded.worker_port,
                            log_file = excluded.log_file,
                            start_offset = excluded.start_offset,
                            end_offset = excluded.end_offset,
                            start_line = excluded.start_line,
                            end_line = excluded.end_line,
                            content = excluded.content,
                            content_bytes = excluded.content_bytes,
                            truncated = excluded.truncated,
                            captured_at = excluded.captured_at
                        """,
                        (
                            prompt_id,
                            int(worker_port),
                            log_path,
                            start_offset,
                            int(log_offset),
                            start_line,
                            log_line,
                            excerpt,
                            len(excerpt),
                            0,
                            now,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE scheduler_workers
                    SET active_prompt_id = NULL, last_heartbeat = ?
                    WHERE port = ? AND active_prompt_id = ?
                    """,
                    (now, int(worker_port), prompt_id),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def get_job_log(self, prompt_id: str) -> Optional[dict]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM scheduler_job_logs WHERE prompt_id = ?", (prompt_id,)
            ).fetchone()
        return dict(row) if row else None

    def record_api_call(self, **record) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO scheduler_api_logs(
                    prompt_id, recorded_at, client, method, url,
                    request_headers, request_body, response_status,
                    response_headers, response_body, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["prompt_id"],
                    record["recorded_at"],
                    record["client"],
                    record["method"],
                    record["url"],
                    record["request_headers"],
                    record.get("request_body"),
                    record.get("response_status"),
                    record["response_headers"],
                    record.get("response_body"),
                    record.get("error"),
                ),
            )

    def get_api_logs(self, prompt_id: str) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM scheduler_api_logs WHERE prompt_id = ? ORDER BY sequence",
                (prompt_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def cancel(self, prompt_id: str, owner_id: str = None, admin: bool = False) -> bool:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT owner_id, status FROM scheduler_jobs WHERE prompt_id = ?",
                    (prompt_id,),
                ).fetchone()
                if (
                    not row
                    or row["status"] != QUEUED
                    or (not admin and row["owner_id"] != (owner_id or ""))
                ):
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    """
                    UPDATE scheduler_jobs
                    SET status = ?, finished_at = ?
                    WHERE prompt_id = ? AND status = ?
                    """,
                    (CANCELLED, time.time(), prompt_id, QUEUED),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def cancel_all(self, owner_id: str = None, admin: bool = False) -> int:
        now = time.time()
        with closing(self._connect()) as connection:
            if admin:
                result = connection.execute(
                    """
                    UPDATE scheduler_jobs
                    SET status = ?, finished_at = ?
                    WHERE status = ?
                    """,
                    (CANCELLED, now, QUEUED),
                )
            else:
                result = connection.execute(
                    """
                    UPDATE scheduler_jobs
                    SET status = ?, finished_at = ?
                    WHERE status = ? AND owner_id = ?
                    """,
                    (CANCELLED, now, QUEUED, owner_id or ""),
                )
        return result.rowcount

    def visible_items(self, owner_id: str = None, admin: bool = False) -> tuple[list, list]:
        where = "status IN (?, ?)"
        params: list = [RUNNING, QUEUED]
        if not admin:
            where += " AND owner_id = ?"
            params.append(owner_id or "")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT payload, owner_id, status
                FROM scheduler_jobs
                WHERE {where}
                ORDER BY priority ASC, sequence ASC
                """,
                tuple(params),
            ).fetchall()
        running = []
        queued = []
        for row in rows:
            item = self._decode_item(row["payload"])
            (running if row["status"] == RUNNING else queued).append(item)
        return running, queued

    def delete_matching(
        self,
        predicate: Callable[[tuple], bool],
        owner_id: str,
        admin: bool,
    ) -> bool:
        with closing(self._connect()) as connection:
            query = "SELECT prompt_id, payload, owner_id FROM scheduler_jobs WHERE status = ?"
            params: list = [QUEUED]
            if not admin:
                query += " AND owner_id = ?"
                params.append(owner_id or "")
            query += " ORDER BY priority ASC, sequence ASC"
            rows = connection.execute(query, tuple(params)).fetchall()
        for row in rows:
            item = self._decode_item(row["payload"])
            if predicate(item) and self.cancel(row["prompt_id"], owner_id, admin):
                return True
        return False

    def get_job(self, prompt_id: str) -> Optional[dict]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM scheduler_jobs WHERE prompt_id = ?", (prompt_id,)
            ).fetchone()
        return dict(row) if row else None

    def running_jobs(self, owner_id: str = None, admin: bool = False) -> list[dict]:
        query = "SELECT * FROM scheduler_jobs WHERE status = ?"
        params: list = [RUNNING]
        if not admin:
            query += " AND owner_id = ?"
            params.append(owner_id or "")
        with closing(self._connect()) as connection:
            return [
                dict(row)
                for row in connection.execute(query, tuple(params)).fetchall()
            ]

    def client_ingress(self, client_id: str) -> Optional[int]:
        if not client_id:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT ingress_port
                FROM scheduler_jobs
                WHERE client_id = ? AND ingress_port IS NOT NULL
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (client_id,),
            ).fetchone()
        return int(row["ingress_port"]) if row and row["ingress_port"] else None

    def worker_ports(self, stale_seconds: int = 60) -> list[int]:
        cutoff = time.time() - max(1, int(stale_seconds))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT port
                FROM scheduler_workers
                WHERE last_heartbeat >= ?
                ORDER BY port
                """,
                (cutoff,),
            ).fetchall()
        return [int(row["port"]) for row in rows]

    def upsert_asset(
        self,
        asset_id: str,
        owner_id: str,
        prompt_id: str,
        worker_port: int,
        data: dict,
    ) -> None:
        if not asset_id:
            return
        file_path = str(data.get("fullpath") or data.get("file_path") or "")
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO scheduler_assets(
                    asset_id, owner_id, prompt_id, worker_port,
                    file_path, data, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    prompt_id = excluded.prompt_id,
                    worker_port = excluded.worker_port,
                    file_path = excluded.file_path,
                    data = excluded.data,
                    updated_at = excluded.updated_at
                """,
                (
                    str(asset_id),
                    owner_id or "",
                    prompt_id,
                    int(worker_port or 0),
                    file_path,
                    json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                    time.time(),
                ),
            )

    def get_asset(self, asset_id: str) -> Optional[dict]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM scheduler_assets WHERE asset_id = ?", (str(asset_id),)
            ).fetchone()
        return dict(row) if row else None


class WorkerHeartbeat:
    def __init__(
        self,
        store: SchedulerStore,
        port: int,
        interval_seconds: int,
        stale_seconds: int,
        resource_class: str = "default",
    ):
        self.store = store
        self.port = int(port)
        self.interval_seconds = max(1, int(interval_seconds))
        self.stale_seconds = max(self.interval_seconds * 2, int(stale_seconds))
        self.resource_class = str(resource_class or "default")
        self.pid = os.getpid()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"account-manager-heartbeat-{self.port}",
            daemon=True,
        )

    def start(self) -> None:
        self.store.register_worker(self.port, self.pid, self.resource_class)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.store.heartbeat(
                    self.port,
                    self.pid,
                    self.stale_seconds,
                    self.resource_class,
                )
            except Exception:
                # The queue operations surface database failures to ComfyUI;
                # heartbeat errors remain retryable and must not kill the worker.
                pass

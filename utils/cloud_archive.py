import base64
import gzip
import hashlib
import io
import ipaddress
import json
import mimetypes
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo


PENDING = "pending"
UPLOADING = "uploading"
UPLOADED = "uploaded"
CLOUD_COMPLETED = "cloud_completed"
CLOUD_FAILED = "cloud_failed"
NO_ARTIFACTS = "no_artifacts"
SOURCE_MISSING = "source_missing"
TERMINAL_ARTIFACT_STATES = (UPLOADED, CLOUD_FAILED, SOURCE_MISSING)
RETRY_DELAYS = (30, 120)


@dataclass(frozen=True)
class CloudArchiveConfig:
    enabled: bool
    server_id: str
    region: str
    endpoint: str
    bucket: str
    prefix: str
    public_base_url: str
    max_attempts: int = 3
    upload_concurrency: int = 2
    remote_max_bytes: int = 20 * 1024 * 1024 * 1024
    manifest_max_bytes: int = 2 * 1024 * 1024 * 1024
    staging_dir: str = "cloud_staging"
    checkpoint_dir: str = "cloud_upload_checkpoints"

    def validate(self) -> None:
        if not self.enabled:
            return
        required = {
            "cloud_server_id": self.server_id,
            "cloud_oss_region": self.region,
            "cloud_oss_endpoint": self.endpoint,
            "cloud_oss_bucket": self.bucket,
            "cloud_oss_prefix": self.prefix,
            "cloud_public_base_url": self.public_base_url,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError("Missing cloud archive settings: " + ", ".join(missing))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.server_id):
            raise ValueError("cloud_server_id contains unsupported characters")

    def media_key(self, prompt_id: str, ordinal: int, filename: str, finished_at: float) -> str:
        date = datetime.fromtimestamp(float(finished_at or time.time()), ZoneInfo("Asia/Shanghai"))
        safe_name = sanitize_filename(filename)
        return (
            f"{self.prefix}/public/servers/{self.server_id}/"
            f"{date:%Y/%m/%d}/{prompt_id}/{int(ordinal):03d}-{safe_name}"
        )

    def manifest_key(self, prompt_id: str) -> str:
        return f"{self.prefix}/private/jobs/{prompt_id}/manifest.json.gz"

    def oss_uri(self, key: str) -> str:
        return f"oss://{self.bucket}/{key}"

    def public_url(self, key: str) -> str:
        return f"{self.public_base_url}/{urllib.parse.quote(key, safe='/')}"


def sanitize_filename(filename: str) -> str:
    name = os.path.basename(str(filename or "artifact"))
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", name).strip(" .")
    if not name:
        name = "artifact"
    if len(name.encode("utf-8")) <= 220:
        return name
    stem, suffix = os.path.splitext(name)
    suffix = suffix[:32]
    while stem and len((stem + suffix).encode("utf-8")) > 220:
        stem = stem[:-1]
    return (stem or "artifact") + suffix


def _safe_join(root: str, subfolder: str, filename: str) -> Optional[str]:
    root_path = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(root_path, str(subfolder or ""), filename))
    try:
        if os.path.commonpath((root_path, candidate)) != root_path:
            return None
    except ValueError:
        return None
    return candidate


def _walk_outputs(value, visit: Callable[[object, str], None], path: str = "outputs") -> None:
    visit(value, path)
    if isinstance(value, dict):
        for key, child in value.items():
            _walk_outputs(child, visit, f"{path}/{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _walk_outputs(child, visit, f"{path}/{index}")


def discover_artifacts(history_item: dict, output_dir: str, temp_dir: str) -> list[dict]:
    discovered = []
    seen = set()

    def add_local(path: str, filename: str, reference: dict, output_path: str) -> None:
        if not path:
            return
        normalized = os.path.realpath(path)
        key = ("local", os.path.normcase(normalized))
        if key in seen:
            return
        seen.add(key)
        discovered.append(
            {
                "source_kind": "local_output",
                "source_path": normalized,
                "source_url": None,
                "filename": sanitize_filename(filename or os.path.basename(normalized)),
                "mime_type": mimetypes.guess_type(filename or normalized)[0],
                "reference": reference,
                "output_path": output_path,
            }
        )

    def visit(value, output_path: str) -> None:
        if isinstance(value, dict):
            filename = str(value.get("filename") or "")
            explicit = str(value.get("fullpath") or value.get("file_path") or "")
            if explicit:
                add_local(explicit, filename or os.path.basename(explicit), value, output_path)
            elif filename and filename == os.path.basename(filename):
                kind = str(value.get("type") or "output").lower()
                root = temp_dir if kind == "temp" else output_dir
                candidate = _safe_join(root, str(value.get("subfolder") or ""), filename)
                if candidate:
                    add_local(candidate, filename, value, output_path)
        elif isinstance(value, str) and value.startswith(("http://", "https://")):
            normalized = value
            key = ("remote", normalized)
            if key in seen:
                return
            parsed = urllib.parse.urlsplit(value)
            filename = sanitize_filename(os.path.basename(parsed.path) or "remote-artifact")
            seen.add(key)
            discovered.append(
                {
                    "source_kind": "remote_url",
                    "source_path": None,
                    "source_url": normalized,
                    "filename": filename,
                    "mime_type": mimetypes.guess_type(filename)[0],
                    "reference": None,
                    "output_path": output_path,
                }
            )

    _walk_outputs((history_item or {}).get("outputs", {}), visit)
    return discovered


def _blob(value: bytes) -> dict:
    raw = bytes(value)
    return {
        "$sqlite_type": "blob",
        "encoding": "base64",
        "byte_length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "data": base64.b64encode(raw).decode("ascii"),
    }


def json_value(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _blob(bytes(value))
    return value


def row_dict(row) -> Optional[dict]:
    if row is None:
        return None
    return {key: json_value(row[key]) for key in row.keys()}


def _asset_ids(value) -> set[str]:
    found = set()
    if isinstance(value, dict):
        asset_id = str(value.get("id") or "")
        if asset_id:
            found.add(asset_id)
        for child in value.values():
            found.update(_asset_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_asset_ids(child))
    return found


class CloudArchiveStore:
    def __init__(self, database: str, busy_timeout_ms: int = 15000):
        self.database = os.fspath(database)
        self.busy_timeout_ms = max(1000, int(busy_timeout_ms))
        self._initialize()

    def _connect(self, readonly: bool = False) -> sqlite3.Connection:
        database = self.database
        if readonly:
            database = f"file:{os.path.abspath(database)}?mode=ro"
        connection = sqlite3.connect(
            database,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            uri=readonly,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scheduler_cloud_tasks (
                    prompt_id TEXT PRIMARY KEY,
                    server_id TEXT NOT NULL,
                    owner_id TEXT,
                    worker_port INTEGER,
                    generation_status TEXT NOT NULL,
                    cloud_status TEXT NOT NULL,
                    artifact_count INTEGER NOT NULL DEFAULT 0,
                    uploaded_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    manifest_object_key TEXT,
                    manifest_oss_uri TEXT,
                    manifest_size_bytes INTEGER,
                    manifest_sha256 TEXT,
                    manifest_upload_status TEXT NOT NULL DEFAULT 'pending',
                    manifest_attempt_count INTEGER NOT NULL DEFAULT 0,
                    manifest_next_attempt_at REAL,
                    manifest_lease_owner TEXT,
                    manifest_lease_expires_at REAL,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL
                );
                CREATE INDEX IF NOT EXISTS scheduler_cloud_tasks_status
                    ON scheduler_cloud_tasks(cloud_status, updated_at);

                CREATE TABLE IF NOT EXISTS scheduler_cloud_artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_id TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_path TEXT,
                    source_url TEXT,
                    output_path TEXT,
                    reference_data TEXT,
                    filename TEXT NOT NULL,
                    mime_type TEXT,
                    size_bytes INTEGER,
                    sha256 TEXT,
                    oss_object_key TEXT NOT NULL,
                    oss_uri TEXT,
                    public_url TEXT,
                    etag TEXT,
                    oss_crc64 TEXT,
                    upload_status TEXT NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    uploaded_at REAL,
                    UNIQUE(prompt_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS scheduler_cloud_artifacts_claim
                    ON scheduler_cloud_artifacts(upload_status, next_attempt_at, id);
                CREATE INDEX IF NOT EXISTS scheduler_cloud_artifacts_prompt
                    ON scheduler_cloud_artifacts(prompt_id, ordinal);
                """
            )

    def enqueue_task(
        self,
        config: CloudArchiveConfig,
        prompt_id: str,
        owner_id: str,
        worker_port: int,
        generation_status: str,
        finished_at: float,
        artifacts: list[dict],
    ) -> None:
        now = time.time()
        manifest_key = config.manifest_key(prompt_id)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT server_id FROM scheduler_cloud_tasks WHERE prompt_id = ?",
                    (prompt_id,),
                ).fetchone()
                if existing and existing["server_id"] != config.server_id:
                    raise RuntimeError("prompt_id already belongs to another cloud server_id")
                connection.execute(
                    """
                    INSERT INTO scheduler_cloud_tasks(
                        prompt_id, server_id, owner_id, worker_port, generation_status,
                        cloud_status, artifact_count, manifest_object_key,
                        manifest_oss_uri, manifest_upload_status,
                        manifest_next_attempt_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(prompt_id) DO UPDATE SET
                        owner_id=excluded.owner_id,
                        worker_port=excluded.worker_port,
                        generation_status=excluded.generation_status,
                        artifact_count=excluded.artifact_count,
                        manifest_object_key=excluded.manifest_object_key,
                        manifest_oss_uri=excluded.manifest_oss_uri,
                        updated_at=excluded.updated_at
                    """,
                    (
                        prompt_id,
                        config.server_id,
                        owner_id or "",
                        int(worker_port or 0),
                        generation_status,
                        PENDING if artifacts else NO_ARTIFACTS,
                        len(artifacts),
                        manifest_key,
                        config.oss_uri(manifest_key),
                        PENDING,
                        now,
                        now,
                        now,
                    ),
                )
                for ordinal, artifact in enumerate(artifacts, 1):
                    key = config.media_key(
                        prompt_id, ordinal, artifact["filename"], finished_at
                    )
                    connection.execute(
                        """
                        INSERT INTO scheduler_cloud_artifacts(
                            prompt_id, server_id, ordinal, source_kind, source_path,
                            source_url, output_path, reference_data, filename, mime_type,
                            oss_object_key, oss_uri, public_url, upload_status,
                            next_attempt_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(prompt_id, ordinal) DO UPDATE SET
                            source_path=excluded.source_path,
                            source_url=excluded.source_url,
                            output_path=excluded.output_path,
                            reference_data=excluded.reference_data,
                            filename=excluded.filename,
                            mime_type=excluded.mime_type,
                            oss_object_key=excluded.oss_object_key,
                            oss_uri=excluded.oss_uri,
                            public_url=excluded.public_url,
                            updated_at=excluded.updated_at
                        """,
                        (
                            prompt_id,
                            config.server_id,
                            ordinal,
                            artifact["source_kind"],
                            artifact.get("source_path"),
                            artifact.get("source_url"),
                            artifact.get("output_path"),
                            json.dumps(artifact.get("reference"), ensure_ascii=False),
                            artifact["filename"],
                            artifact.get("mime_type"),
                            key,
                            config.oss_uri(key),
                            config.public_url(key),
                            PENDING,
                            now,
                            now,
                            now,
                        ),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def claim_artifact(self, claimant: str, concurrency: int, lease_seconds: int = 1800):
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                active_artifacts = connection.execute(
                    """
                    SELECT COUNT(*) FROM scheduler_cloud_artifacts
                    WHERE upload_status = ? AND lease_expires_at > ?
                    """,
                    (UPLOADING, now),
                ).fetchone()[0]
                active_manifests = connection.execute(
                    """
                    SELECT COUNT(*) FROM scheduler_cloud_tasks
                    WHERE manifest_upload_status = ? AND manifest_lease_expires_at > ?
                    """,
                    (UPLOADING, now),
                ).fetchone()[0]
                if int(active_artifacts) + int(active_manifests) >= int(concurrency):
                    connection.execute("COMMIT")
                    return None
                row = connection.execute(
                    """
                    SELECT * FROM scheduler_cloud_artifacts
                    WHERE (
                        upload_status = ?
                        OR (upload_status = ? AND COALESCE(lease_expires_at, 0) <= ?)
                    )
                      AND COALESCE(next_attempt_at, 0) <= ?
                    ORDER BY id
                    LIMIT 1
                    """,
                    (PENDING, UPLOADING, now, now),
                ).fetchone()
                if not row:
                    connection.execute("COMMIT")
                    return None
                connection.execute(
                    """
                    UPDATE scheduler_cloud_artifacts
                    SET upload_status=?, attempt_count=attempt_count+1,
                        lease_owner=?, lease_expires_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (UPLOADING, claimant, now + lease_seconds, now, row["id"]),
                )
                connection.execute(
                    "UPDATE scheduler_cloud_tasks SET cloud_status=?, updated_at=? WHERE prompt_id=?",
                    (UPLOADING, now, row["prompt_id"]),
                )
                claimed = connection.execute(
                    "SELECT * FROM scheduler_cloud_artifacts WHERE id=?", (row["id"],)
                ).fetchone()
                connection.execute("COMMIT")
                return dict(claimed)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def artifact_succeeded(self, artifact_id: int, result: dict) -> None:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT prompt_id FROM scheduler_cloud_artifacts WHERE id=?",
                    (artifact_id,),
                ).fetchone()
                if not row:
                    return
                connection.execute(
                    """
                    UPDATE scheduler_cloud_artifacts
                    SET upload_status=?, mime_type=?, size_bytes=?, sha256=?, etag=?, oss_crc64=?,
                        lease_owner=NULL, lease_expires_at=NULL, next_attempt_at=NULL,
                        last_error=NULL, uploaded_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        UPLOADED,
                        result.get("mime_type"),
                        result.get("size_bytes"),
                        result.get("sha256"),
                        result.get("etag"),
                        result.get("oss_crc64"),
                        now,
                        now,
                        artifact_id,
                    ),
                )
                self._refresh_counts(connection, row["prompt_id"], now)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def artifact_failed(self, artifact_id: int, error: str, max_attempts: int) -> None:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT prompt_id, attempt_count FROM scheduler_cloud_artifacts WHERE id=?",
                    (artifact_id,),
                ).fetchone()
                if not row:
                    return
                attempts = int(row["attempt_count"])
                terminal = attempts >= int(max_attempts)
                delay = RETRY_DELAYS[min(max(0, attempts - 1), len(RETRY_DELAYS) - 1)]
                connection.execute(
                    """
                    UPDATE scheduler_cloud_artifacts
                    SET upload_status=?, next_attempt_at=?, lease_owner=NULL,
                        lease_expires_at=NULL, last_error=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        CLOUD_FAILED if terminal else PENDING,
                        None if terminal else now + delay,
                        str(error),
                        now,
                        artifact_id,
                    ),
                )
                self._refresh_counts(connection, row["prompt_id"], now)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def artifact_source_missing(self, artifact_id: int, error: str) -> None:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT prompt_id FROM scheduler_cloud_artifacts WHERE id=?",
                    (artifact_id,),
                ).fetchone()
                if not row:
                    connection.execute("COMMIT")
                    return
                connection.execute(
                    """
                    UPDATE scheduler_cloud_artifacts
                    SET upload_status=?, next_attempt_at=NULL, lease_owner=NULL,
                        lease_expires_at=NULL, last_error=?, updated_at=?
                    WHERE id=?
                    """,
                    (SOURCE_MISSING, str(error), now, artifact_id),
                )
                self._refresh_counts(connection, row["prompt_id"], now)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def renew_artifact_lease(
        self, artifact_id: int, claimant: str, lease_seconds: int = 1800
    ) -> bool:
        now = time.time()
        with closing(self._connect()) as connection:
            changed = connection.execute(
                """
                UPDATE scheduler_cloud_artifacts
                SET lease_expires_at=?, updated_at=?
                WHERE id=? AND upload_status=? AND lease_owner=?
                """,
                (now + lease_seconds, now, artifact_id, UPLOADING, claimant),
            ).rowcount
        return bool(changed)

    @staticmethod
    def _refresh_counts(connection, prompt_id: str, now: float) -> None:
        counts = connection.execute(
            """
            SELECT COUNT(*) total,
                   SUM(CASE WHEN upload_status='uploaded' THEN 1 ELSE 0 END) uploaded,
                   SUM(CASE WHEN upload_status IN ('cloud_failed','source_missing') THEN 1 ELSE 0 END) failed
            FROM scheduler_cloud_artifacts WHERE prompt_id=?
            """,
            (prompt_id,),
        ).fetchone()
        connection.execute(
            """
            UPDATE scheduler_cloud_tasks
            SET artifact_count=?, uploaded_count=?, failed_count=?, updated_at=?
            WHERE prompt_id=?
            """,
            (
                int(counts["total"] or 0),
                int(counts["uploaded"] or 0),
                int(counts["failed"] or 0),
                now,
                prompt_id,
            ),
        )

    def claim_manifest(
        self,
        claimant: str,
        max_attempts: int,
        concurrency: int,
        lease_seconds: int = 1800,
    ):
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                active_artifacts = connection.execute(
                    """
                    SELECT COUNT(*) FROM scheduler_cloud_artifacts
                    WHERE upload_status = ? AND lease_expires_at > ?
                    """,
                    (UPLOADING, now),
                ).fetchone()[0]
                active_manifests = connection.execute(
                    """
                    SELECT COUNT(*) FROM scheduler_cloud_tasks
                    WHERE manifest_upload_status = ? AND manifest_lease_expires_at > ?
                    """,
                    (UPLOADING, now),
                ).fetchone()[0]
                if int(active_artifacts) + int(active_manifests) >= int(concurrency):
                    connection.execute("COMMIT")
                    return None
                row = connection.execute(
                    """
                    SELECT task.*
                    FROM scheduler_cloud_tasks task
                    WHERE (
                        task.manifest_upload_status = ?
                        OR (task.manifest_upload_status = ?
                            AND COALESCE(task.manifest_lease_expires_at, 0) <= ?)
                    )
                      AND task.manifest_attempt_count < ?
                      AND COALESCE(task.manifest_next_attempt_at, 0) <= ?
                      AND NOT EXISTS (
                          SELECT 1 FROM scheduler_cloud_artifacts artifact
                          WHERE artifact.prompt_id=task.prompt_id
                            AND artifact.upload_status NOT IN ('uploaded','cloud_failed','source_missing')
                      )
                    ORDER BY task.created_at
                    LIMIT 1
                    """,
                    (PENDING, UPLOADING, now, int(max_attempts), now),
                ).fetchone()
                if not row:
                    connection.execute("COMMIT")
                    return None
                connection.execute(
                    """
                    UPDATE scheduler_cloud_tasks
                    SET manifest_upload_status=?, manifest_attempt_count=manifest_attempt_count+1,
                        manifest_lease_owner=?, manifest_lease_expires_at=?, updated_at=?
                    WHERE prompt_id=?
                    """,
                    (UPLOADING, claimant, now + lease_seconds, now, row["prompt_id"]),
                )
                claimed = connection.execute(
                    "SELECT * FROM scheduler_cloud_tasks WHERE prompt_id=?",
                    (row["prompt_id"],),
                ).fetchone()
                connection.execute("COMMIT")
                return dict(claimed)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def manifest_succeeded(self, prompt_id: str, result: dict) -> None:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = connection.execute(
                    "SELECT artifact_count, failed_count FROM scheduler_cloud_tasks WHERE prompt_id=?",
                    (prompt_id,),
                ).fetchone()
                if not task:
                    return
                if int(task["artifact_count"] or 0) == 0:
                    status = NO_ARTIFACTS
                elif int(task["failed_count"] or 0) > 0:
                    status = CLOUD_FAILED
                else:
                    status = CLOUD_COMPLETED
                connection.execute(
                    """
                    UPDATE scheduler_cloud_tasks
                    SET cloud_status=?, manifest_upload_status=?, manifest_size_bytes=?,
                        manifest_sha256=?, manifest_next_attempt_at=NULL,
                        manifest_lease_owner=NULL, manifest_lease_expires_at=NULL,
                        last_error=NULL, completed_at=?, updated_at=?
                    WHERE prompt_id=?
                    """,
                    (
                        status,
                        UPLOADED,
                        result.get("size_bytes"),
                        result.get("sha256"),
                        now,
                        now,
                        prompt_id,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def manifest_failed(self, prompt_id: str, error: str, max_attempts: int) -> None:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = connection.execute(
                    "SELECT manifest_attempt_count FROM scheduler_cloud_tasks WHERE prompt_id=?",
                    (prompt_id,),
                ).fetchone()
                if not task:
                    return
                attempts = int(task["manifest_attempt_count"])
                terminal = attempts >= int(max_attempts)
                delay = RETRY_DELAYS[min(max(0, attempts - 1), len(RETRY_DELAYS) - 1)]
                connection.execute(
                    """
                    UPDATE scheduler_cloud_tasks
                    SET cloud_status=?, manifest_upload_status=?, manifest_next_attempt_at=?,
                        manifest_lease_owner=NULL, manifest_lease_expires_at=NULL,
                        last_error=?, updated_at=?, completed_at=?
                    WHERE prompt_id=?
                    """,
                    (
                        CLOUD_FAILED if terminal else UPLOADING,
                        "failed" if terminal else PENDING,
                        None if terminal else now + delay,
                        str(error),
                        now,
                        now if terminal else None,
                        prompt_id,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def renew_manifest_lease(
        self, prompt_id: str, claimant: str, lease_seconds: int = 1800
    ) -> bool:
        now = time.time()
        with closing(self._connect()) as connection:
            changed = connection.execute(
                """
                UPDATE scheduler_cloud_tasks
                SET manifest_lease_expires_at=?, updated_at=?
                WHERE prompt_id=? AND manifest_upload_status=?
                  AND manifest_lease_owner=?
                """,
                (now + lease_seconds, now, prompt_id, UPLOADING, claimant),
            ).rowcount
        return bool(changed)

    def task(self, prompt_id: str) -> Optional[dict]:
        with closing(self._connect(readonly=True)) as connection:
            row = connection.execute(
                "SELECT * FROM scheduler_cloud_tasks WHERE prompt_id=?", (prompt_id,)
            ).fetchone()
        return dict(row) if row else None

    def artifacts(self, prompt_id: str) -> list[dict]:
        with closing(self._connect(readonly=True)) as connection:
            rows = connection.execute(
                "SELECT * FROM scheduler_cloud_artifacts WHERE prompt_id=? ORDER BY ordinal",
                (prompt_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def reset_failed(self, prompt_id: str) -> bool:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                changed = connection.execute(
                    """
                    UPDATE scheduler_cloud_artifacts
                    SET upload_status=?, attempt_count=0, next_attempt_at=?,
                        lease_owner=NULL, lease_expires_at=NULL, last_error=NULL, updated_at=?
                    WHERE prompt_id=? AND upload_status IN (?, ?)
                    """,
                    (PENDING, now, now, prompt_id, CLOUD_FAILED, SOURCE_MISSING),
                ).rowcount
                task_changed = connection.execute(
                    """
                    UPDATE scheduler_cloud_tasks
                    SET cloud_status=?, manifest_upload_status=?, manifest_attempt_count=0,
                        manifest_next_attempt_at=?, manifest_lease_owner=NULL,
                        manifest_lease_expires_at=NULL, last_error=NULL, completed_at=NULL,
                        updated_at=? WHERE prompt_id=?
                    """,
                    (PENDING, PENDING, now, now, prompt_id),
                ).rowcount
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return bool(changed or task_changed)


class OssV2Backend:
    def __init__(self, config: CloudArchiveConfig):
        config.validate()
        try:
            import alibabacloud_oss_v2 as oss
        except ImportError as error:
            raise RuntimeError("alibabacloud-oss-v2 is not installed") from error
        provider = oss.credentials.EnvironmentVariableCredentialsProvider()
        sdk_config = oss.Config(
            region=config.region,
            endpoint=config.endpoint,
            signature_version="v4",
            credentials_provider=provider,
            retry_max_attempts=3,
            connect_timeout=10,
            readwrite_timeout=120,
        )
        self.oss = oss
        self.client = oss.Client(sdk_config)
        os.makedirs(config.checkpoint_dir, exist_ok=True)
        self.uploader = self.client.uploader(
            parallel_num=3,
            enable_checkpoint=True,
            checkpoint_dir=config.checkpoint_dir,
        )
        self.config = config

    def upload_file(
        self,
        key: str,
        path: str,
        content_type: str,
        content_encoding: str = None,
        cache_control: str = None,
        sha256: str = None,
        private: bool = False,
    ) -> dict:
        metadata = {"server-id": self.config.server_id}
        if sha256:
            metadata["sha256"] = sha256
        request = self.oss.PutObjectRequest(
            bucket=self.config.bucket,
            key=key,
            content_type=content_type or "application/octet-stream",
            content_encoding=content_encoding,
            cache_control=cache_control,
            metadata=metadata,
            object_acl="private" if private else None,
        )
        result = self.uploader.upload_file(request, path)
        head = self.client.head_object(
            self.oss.HeadObjectRequest(bucket=self.config.bucket, key=key)
        )
        expected = os.path.getsize(path)
        if int(head.content_length or -1) != expected:
            raise RuntimeError(
                f"OSS object length mismatch: expected {expected}, got {head.content_length}"
            )
        return {
            "etag": getattr(result, "etag", None) or getattr(head, "etag", None),
            "oss_crc64": getattr(result, "hash_crc64", None)
            or getattr(head, "hash_crc64", None),
            "content_length": int(head.content_length),
        }


def _validate_public_http_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only absolute HTTP(S) artifact URLs are supported")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ValueError(f"Unable to resolve artifact host: {error}") from error
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("Remote artifact URL resolves to a non-public address")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_remote(url: str, destination: str, max_bytes: int) -> tuple[str, int]:
    _validate_public_http_url(url)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    request = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-Account-Manager/3"})
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with opener.open(request, timeout=60) as response:
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0]
        allowed = content_type.startswith(("image/", "video/", "audio/")) or content_type in (
            "application/octet-stream",
            "application/zip",
        )
        if content_type and not allowed:
            raise ValueError(f"Remote artifact content type is not media: {content_type}")
        declared = int(response.headers.get("Content-Length") or 0)
        if declared > max_bytes:
            raise ValueError("Remote artifact exceeds cloud_remote_max_bytes")
        written = 0
        with open(destination, "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError("Remote artifact exceeds cloud_remote_max_bytes")
                output.write(chunk)
    return content_type or "application/octet-stream", written


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while True:
            chunk = source.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class ManifestExporter:
    SCHEDULER_TABLES = (
        "scheduler_jobs",
        "scheduler_api_logs",
        "scheduler_assets",
        "scheduler_job_logs",
        "scheduler_workers",
        "scheduler_cloud_tasks",
        "scheduler_cloud_artifacts",
    )

    def __init__(self, scheduler_database: str, history_database: str, project_dir: str):
        self.scheduler_database = scheduler_database
        self.history_database = history_database
        self.project_dir = project_dir

    @staticmethod
    def _schema(connection, table: str) -> dict:
        sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return {
            "create_sql": sql_row[0] if sql_row else None,
            "columns": [row_dict(row) for row in columns],
        }

    def _commit(self) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", self.project_dir, "rev-parse", "--short", "HEAD"],
                text=True,
                timeout=3,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            return "unknown"

    def build(self, prompt_id: str, server_id: str, desired_status: str) -> dict:
        with closing(sqlite3.connect(
            f"file:{os.path.abspath(self.scheduler_database)}?mode=ro", uri=True
        )) as scheduler:
            scheduler.row_factory = sqlite3.Row
            scheduler.execute("BEGIN")
            job = scheduler.execute(
                "SELECT * FROM scheduler_jobs WHERE prompt_id=?", (prompt_id,)
            ).fetchone()
            api_logs = scheduler.execute(
                "SELECT * FROM scheduler_api_logs WHERE prompt_id=? ORDER BY sequence",
                (prompt_id,),
            ).fetchall()
            job_logs = scheduler.execute(
                "SELECT * FROM scheduler_job_logs WHERE prompt_id=?", (prompt_id,)
            ).fetchall()
            cloud_task = scheduler.execute(
                "SELECT * FROM scheduler_cloud_tasks WHERE prompt_id=?", (prompt_id,)
            ).fetchone()
            cloud_artifacts = scheduler.execute(
                "SELECT * FROM scheduler_cloud_artifacts WHERE prompt_id=? ORDER BY ordinal",
                (prompt_id,),
            ).fetchall()

            history_row = None
            history_parsed = None
            history_schema = {"create_sql": None, "columns": []}
            if os.path.isfile(self.history_database):
                with closing(sqlite3.connect(
                    f"file:{os.path.abspath(self.history_database)}?mode=ro", uri=True
                )) as history:
                    history.row_factory = sqlite3.Row
                    history.execute("BEGIN")
                    history_row = history.execute(
                        "SELECT * FROM history WHERE prompt_id=?", (prompt_id,)
                    ).fetchone()
                    history_schema = self._schema(history, "history")
                    if history_row:
                        try:
                            history_parsed = json.loads(history_row["data"])
                        except (TypeError, json.JSONDecodeError):
                            history_parsed = None

            related_assets = {}
            for row in scheduler.execute(
                "SELECT * FROM scheduler_assets WHERE prompt_id=?", (prompt_id,)
            ).fetchall():
                related_assets[row["asset_id"]] = {
                    "relation_basis": ["scheduler_assets.prompt_id"],
                    "row": row_dict(row),
                }
            for asset_id in _asset_ids((history_parsed or {}).get("outputs", {})):
                row = scheduler.execute(
                    "SELECT * FROM scheduler_assets WHERE asset_id=?", (asset_id,)
                ).fetchone()
                if not row:
                    continue
                item = related_assets.setdefault(
                    asset_id, {"relation_basis": [], "row": row_dict(row)}
                )
                if "history.outputs.asset_id" not in item["relation_basis"]:
                    item["relation_basis"].append("history.outputs.asset_id")

            worker = None
            if job and job["worker_port"]:
                worker_row = scheduler.execute(
                    "SELECT * FROM scheduler_workers WHERE port=?", (job["worker_port"],)
                ).fetchone()
                if worker_row:
                    worker = {
                        "snapshot_kind": "snapshot_at_export",
                        "row": row_dict(worker_row),
                    }

            schemas = {
                table: self._schema(scheduler, table) for table in self.SCHEDULER_TABLES
            }
            schemas["history"] = history_schema

        task_data = row_dict(cloud_task)
        if task_data:
            task_data["cloud_status"] = desired_status
            task_data["manifest_upload_status"] = UPLOADED
        artifact_rows = [row_dict(row) for row in cloud_artifacts]
        artifacts = [
            {
                "ordinal": row["ordinal"],
                "source_kind": row["source_kind"],
                "original_filename": row["filename"],
                "content_type": row["mime_type"],
                "size_bytes": row["size_bytes"],
                "sha256": row["sha256"],
                "oss_object_key": row["oss_object_key"],
                "oss_uri": row["oss_uri"],
                "public_url": row["public_url"],
                "upload_status": row["upload_status"],
                "attempt_count": row["attempt_count"],
                "last_error": row["last_error"],
            }
            for row in artifact_rows
        ]
        manifest = {
            "schema_version": 1,
            "export": {
                "prompt_id": prompt_id,
                "server_id": server_id,
                "exported_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                "source_scheduler_db": os.path.basename(self.scheduler_database),
                "source_history_db": os.path.basename(self.history_database),
                "plugin_commit": self._commit(),
                "generation_status": job["status"] if job else None,
                "cloud_status": desired_status,
            },
            "database_schema": schemas,
            "records": {
                "scheduler_jobs": row_dict(job),
                "scheduler_api_logs": [row_dict(row) for row in api_logs],
                "scheduler_assets": list(related_assets.values()),
                "scheduler_job_logs": [row_dict(row) for row in job_logs],
                "scheduler_workers": worker,
                "history": {
                    "row": row_dict(history_row),
                    "parsed_data": history_parsed,
                    "restore_source": "row.data",
                }
                if history_row
                else None,
                "scheduler_cloud_tasks": task_data,
                "scheduler_cloud_artifacts": artifact_rows,
            },
            "artifacts": artifacts,
            "integrity": {
                "manifest_content_sha256": None,
                "record_counts": {
                    "scheduler_jobs": 1 if job else 0,
                    "scheduler_api_logs": len(api_logs),
                    "scheduler_assets": len(related_assets),
                    "scheduler_job_logs": len(job_logs),
                    "scheduler_workers": 1 if worker else 0,
                    "history": 1 if history_row else 0,
                    "scheduler_cloud_tasks": 1 if cloud_task else 0,
                    "scheduler_cloud_artifacts": len(cloud_artifacts),
                },
                "artifact_count": len(artifacts),
                "artifact_uploaded_count": sum(
                    1 for row in artifact_rows if row["upload_status"] == UPLOADED
                ),
                "artifact_failed_count": sum(
                    1
                    for row in artifact_rows
                    if row["upload_status"] in (CLOUD_FAILED, SOURCE_MISSING)
                ),
            },
        }
        canonical = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        manifest["integrity"]["manifest_content_sha256"] = hashlib.sha256(
            canonical
        ).hexdigest()
        return manifest

    def write_gzip(self, manifest: dict, path: str, max_bytes: int) -> dict:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Do not let gzip derive its embedded FNAME from the staging path.  The
        # staging file deliberately ends in `.gz.tmp`; archive tools on Windows
        # prefer the embedded name and would otherwise extract that temporary
        # filename instead of the user-facing `manifest.json`.
        with open(path, "wb") as raw_output:
            with gzip.GzipFile(
                filename="manifest.json",
                mode="wb",
                fileobj=raw_output,
                compresslevel=6,
                mtime=0,
            ) as gzip_output:
                with io.TextIOWrapper(gzip_output, encoding="utf-8") as output:
                    json.dump(
                        manifest,
                        output,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    )
                    output.write("\n")
        size = os.path.getsize(path)
        if size > int(max_bytes):
            raise ValueError("manifest_size_limit_exceeded")
        return {"size_bytes": size, "sha256": file_sha256(path)}


class CloudArchiveManager:
    def __init__(
        self,
        config: CloudArchiveConfig,
        scheduler_database: str,
        history_database: str,
        output_dir: str,
        temp_dir: str,
        project_dir: str,
        logger=None,
        backend=None,
        start_worker: bool = True,
    ):
        config.validate()
        self.config = config
        self.store = CloudArchiveStore(scheduler_database)
        self.scheduler_database = scheduler_database
        self.history_database = history_database
        self.output_dir = output_dir
        self.temp_dir = temp_dir
        self.project_dir = project_dir
        self.logger = logger
        self._backend = backend
        self._stop = threading.Event()
        self._thread = None
        self._credentials_warning_logged = False
        self._next_reconcile_at = 0.0
        self._archive_started_at = time.time()
        self.claimant = f"{config.server_id}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.exporter = ManifestExporter(
            scheduler_database, history_database, project_dir
        )
        os.makedirs(config.staging_dir, exist_ok=True)
        if config.enabled and start_worker:
            self._thread = threading.Thread(
                target=self._run,
                name=f"cloud-archive-{config.server_id}-{os.getpid()}",
                daemon=True,
            )
            self._thread.start()

    @property
    def backend(self):
        if self._backend is None:
            self._backend = OssV2Backend(self.config)
        return self._backend

    def enqueue_completion(
        self,
        prompt_id: str,
        owner_id: str,
        worker_port: int,
        generation_status: str,
        history_item: dict,
        finished_at: float = None,
    ) -> int:
        artifacts = discover_artifacts(history_item, self.output_dir, self.temp_dir)
        self.store.enqueue_task(
            self.config,
            prompt_id,
            owner_id,
            worker_port,
            generation_status,
            finished_at or time.time(),
            artifacts,
        )
        return len(artifacts)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _log_exception(self, message: str, *args) -> None:
        if self.logger:
            self.logger.exception(message, *args)

    def _run(self) -> None:
        while not self._stop.wait(1):
            try:
                if time.time() >= self._next_reconcile_at:
                    self._reconcile_terminal_jobs()
                    self._next_reconcile_at = time.time() + 30
                if not (
                    os.getenv("OSS_ACCESS_KEY_ID")
                    and os.getenv("OSS_ACCESS_KEY_SECRET")
                ):
                    if not self._credentials_warning_logged and self.logger:
                        self.logger.error(
                            "Cloud archive is enabled but OSS credentials are not loaded; "
                            "pending rows will wait without consuming attempts"
                        )
                    self._credentials_warning_logged = True
                    self._stop.wait(10)
                    continue
                self._credentials_warning_logged = False
                artifact = self.store.claim_artifact(
                    self.claimant, self.config.upload_concurrency
                )
                if artifact:
                    self._upload_artifact(artifact)
                    continue
                task = self.store.claim_manifest(
                    self.claimant,
                    self.config.max_attempts,
                    self.config.upload_concurrency,
                )
                if task:
                    self._upload_manifest(task)
            except Exception:
                self._log_exception("Unexpected cloud archive worker error")
                self._stop.wait(2)

    def _reconcile_terminal_jobs(self, limit: int = 20) -> int:
        with closing(sqlite3.connect(self.scheduler_database)) as scheduler:
            scheduler.row_factory = sqlite3.Row
            jobs = scheduler.execute(
                """
                SELECT job.prompt_id, job.owner_id, job.worker_port, job.status,
                       COALESCE(job.finished_at, job.submitted_at) finished_at
                FROM scheduler_jobs job
                LEFT JOIN scheduler_cloud_tasks cloud
                  ON cloud.prompt_id=job.prompt_id
                WHERE cloud.prompt_id IS NULL
                  AND job.status IN ('completed','failed','cancelled','worker_lost')
                  AND COALESCE(job.finished_at, job.submitted_at) >= ?
                ORDER BY COALESCE(job.finished_at, job.submitted_at), job.sequence
                LIMIT ?
                """,
                (self._archive_started_at, max(1, int(limit))),
            ).fetchall()
        if not jobs:
            return 0

        history_by_prompt = {}
        if os.path.isfile(self.history_database):
            with closing(sqlite3.connect(self.history_database)) as history:
                for job in jobs:
                    row = history.execute(
                        "SELECT data FROM history WHERE prompt_id=?",
                        (job["prompt_id"],),
                    ).fetchone()
                    if not row:
                        continue
                    try:
                        parsed = json.loads(row[0])
                    except (TypeError, json.JSONDecodeError):
                        parsed = {}
                    history_by_prompt[job["prompt_id"]] = (
                        parsed if isinstance(parsed, dict) else {}
                    )

        registered = 0
        for job in jobs:
            item = history_by_prompt.get(job["prompt_id"], {})
            artifacts = discover_artifacts(item, self.output_dir, self.temp_dir)
            self.store.enqueue_task(
                self.config,
                job["prompt_id"],
                job["owner_id"] or "",
                int(job["worker_port"] or 0),
                job["status"],
                float(job["finished_at"] or time.time()),
                artifacts,
            )
            registered += 1
        return registered

    def _artifact_staging_path(self, artifact: dict) -> str:
        return os.path.join(
            self.config.staging_dir,
            self.config.server_id,
            artifact["prompt_id"],
            f"{int(artifact['ordinal']):03d}-{sanitize_filename(artifact['filename'])}",
        )

    @contextmanager
    def _lease_heartbeat(self, renew: Callable[[], bool]):
        stopped = threading.Event()

        def run():
            while not stopped.wait(60):
                try:
                    if not renew():
                        return
                except Exception:
                    self._log_exception("Failed to renew cloud archive lease")

        thread = threading.Thread(target=run, name="cloud-archive-lease", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=2)

    def _upload_artifact(self, artifact: dict) -> None:
        try:
            path = artifact.get("source_path")
            mime_type = artifact.get("mime_type") or "application/octet-stream"
            if artifact["source_kind"] == "remote_url":
                path = self._artifact_staging_path(artifact)
                if not os.path.isfile(path):
                    download_path = path + ".download.tmp"
                    try:
                        mime_type, _ = download_remote(
                            artifact["source_url"],
                            download_path,
                            self.config.remote_max_bytes,
                        )
                        os.replace(download_path, path)
                    except Exception:
                        try:
                            os.remove(download_path)
                        except OSError:
                            pass
                        raise
            if not path or not os.path.isfile(path):
                raise FileNotFoundError(f"Artifact source is missing: {path}")
            size = os.path.getsize(path)
            digest = file_sha256(path)
            with self._lease_heartbeat(
                lambda: self.store.renew_artifact_lease(
                    artifact["id"], self.claimant
                )
            ):
                result = self.backend.upload_file(
                    artifact["oss_object_key"], path, mime_type, sha256=digest
                )
            if os.path.getsize(path) != size or file_sha256(path) != digest:
                raise RuntimeError("Artifact source changed during upload")
            self.store.artifact_succeeded(
                artifact["id"],
                {
                    "source_path": path,
                    "mime_type": mime_type,
                    "size_bytes": size,
                    "sha256": digest,
                    "etag": result.get("etag"),
                    "oss_crc64": result.get("oss_crc64"),
                },
            )
            if artifact["source_kind"] == "remote_url":
                try:
                    os.remove(path)
                except OSError:
                    pass
        except FileNotFoundError as error:
            self.store.artifact_source_missing(artifact["id"], str(error))
            self._log_exception(
                "Cloud artifact source is missing for %s #%s",
                artifact["prompt_id"],
                artifact["ordinal"],
            )
        except Exception as error:
            self.store.artifact_failed(
                artifact["id"], str(error), self.config.max_attempts
            )
            self._log_exception(
                "Cloud artifact upload failed for %s #%s",
                artifact["prompt_id"],
                artifact["ordinal"],
            )

    def _desired_status(self, task: dict) -> str:
        if int(task.get("artifact_count") or 0) == 0:
            return NO_ARTIFACTS
        if int(task.get("failed_count") or 0) > 0:
            return CLOUD_FAILED
        return CLOUD_COMPLETED

    def _upload_manifest(self, task: dict) -> None:
        prompt_id = task["prompt_id"]
        staging = os.path.join(
            self.config.staging_dir,
            self.config.server_id,
            prompt_id,
            "manifest.json.gz.tmp",
        )
        try:
            desired = self._desired_status(task)
            manifest = self.exporter.build(prompt_id, self.config.server_id, desired)
            file_result = self.exporter.write_gzip(
                manifest, staging, self.config.manifest_max_bytes
            )
            with self._lease_heartbeat(
                lambda: self.store.renew_manifest_lease(prompt_id, self.claimant)
            ):
                self.backend.upload_file(
                    task["manifest_object_key"],
                    staging,
                    "application/gzip",
                    cache_control="no-store",
                    sha256=file_result["sha256"],
                    private=True,
                )
            self.store.manifest_succeeded(prompt_id, file_result)
            try:
                os.remove(staging)
            except OSError:
                pass
        except Exception as error:
            self.store.manifest_failed(
                prompt_id, str(error), self.config.max_attempts
            )
            self._log_exception("Cloud manifest upload failed for %s", prompt_id)

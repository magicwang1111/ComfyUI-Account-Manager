#!/usr/bin/env python3
"""Register a controlled set of historical prompts for OSS cloud archiving."""

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


PLUGIN_DIR = Path(__file__).resolve().parents[1]


def load_cloud_module():
    path = PLUGIN_DIR / "utils" / "cloud_archive.py"
    spec = importlib.util.spec_from_file_location("account_manager_cloud_archive", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as source:
            value = json.load(source)
            return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def resolve_plugin_path(config: dict, name: str, default: str) -> str:
    value = Path(str(config.get(name, default)))
    return os.fspath(value if value.is_absolute() else PLUGIN_DIR / value)


def parse_time(value: str) -> float:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.timestamp()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Register selected historical jobs for asynchronous OSS archiving."
    )
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--prompt-id", action="append", default=[])
    result.add_argument("--since", type=parse_time)
    result.add_argument("--until", type=parse_time)
    result.add_argument("--limit", type=int)
    result.add_argument("--retry-cloud-failed", action="store_true")
    return result


def has_cloud_table(connection: sqlite3.Connection) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduler_cloud_tasks'"
        ).fetchone()
    )


def select_jobs(database: str, args) -> list[dict]:
    conditions = []
    params = []
    if args.prompt_id:
        placeholders = ",".join("?" for _ in args.prompt_id)
        conditions.append(f"job.prompt_id IN ({placeholders})")
        params.extend(args.prompt_id)
    if args.since is not None:
        conditions.append("COALESCE(job.finished_at, job.submitted_at) >= ?")
        params.append(args.since)
    if args.until is not None:
        conditions.append("COALESCE(job.finished_at, job.submitted_at) <= ?")
        params.append(args.until)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        cloud_table = has_cloud_table(connection)
        cloud_join = (
            "LEFT JOIN scheduler_cloud_tasks cloud ON cloud.prompt_id=job.prompt_id"
            if cloud_table
            else ""
        )
        cloud_columns = (
            "cloud.cloud_status AS cloud_status, cloud.manifest_upload_status AS manifest_upload_status"
            if cloud_table
            else "NULL AS cloud_status, NULL AS manifest_upload_status"
        )
        sql = f"""
            SELECT job.*, {cloud_columns}
            FROM scheduler_jobs job
            {cloud_join}
            {where}
            ORDER BY COALESCE(job.finished_at, job.submitted_at), job.sequence
        """
        if args.limit is not None:
            sql += " LIMIT ?"
            params.append(args.limit)
        return [dict(row) for row in connection.execute(sql, tuple(params)).fetchall()]


def load_history(database: str, prompt_id: str) -> tuple[dict, str]:
    if not os.path.isfile(database):
        return {}, ""
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            "SELECT owner_id, data FROM history WHERE prompt_id=?", (prompt_id,)
        ).fetchone()
    if not row:
        return {}, ""
    try:
        value = json.loads(row[1])
    except (TypeError, json.JSONDecodeError):
        value = {}
    return (value if isinstance(value, dict) else {}), str(row[0] or "")


def build_config(module, config: dict):
    return module.CloudArchiveConfig(
        enabled=True,
        server_id=str(config.get("cloud_server_id", "")).strip(),
        region=str(config.get("cloud_oss_region", "cn-hangzhou")).strip(),
        endpoint=str(config.get("cloud_oss_endpoint", "")).strip(),
        bucket=str(config.get("cloud_oss_bucket", "")).strip(),
        prefix=str(config.get("cloud_oss_prefix", "")).strip().strip("/"),
        public_base_url=str(config.get("cloud_public_base_url", "")).strip().rstrip("/"),
        max_attempts=max(1, int(config.get("cloud_max_attempts", 3))),
        upload_concurrency=min(
            2, max(1, int(config.get("cloud_upload_concurrency", 2)))
        ),
        remote_max_bytes=max(
            1, int(config.get("cloud_remote_max_bytes", 20 * 1024 * 1024 * 1024))
        ),
        manifest_max_bytes=max(
            1, int(config.get("cloud_manifest_max_bytes", 2 * 1024 * 1024 * 1024))
        ),
        staging_dir=resolve_plugin_path(config, "cloud_staging_dir", "cloud_staging"),
        checkpoint_dir=resolve_plugin_path(
            config, "cloud_checkpoint_dir", "cloud_upload_checkpoints"
        ),
    )


def main() -> int:
    args = parser().parse_args()
    if not (args.prompt_id or args.since is not None or args.until is not None or args.limit):
        parser().error("at least one of --prompt-id, --since, --until, or --limit is required")
    if args.limit is not None and args.limit <= 0:
        parser().error("--limit must be a positive integer")
    if args.since is not None and args.until is not None and args.since > args.until:
        parser().error("--since must not be later than --until")

    module = load_cloud_module()
    raw_config = load_json(PLUGIN_DIR / "config.json")
    cloud_config = build_config(module, raw_config)
    cloud_config.validate()
    scheduler_db = resolve_plugin_path(raw_config, "scheduler_db", "scheduler.sqlite3")
    history_db = resolve_plugin_path(raw_config, "history_db", "history.sqlite3")
    app_dir = Path(os.getenv("APP_DIR", PLUGIN_DIR.parents[1])).resolve()
    jobs = select_jobs(scheduler_db, args)
    if not jobs:
        print("No matching scheduler jobs.")
        return 0

    store = None if args.dry_run else module.CloudArchiveStore(scheduler_db)
    registered = skipped = reset = 0
    for job in jobs:
        prompt_id = job["prompt_id"]
        current = job.get("cloud_status")
        action = "register"
        if current:
            if args.retry_cloud_failed and current == module.CLOUD_FAILED:
                action = "retry"
            else:
                action = "skip_existing"
        print(
            f"{prompt_id} generation={job.get('status')} cloud={current or '-'} action={action}"
        )
        if args.dry_run:
            continue
        if action == "skip_existing":
            skipped += 1
            continue
        if action == "retry":
            if store.reset_failed(prompt_id):
                reset += 1
            continue
        history_item, history_owner = load_history(history_db, prompt_id)
        artifacts = module.discover_artifacts(
            history_item, os.fspath(app_dir / "output"), os.fspath(app_dir / "temp")
        )
        finished_at = float(job.get("finished_at") or job.get("submitted_at") or 0)
        store.enqueue_task(
            cloud_config,
            prompt_id,
            str(job.get("owner_id") or history_owner or ""),
            int(job.get("worker_port") or 0),
            str(job.get("status") or "unknown"),
            finished_at,
            artifacts,
        )
        registered += 1

    if args.dry_run:
        print(f"Dry run: {len(jobs)} matching job(s); no database changes made.")
    else:
        print(
            f"Registered {registered}; reset {reset}; skipped existing {skipped}. "
            "Running ComfyUI cloud workers will process the pending rows asynchronously."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

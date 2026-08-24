#!/usr/bin/env python3
"""Inspect or follow the instance-log slice associated with a scheduler job."""

import argparse
import base64
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path


TERMINAL_STATES = {"completed", "failed", "cancelled", "worker_lost"}
PROJECT_DIR = Path(__file__).resolve().parents[1]


def default_database() -> Path:
    config_path = PROJECT_DIR / "config.json"
    scheduler_name = "scheduler.sqlite3"
    try:
        scheduler_name = json.loads(config_path.read_text(encoding="utf-8")).get(
            "scheduler_db", scheduler_name
        )
    except (OSError, ValueError, TypeError):
        pass
    return PROJECT_DIR / scheduler_name


def read_job(database: Path, prompt_id: str) -> dict | None:
    if not database.is_file():
        raise FileNotFoundError(f"scheduler database not found: {database}")
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT prompt_id, owner_id, ingress_port, worker_port, status,
                       submitted_at, started_at, finished_at, error,
                       log_file, log_start_offset, log_end_offset,
                       log_start_line, log_end_line
                FROM scheduler_jobs
                WHERE prompt_id = ?
                """,
                (prompt_id,),
            ).fetchone()
        except sqlite3.OperationalError as error:
            if "no such column" in str(error).lower():
                raise RuntimeError(
                    "scheduler database has not been migrated for job logs; "
                    "restart one Account Manager instance first"
                ) from error
            raise
        job = dict(row) if row else None
        if job:
            try:
                stored = connection.execute(
                    """
                    SELECT content_bytes, truncated, start_line, end_line
                    FROM scheduler_job_logs WHERE prompt_id = ?
                    """,
                    (prompt_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                stored = None
            job["stored_log_bytes"] = stored["content_bytes"] if stored else None
            job["stored_log_truncated"] = bool(stored["truncated"]) if stored else None
            if stored:
                job["log_start_line"] = stored["start_line"]
                job["log_end_line"] = stored["end_line"]
            try:
                job["api_record_count"] = connection.execute(
                    "SELECT COUNT(*) FROM scheduler_api_logs WHERE prompt_id = ?",
                    (prompt_id,),
                ).fetchone()[0]
            except sqlite3.OperationalError:
                job["api_record_count"] = 0
    return job


def read_stored_log(database: Path, prompt_id: str) -> bytes | None:
    with closing(sqlite3.connect(database)) as connection:
        try:
            row = connection.execute(
                "SELECT content FROM scheduler_job_logs WHERE prompt_id = ?",
                (prompt_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    return bytes(row[0]) if row else None


def print_status(job: dict) -> None:
    fields = (
        "prompt_id",
        "status",
        "owner_id",
        "ingress_port",
        "worker_port",
        "submitted_at",
        "started_at",
        "finished_at",
        "log_file",
        "log_start_offset",
        "log_end_offset",
        "log_start_line",
        "log_end_line",
        "stored_log_bytes",
        "stored_log_truncated",
        "api_record_count",
        "error",
    )
    print(json.dumps({key: job.get(key) for key in fields}, ensure_ascii=False, indent=2))


def _body_json(value):
    if value is None:
        return None
    raw = bytes(value)
    try:
        return {"encoding": "utf-8", "data": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return {"encoding": "base64", "data": base64.b64encode(raw).decode("ascii")}


def show_api_logs(database: Path, prompt_id: str) -> int:
    job = read_job(database, prompt_id)
    if not job:
        print(f"job not found: {prompt_id}", file=sys.stderr)
        return 2
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT * FROM scheduler_api_logs WHERE prompt_id = ? ORDER BY sequence",
                (prompt_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    records = []
    for row in rows:
        record = dict(row)
        record["request_body"] = _body_json(record.get("request_body"))
        record["response_body"] = _body_json(record.get("response_body"))
        records.append(record)
    print(json.dumps(records, ensure_ascii=False, indent=2))
    return 0


def copy_available(job: dict, position: int, output) -> int:
    log_file = job.get("log_file")
    if not log_file:
        return position
    path = Path(log_file)
    if not path.is_file():
        return position
    end_offset = job.get("log_end_offset")
    try:
        available = path.stat().st_size
        end = min(available, int(end_offset)) if end_offset is not None else available
        if position >= end:
            return position
        with path.open("rb") as source:
            source.seek(position)
            remaining = end - position
            while remaining:
                chunk = source.read(min(65536, remaining))
                if not chunk:
                    break
                output.write(chunk)
                output.flush()
                position += len(chunk)
                remaining -= len(chunk)
    except OSError:
        return position
    return position


def show_job(database: Path, prompt_id: str, follow: bool) -> int:
    job = read_job(database, prompt_id)
    if not job:
        print(f"job not found: {prompt_id}", file=sys.stderr)
        return 2
    if not follow:
        stored = read_stored_log(database, prompt_id)
        if stored is not None:
            sys.stdout.buffer.write(stored)
            sys.stdout.buffer.flush()
            return 0
    if job.get("status") == "queued":
        if not follow:
            print("job is queued and has no worker log yet", file=sys.stderr)
            return 3
        position = 0
    else:
        position = int(job.get("log_start_offset") or 0)

    output = sys.stdout.buffer
    while True:
        job = read_job(database, prompt_id)
        if not job:
            return 2
        if job.get("log_file") and position == 0:
            position = int(job.get("log_start_offset") or 0)
        position = copy_available(job, position, output)
        if not follow:
            return 0
        end_offset = job.get("log_end_offset")
        if job.get("status") in TERMINAL_STATES:
            if end_offset is None or position >= int(end_offset):
                return 0
        time.sleep(0.5)


def line_at_offset(path: Path, offset: int) -> int:
    newline_count = 0
    remaining = max(0, int(offset))
    with path.open("rb") as log:
        while remaining:
            chunk = log.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            newline_count += chunk.count(b"\n")
            remaining -= len(chunk)
    return newline_count + 1


def backfill_lines(database: Path) -> int:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
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
            )
            """
        )
        rows = connection.execute(
            """
            SELECT prompt_id, worker_port, log_file,
                   log_start_offset, log_end_offset
            FROM scheduler_jobs
            WHERE log_file IS NOT NULL
              AND log_start_offset IS NOT NULL
              AND log_end_offset IS NOT NULL
            """
        ).fetchall()
        updated = 0
        for row in rows:
            path = Path(row["log_file"])
            if not path.is_file():
                continue
            start_line = (
                line_at_offset(path, row["log_start_offset"])
                if row["log_start_offset"] is not None
                else None
            )
            end_line = (
                line_at_offset(path, row["log_end_offset"])
                if row["log_end_offset"] is not None
                else None
            )
            connection.execute(
                """
                UPDATE scheduler_jobs
                SET log_start_line = COALESCE(log_start_line, ?),
                    log_end_line = COALESCE(log_end_line, ?)
                WHERE prompt_id = ?
                """,
                (start_line, end_line, row["prompt_id"]),
            )
            byte_count = max(0, int(row["log_end_offset"]) - int(row["log_start_offset"]))
            with path.open("rb") as log:
                log.seek(int(row["log_start_offset"]))
                content = log.read(byte_count)
            connection.execute(
                """
                INSERT INTO scheduler_job_logs(
                    prompt_id, worker_port, log_file,
                    start_offset, end_offset, start_line, end_line,
                    content, content_bytes, truncated, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))
                ON CONFLICT(prompt_id) DO UPDATE SET
                    content=excluded.content,
                    content_bytes=excluded.content_bytes,
                    start_line=excluded.start_line,
                    end_line=excluded.end_line,
                    truncated=excluded.truncated,
                    captured_at=excluded.captured_at
                """,
                (
                    row["prompt_id"], row["worker_port"], str(path),
                    row["log_start_offset"], row["log_end_offset"],
                    start_line, end_line, content, len(content),
                    0,
                ),
            )
            updated += 1
        connection.commit()
    print(f"backfilled_jobs={updated}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show the exact per-instance log slice for a scheduler job ID."
    )
    parser.add_argument("prompt_id", nargs="?", help="ComfyUI prompt/job ID")
    parser.add_argument("-f", "--follow", action="store_true", help="follow in real time")
    parser.add_argument(
        "--status", action="store_true", help="print job/log metadata as JSON"
    )
    parser.add_argument(
        "--api", action="store_true", help="print every captured API request and response"
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=default_database(),
        help="scheduler.sqlite3 path",
    )
    parser.add_argument(
        "--backfill-lines",
        action="store_true",
        help="write line ranges for existing jobs that already have byte offsets",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.backfill_lines:
            return backfill_lines(args.database)
        if not args.prompt_id:
            print("prompt_id is required unless --backfill-lines is used", file=sys.stderr)
            return 2
        if args.status:
            job = read_job(args.database, args.prompt_id)
            if not job:
                print(f"job not found: {args.prompt_id}", file=sys.stderr)
                return 2
            print_status(job)
            return 0
        if args.api:
            return show_api_logs(args.database, args.prompt_id)
        return show_job(args.database, args.prompt_id, args.follow)
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

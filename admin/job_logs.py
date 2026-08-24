#!/usr/bin/env python3
"""Inspect or follow the instance-log slice associated with a scheduler job."""

import argparse
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
                       log_file, log_start_offset, log_end_offset
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
    return dict(row) if row else None


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
        "error",
    )
    print(json.dumps({key: job.get(key) for key in fields}, ensure_ascii=False, indent=2))


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show the exact per-instance log slice for a scheduler job ID."
    )
    parser.add_argument("prompt_id", help="ComfyUI prompt/job ID")
    parser.add_argument("-f", "--follow", action="store_true", help="follow in real time")
    parser.add_argument(
        "--status", action="store_true", help="print job/log metadata as JSON"
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=default_database(),
        help="scheduler.sqlite3 path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.status:
            job = read_job(args.database, args.prompt_id)
            if not job:
                print(f"job not found: {args.prompt_id}", file=sys.stderr)
                return 2
            print_status(job)
            return 0
        return show_job(args.database, args.prompt_id, args.follow)
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

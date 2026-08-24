import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "admin" / "job_logs.py"


class JobLogsCliTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "scheduler.sqlite3"
        self.log_file = Path(self.temp_dir.name) / "6006.log"
        self.log_file.write_bytes(b"startup\njob line 1\njob line 2\nafter\n")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                CREATE TABLE scheduler_jobs (
                    prompt_id TEXT PRIMARY KEY,
                    owner_id TEXT,
                    ingress_port INTEGER,
                    worker_port INTEGER,
                    status TEXT,
                    submitted_at REAL,
                    started_at REAL,
                    finished_at REAL,
                    error TEXT,
                    log_file TEXT,
                    log_start_offset INTEGER,
                    log_end_offset INTEGER,
                    log_start_line INTEGER,
                    log_end_line INTEGER
                )
                """
            )
            connection.execute(
                """
                INSERT INTO scheduler_jobs VALUES (
                    'job-1', 'user-a', 6007, 6006, 'completed',
                    1, 2, 3, NULL, ?, 8, 30, 2, 3
                )
                """,
                (os.fspath(self.log_file),),
            )
            connection.execute(
                """
                CREATE TABLE scheduler_job_logs (
                    prompt_id TEXT PRIMARY KEY,
                    content BLOB,
                    content_bytes INTEGER,
                    truncated INTEGER,
                    start_line INTEGER,
                    end_line INTEGER
                )
                """
            )
            connection.execute(
                "INSERT INTO scheduler_job_logs VALUES (?, ?, ?, ?, ?, ?)",
                ("job-1", b"stored job log\n", 15, 0, 2, 3),
            )
            connection.execute(
                """
                CREATE TABLE scheduler_api_logs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_id TEXT,
                    recorded_at REAL,
                    client TEXT,
                    method TEXT,
                    url TEXT,
                    request_headers TEXT,
                    request_body BLOB,
                    response_status INTEGER,
                    response_headers TEXT,
                    response_body BLOB,
                    error TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO scheduler_api_logs(
                    prompt_id, recorded_at, client, method, url,
                    request_headers, request_body, response_status,
                    response_headers, response_body, error
                ) VALUES ('job-1', 4, 'httpx', 'POST', 'https://api.example/tasks',
                          '{}', ?, 200, '{}', ?, NULL)
                """,
                (b'{"prompt":"original"}', b'{"task_id":"task-123"}'),
            )
            connection.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_cli(self, *arguments):
        return subprocess.run(
            [
                sys.executable,
                os.fspath(SCRIPT),
                *arguments,
                "--database",
                os.fspath(self.database),
            ],
            capture_output=True,
            check=False,
        )

    def test_show_prints_only_the_indexed_job_slice(self):
        result = self.run_cli("job-1")
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertEqual(b"stored job log\n", result.stdout)

    def test_status_reports_worker_and_log_metadata(self):
        result = self.run_cli("job-1", "--status")
        self.assertEqual(0, result.returncode, result.stderr.decode())
        output = result.stdout.decode("utf-8")
        self.assertIn('"worker_port": 6006', output)
        self.assertIn('"log_start_offset": 8', output)
        self.assertIn('"log_end_offset": 30', output)
        self.assertIn('"log_start_line": 2', output)
        self.assertIn('"log_end_line": 3', output)
        self.assertIn('"stored_log_bytes": 15', output)
        self.assertIn('"api_record_count": 1', output)

    def test_api_prints_original_request_and_response_bodies(self):
        result = self.run_cli("job-1", "--api")
        self.assertEqual(0, result.returncode, result.stderr.decode())
        records = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual("POST", records[0]["method"])
        self.assertEqual('{"prompt":"original"}', records[0]["request_body"]["data"])
        self.assertEqual('{"task_id":"task-123"}', records[0]["response_body"]["data"])

    def test_backfill_keeps_log_larger_than_one_megabyte(self):
        original = b"api-record\n" * 100000
        self.log_file.write_bytes(b"startup\n" + original)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                UPDATE scheduler_jobs
                SET log_start_offset = 8, log_end_offset = ?
                WHERE prompt_id = 'job-1'
                """,
                (8 + len(original),),
            )
            connection.execute("DROP TABLE scheduler_job_logs")
            connection.commit()

        result = self.run_cli("--backfill-lines")
        self.assertEqual(0, result.returncode, result.stderr.decode())
        with closing(sqlite3.connect(self.database)) as connection:
            content, content_bytes, truncated = connection.execute(
                """
                SELECT content, content_bytes, truncated
                FROM scheduler_job_logs WHERE prompt_id = 'job-1'
                """
            ).fetchone()
        self.assertEqual(original, content)
        self.assertEqual(len(original), content_bytes)
        self.assertEqual(0, truncated)


if __name__ == "__main__":
    unittest.main()

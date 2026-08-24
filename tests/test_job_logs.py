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
                    log_end_offset INTEGER
                )
                """
            )
            connection.execute(
                """
                INSERT INTO scheduler_jobs VALUES (
                    'job-1', 'user-a', 6007, 6006, 'completed',
                    1, 2, 3, NULL, ?, 8, 30
                )
                """,
                (os.fspath(self.log_file),),
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
        self.assertEqual(b"job line 1\njob line 2\n", result.stdout)

    def test_status_reports_worker_and_log_metadata(self):
        result = self.run_cli("job-1", "--status")
        self.assertEqual(0, result.returncode, result.stderr.decode())
        output = result.stdout.decode("utf-8")
        self.assertIn('"worker_port": 6006', output)
        self.assertIn('"log_start_offset": 8', output)
        self.assertIn('"log_end_offset": 30', output)


if __name__ == "__main__":
    unittest.main()

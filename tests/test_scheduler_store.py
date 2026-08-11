import os
import importlib.util
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

module_path = Path(__file__).parents[1] / "utils" / "scheduler_store.py"
spec = importlib.util.spec_from_file_location("scheduler_store", module_path)
scheduler_store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduler_store)

CANCELLED = scheduler_store.CANCELLED
WORKER_LOST = scheduler_store.WORKER_LOST
SchedulerStore = scheduler_store.SchedulerStore


def queue_item(number: float, prompt_id: str, client_id: str = "") -> tuple:
    extra = {"client_id": client_id} if client_id else {}
    return (number, prompt_id, {"1": {"class_type": "Test"}}, extra, ["1"], {})


class SchedulerStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.temp_dir.name, "scheduler.sqlite3")
        self.store = SchedulerStore(self.database, max_concurrent_jobs_per_user=6)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_claims_are_unique_across_thirty_workers(self):
        for index in range(30):
            self.store.enqueue(queue_item(index, f"job-{index}"), f"user-{index}", 8180)

        claimed = []
        lock = threading.Lock()

        def claim(port):
            store = SchedulerStore(self.database, max_concurrent_jobs_per_user=6)
            store.register_worker(port, port)
            item = store.claim(port, port)
            with lock:
                claimed.append(item[1] if item else None)

        threads = [threading.Thread(target=claim, args=(8180 + i,)) for i in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(30, len(set(claimed)))
        self.assertNotIn(None, claimed)

    def test_seventh_job_waits_until_a_slot_is_released(self):
        for index in range(7):
            self.store.enqueue(queue_item(index, f"job-{index}"), "user-a", 8180)
        for index in range(7):
            self.store.register_worker(8180 + index, 100 + index)

        first_six = [self.store.claim(8180 + i, 100 + i) for i in range(6)]
        self.assertTrue(all(first_six))
        self.assertIsNone(self.store.claim(8186, 106))

        self.store.complete("job-0", 8180, succeeded=True)
        seventh = self.store.claim(8186, 106)
        self.assertEqual("job-6", seventh[1])

    def test_admin_jobs_are_not_limited(self):
        for index in range(8):
            self.store.enqueue(
                queue_item(index, f"admin-{index}"),
                "admin",
                8180,
                concurrency_exempt=True,
            )
            self.store.register_worker(8180 + index, 200 + index)
        claimed = [self.store.claim(8180 + i, 200 + i) for i in range(8)]
        self.assertTrue(all(claimed))

    def test_full_user_does_not_block_another_user(self):
        for index in range(7):
            self.store.enqueue(queue_item(index, f"a-{index}"), "user-a", 8180)
        self.store.enqueue(queue_item(100, "b-0"), "user-b", 8180)
        for index in range(7):
            self.store.register_worker(8180 + index, 300 + index)
        for index in range(6):
            self.assertIsNotNone(self.store.claim(8180 + index, 300 + index))
        claimed = self.store.claim(8186, 306)
        self.assertEqual("b-0", claimed[1])

    def test_five_users_can_fill_thirty_workers(self):
        for user_index in range(5):
            for job_index in range(6):
                sequence = user_index * 6 + job_index
                self.store.enqueue(
                    queue_item(sequence, f"u{user_index}-{job_index}"),
                    f"user-{user_index}",
                    8180,
                )
        claimed = []
        for worker_index in range(30):
            port = 8180 + worker_index
            self.store.register_worker(port, 500 + worker_index)
            claimed.append(self.store.claim(port, 500 + worker_index))
        self.assertTrue(all(claimed))
        self.assertEqual(30, len({item[1] for item in claimed}))

    def test_cancel_only_affects_visible_queued_job(self):
        self.store.enqueue(queue_item(1, "a"), "user-a", 8180)
        self.assertFalse(self.store.cancel("a", "user-b", admin=False))
        self.assertTrue(self.store.cancel("a", "user-a", admin=False))
        self.assertEqual(CANCELLED, self.store.get_job("a")["status"])

    def test_stale_worker_marks_job_lost_without_requeue(self):
        self.store.enqueue(queue_item(1, "lost"), "user-a", 8180)
        self.store.register_worker(8181, 400)
        self.assertIsNotNone(self.store.claim(8181, 400))

        with closing(self.store._connect()) as connection:
            connection.execute(
                "UPDATE scheduler_workers SET last_heartbeat = ? WHERE port = 8181",
                (time.time() - 120,),
            )
        lost = self.store.heartbeat(8182, 401, stale_seconds=60)
        self.assertEqual(["lost"], lost)
        self.assertEqual(WORKER_LOST, self.store.get_job("lost")["status"])

        self.store.register_worker(8183, 402)
        self.assertIsNone(self.store.claim(8183, 402))

    def test_client_ingress_uses_latest_submission(self):
        self.store.enqueue(queue_item(1, "a", "client-1"), "user-a", 8180)
        self.store.enqueue(queue_item(2, "b", "client-1"), "user-a", 8189)
        self.assertEqual(8189, self.store.client_ingress("client-1"))

    def test_asset_index_records_owner_and_worker(self):
        self.store.upsert_asset(
            "asset-1",
            "user-a",
            "job-1",
            8187,
            {"id": "asset-1", "fullpath": "/mnt/ComfyUI/output/a.mp4"},
        )
        row = self.store.get_asset("asset-1")
        self.assertEqual("user-a", row["owner_id"])
        self.assertEqual(8187, row["worker_port"])

    def test_prompt_classification_uses_configured_gpu_node_types(self):
        store = SchedulerStore(
            self.database,
            gpu_node_types=["UNETLoader", "MiniMaxH3TurboSampler"],
        )
        gpu_item = list(queue_item(1, "gpu"))
        gpu_item[2] = {"1": {"class_type": "UNETLoader"}}
        api_item = list(queue_item(2, "api"))
        api_item[2] = {
            "1": {"class_type": "MiniMax H3 Generate Video"},
            "2": {"class_type": "MiniMax H3 Preview Video"},
        }

        self.assertEqual("gpu", store.classify_item(tuple(gpu_item)))
        self.assertEqual("api", store.classify_item(tuple(api_item)))

    def test_workers_only_claim_jobs_from_their_resource_pool(self):
        store = SchedulerStore(self.database, gpu_node_types=["UNETLoader"])
        gpu_item = list(queue_item(1, "gpu"))
        gpu_item[2] = {"1": {"class_type": "UNETLoader"}}
        store.enqueue(tuple(gpu_item), "user-a", 8180)
        store.enqueue(queue_item(2, "api"), "user-a", 8180)
        store.register_worker(8181, 101, "api")
        store.register_worker(8182, 102, "gpu")

        api_claim = store.claim(8181, 101, "api")
        gpu_claim = store.claim(8182, 102, "gpu")

        self.assertEqual("api", api_claim[1])
        self.assertEqual("gpu", gpu_claim[1])
        self.assertEqual("api", store.get_job("api")["resource_class"])
        self.assertEqual("gpu", store.get_job("gpu")["resource_class"])

    def test_per_user_limits_are_counted_separately_per_resource_pool(self):
        store = SchedulerStore(
            self.database,
            max_concurrent_jobs_per_user=6,
            resource_concurrency_limits={"gpu": 1, "api": 3},
        )
        for index in range(2):
            store.enqueue(
                queue_item(index, f"gpu-{index}"),
                "user-a",
                8180,
                resource_class="gpu",
            )
            store.register_worker(8200 + index, 200 + index, "gpu")
        for index in range(3):
            store.enqueue(
                queue_item(10 + index, f"api-{index}"),
                "user-a",
                8180,
                resource_class="api",
            )
            store.register_worker(8210 + index, 210 + index, "api")

        self.assertIsNotNone(store.claim(8200, 200, "gpu"))
        self.assertIsNone(store.claim(8201, 201, "gpu"))
        self.assertTrue(
            all(store.claim(8210 + index, 210 + index, "api") for index in range(3))
        )

    def test_existing_scheduler_database_is_migrated_in_place(self):
        legacy_database = os.path.join(self.temp_dir.name, "legacy.sqlite3")
        with closing(sqlite3.connect(legacy_database)) as connection:
            connection.executescript(
                """
                CREATE TABLE scheduler_jobs (
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
                CREATE TABLE scheduler_workers (
                    port INTEGER PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    active_prompt_id TEXT,
                    last_heartbeat REAL NOT NULL
                );
                """
            )
            gpu_item = list(queue_item(1, "legacy-gpu"))
            gpu_item[2] = {"1": {"class_type": "UNETLoader"}}
            connection.execute(
                """
                INSERT INTO scheduler_jobs (
                    prompt_id, owner_id, ingress_port, priority, payload,
                    status, concurrency_limit, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-gpu",
                    "user-a",
                    8180,
                    1,
                    json.dumps(gpu_item),
                    "queued",
                    6,
                    time.time(),
                ),
            )
            connection.commit()

        migrated = SchedulerStore(
            legacy_database,
            resource_concurrency_limits={"gpu": 1, "api": 3},
            gpu_node_types=["UNETLoader"],
        )
        with closing(migrated._connect()) as connection:
            job_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(scheduler_jobs)"
                ).fetchall()
            }
            worker_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(scheduler_workers)"
                ).fetchall()
            }
        self.assertIn("resource_class", job_columns)
        self.assertIn("resource_class", worker_columns)
        migrated_job = migrated.get_job("legacy-gpu")
        self.assertEqual("gpu", migrated_job["resource_class"])
        self.assertEqual(1, migrated_job["concurrency_limit"])


if __name__ == "__main__":
    unittest.main()

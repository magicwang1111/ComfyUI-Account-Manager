import os
import importlib.util
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


if __name__ == "__main__":
    unittest.main()
